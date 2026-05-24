#!/usr/bin/env python3
# [Input] Consume backend/.env, HTTP requests, database/auth/config modules.
# [Output] Publish FastAPI application and REST/SSE routes.
# [Pos] backend API entrypoint
# [Sync] 2026-05-24: load backend/.env before importing config and route modules.
# [Sync] 2026-05-24: keep only current Ink Agent env keys after dotenv loading.
"""FastAPI-based voice analysis server with sync API support."""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(_BACKEND_ENV_FILE, override=False)


def _drop_unsupported_agent_env() -> None:
    """Remove stale Agent env aliases that are outside this project's contract."""

    allowed_ink_names = {
        "INK_AGENT_ENABLE_MEMORY_MCP",
        "INK_AGENT_TTL_S",
        "INK_AGENT_SWEEP_INTERVAL_S",
        "INK_AGENT_SSE_KEEPALIVE_S",
        "INK_AGENT_MAX_TURNS",
        "INK_AGENT_CONTEXT_SESSIONS",
    }
    os.environ.pop("ANTHROPIC_API_KEY", None)
    for key in list(os.environ):
        if key.startswith("INK_AGENT_MEM0_") or key in allowed_ink_names:
            continue
        if key.startswith("INK_AGENT_"):
            os.environ.pop(key, None)
            continue
        if key.startswith("CLAUDE_CODE_") and key.endswith("_TOKEN"):
            os.environ.pop(key, None)


_drop_unsupported_agent_env()

os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

import asyncio
from datetime import datetime
import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from polycli.orchestration.session_registry import session_def, get_registry
from polycli.integrations.fastapi import mount_control_panel
from polycli import PolyAgent
from stateless_analyzer import analyze_stateless
from speech_recognition import init_speech_recognition
import config
from typing import Optional, List, Any
from pydantic import BaseModel

# Import database and auth modules
import database
import auth

SUPPORTED_LANGUAGES = {"en", "zh"}
DEFAULT_LANGUAGE = "en"
BACKEND_VERSION = os.environ.get("BACKEND_VERSION", "unknown")
PUBLIC_BASE_URL = os.environ.get("INK_PUBLIC_BASE_URL", "/")


def normalize_language_code(language: Optional[str]) -> str:
    if not language:
        return DEFAULT_LANGUAGE
    language = language.lower()
    if language.startswith("zh"):
        return "zh"
    return "en"


def _count_mixed_words(text: str) -> int:
    """
    Count words in mixed Chinese/English text.
    - CJK characters count as 1 each
    - English is counted by whitespace-separated tokens
    """
    word_count = 0
    for ch in text:
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0x3040 <= code <= 0x309F or 0x30A0 <= code <= 0x30FF):
            word_count += 1
    import re
    english_words = re.sub(r"[\u4E00-\u9FFF\u3400-\u4DBF\u3040-\u309F\u30A0-\u30FF]", " ", text)
    word_count += len([w for w in english_words.split() if w])
    return word_count




def _load_all_notes_text(user_id: int) -> str:
    sessions = database.get_all_sessions_with_text(user_id)
    texts = [s.get("text", "") for s in sessions if (s.get("text") or "").strip()]
    return "\n\n".join(texts)

def resolve_language(_user_id: int, requested_language: Optional[str] = None) -> str:
    """Return a supported language code, falling back to default."""
    if requested_language:
        code = normalize_language_code(requested_language)
        if code in SUPPORTED_LANGUAGES:
            return code
    return DEFAULT_LANGUAGE


def language_instruction(language_code: str, detail: str = "") -> str:
    if language_code == "zh":
        base = "请使用简体中文输出所有内容。"
    else:
        base = "Respond in English."
    if detail:
        return f"{base} {detail}".strip()
    return base


# ========== Session Definitions (PolyCLI) ==========


@session_def(
    name="Get Writing Suggestion",
    description="Get AI-powered writing inspiration from a voice persona",
    params={
        "text": {"type": "str"},
        "user_id": {"type": "int"},
        "meta_prompt": {"type": "str"},
        "state_prompt": {"type": "str"},
    },
    category="Writing",
)
def get_writing_suggestion(
    text: str, user_id: int, meta_prompt: str = "", state_prompt: str = ""
):
    """Generate writing inspiration from a random voice persona."""
    print(f"\n{'=' * 60}")
    print(f"✍️  get_writing_suggestion() called")
    print(f"   Text length: {len(text)} chars")
    print(f"{'=' * 60}\n")

    if not text or len(text.strip()) < 10:
        return {"success": False, "error": "Text too short"}

    # @@@ Load voices from user's enabled decks (deck system)
    import random

    voices = database.load_voices_from_user_decks(user_id)

    if not voices:
        return {"success": False, "error": "No enabled voices found in your decks"}

    # Pick a random enabled voice
    voice_key = random.choice(list(voices.keys()))
    voice_info = voices[voice_key]

    print(f"🎭 Selected voice: {voice_info['name']} ({voice_key})")
    print(f"📚 Selected from {len(voices)} enabled voices")

    agent = PolyAgent(id="writing-suggester")

    # Build system prompt - voice gives inspiration, not continuation
    system_prompt = f"""You are {voice_info["name"]}, an inner voice persona.
Your role: {voice_info.get("systemPrompt", "")}

Read what the user just wrote and offer a VERY SHORT, gentle nudge about what to write next.

IMPORTANT STYLE:
- Keep it EXTREMELY brief (1 short sentence, max 15 words)
- Be warm, conversational, and friendly
- Focus on inspiration and possibility, not criticism
- Suggest what to explore next, don't analyze what was written
- Use casual, everyday language
- Think: "What if you..." or "Maybe explore..." or "How about..."

DO NOT:
- Analyze or critique their writing
- Summarize what they wrote
- Give formal feedback
- Be verbose or explanatory

Examples of GOOD suggestions:
- "What if you describe how that made you feel?"
- "Maybe explore what happened next?"
- "I'm curious about the details..."
- "How did that moment change things?"

Speak in {voice_info["name"]}'s characteristic style, but keep it brief and inspiring."""

    if state_prompt:
        system_prompt += f"\n\nEmotional context: {state_prompt}"

    if meta_prompt:
        system_prompt += f"\n\nWriter's style: {meta_prompt}"

    user_prompt = f"""The user just wrote:

{text}

Give them ONE very short, gentle nudge about what to write next (max 15 words)."""

    # Generate inspiration
    print(f"📤 Calling agent.run() with model='{config.VOICE_INSPIRATION_MODEL}'...")
    result = agent.run(
        user_prompt,
        system_prompt=system_prompt,
        model=config.VOICE_INSPIRATION_MODEL,
        cli="no-tools",
        tracked=True,
    )

    if not result.is_success or not result.content:
        print(f"⚠️  Failed to generate inspiration")
        inspiration = None
    else:
        inspiration = result.content.strip()
        print(f"✅ Got inspiration: {inspiration[:100]}...")

    print(f"\n📦 Returning voice inspiration\n")

    return {
        "success": True,
        "inspiration": inspiration,
        "voice": voice_info["name"],
        "voice_key": voice_key,
        "icon": voice_info["icon"],
        "color": voice_info["color"],
    }


@session_def(
    name="Chat with Voice",
    description="Have a conversation with a specific inner voice persona",
    params={
        "voice_id": {"type": "str"},
        "user_id": {"type": "int"},
        "conversation_history": {"type": "list"},
        "user_message": {"type": "str"},
        "original_text": {"type": "str"},
        "meta_prompt": {"type": "str"},
        "state_prompt": {"type": "str"},
    },
    category="Chat",
)
def chat_with_voice(
    voice_id: str,
    user_id: int,
    conversation_history: list,
    user_message: str,
    original_text: str = "",
    meta_prompt: str = "",
    state_prompt: str = "",
):
    """Chat with a specific voice persona."""
    print(f"\n{'=' * 60}")
    print(f"💬 chat_with_voice() called")
    print(f"   Voice ID: {voice_id}")
    print(f"   User ID: {user_id}")
    print(f"   User message: {user_message}")
    print(f"   History length: {len(conversation_history)}")
    print(f"   Meta prompt: {repr(meta_prompt)[:100]}")
    print(f"   State prompt: {repr(state_prompt)[:100]}")
    print(f"{'=' * 60}\n")

    # @@@ Load voices from user's enabled decks (deck system)
    voices = database.load_voices_from_user_decks(user_id)

    # @@@ Get voice config for this specific voice
    if voice_id in voices:
        voice_config = voices[voice_id]
        voice_name = voice_config.get("name", voice_id)
        print(f"📚 Loaded voice from deck system: {voice_id} ({voice_name})")
    else:
        # Fallback: voice might be disabled or not in user's decks
        return {
            "success": False,
            "error": f"Voice {voice_id} not found in your enabled decks. Please enable it in the Decks tab.",
        }

    agent = PolyAgent(id=f"voice-chat-{voice_name.lower()}")

    # Build system prompt for this voice
    system_prompt = f"""You are {voice_name}, an inner voice archetype from Disco Elysium.

Your character: {voice_config.get("systemPrompt", "")}

Respond in character as {voice_name}. Be concise (1-3 sentences). Stay true to your archetype.
Use the conversation context but focus on your unique perspective."""

    # Add original writing area text if available
    if original_text and original_text.strip():
        system_prompt += f"""

Context: The user is writing this text:
---
{original_text.strip()}
---

Your initial comment was about this text. Keep this context in mind when responding to the user's questions."""

    # Add meta prompt if available
    if meta_prompt and meta_prompt.strip():
        system_prompt += f"""

Additional instructions:
{meta_prompt.strip()}"""

    # Add state prompt if available
    if state_prompt and state_prompt.strip():
        system_prompt += f"""

User's current state:
{state_prompt.strip()}"""

    # Build full prompt with conversation history
    prompt = system_prompt + "\n\nConversation history:\n"

    # Add conversation history
    for msg in conversation_history:
        role_label = "User" if msg["role"] == "user" else voice_name
        prompt += f"\n{role_label}: {msg['content']}"

    # Add user's new message
    prompt += f"\n\nUser: {user_message}\n\n{voice_name}:"

    # Get response from LLM
    result = agent.run(prompt, model=config.VOICE_CHAT_MODEL, cli="no-tools", tracked=True)

    if not result.is_success or not result.content:
        response = "..."
    else:
        response = result.content

    print(f"✅ Got response: {response[:100]}...")

    return {"response": response, "voice_name": voice_name}


@session_def(
    name="Analyze Voices",
    description="Get one new voice comment for text",
    params={
        "text": {"type": "str"},
        "editor_session_id": {"type": "str"},
        "user_id": {"type": "int"},
        "applied_comments": {"type": "list"},
        "meta_prompt": {"type": "str"},
        "state_prompt": {"type": "str"},
        "overlapped_phrases": {"type": "list"},
        "not_found_phrases": {"type": "list"},
    },
    category="Analysis",
)
def analyze_text(
    text: str,
    editor_session_id: str,
    user_id: int,
    applied_comments: list = None,
    meta_prompt: str = "",
    state_prompt: str = "",
    overlapped_phrases: list = None,
    not_found_phrases: list = None,
):
    """Stateless analysis - returns ONE new comment based on text and applied comments."""
    print(f"\n{'=' * 60}")
    print(f"🎯 Stateless analyze_text() called")
    print(f"   User ID: {user_id}")
    print(f"   Text: {text[:100]}...")
    print(f"   Applied comments: {len(applied_comments or [])}")
    print(f"   Overlapped phrases: {len(overlapped_phrases or [])}")
    print(f"   Not found phrases: {len(not_found_phrases or [])}")
    print(f"   Meta prompt: {repr(meta_prompt)[:100]}")
    print(f"   State prompt: {repr(state_prompt)[:100]}")
    print(f"{'=' * 60}\n")

    # @@@ Load voices from user's enabled decks (deck system)
    voices = database.load_voices_from_user_decks(user_id)
    print(
        f"📚 Loaded {len(voices)} enabled voices from deck system: {list(voices.keys()) if voices else 'None (will use defaults)'}"
    )

    agent = PolyAgent(id="voice-analyzer")

    # Get voices from stateless analyzer
    result = analyze_stateless(
        agent,
        text,
        applied_comments or [],
        voices,
        meta_prompt,
        state_prompt,
        overlapped_phrases or [],
        not_found_phrases or [],
    )

    print(f"✅ Returning {result['new_voices_added']} new voice(s)")

    return {
        "voices": result["voices"],
        "new_voices_added": result["new_voices_added"],
        "status": "completed",
    }


@session_def(
    name="Analyze Echoes",
    description="Find recurring themes and topics in all user notes",
    params={
        "user_id": {"type": "int"},
        "language": {"type": "str"},
    },
    category="Analysis",
)
def analyze_echoes(user_id: int, language: str = "en"):
    """Analyze recurring themes and topics across all notes."""
    notes = _load_all_notes_text(user_id)
    if not notes.strip():
        return {"echoes": []}
    print(f"\n{'=' * 60}")
    print(f"🔄 analyze_echoes() called")
    language_code = normalize_language_code(language)
    print(f"   Language: {language_code}")
    print(f"{'=' * 60}\n")

    agent = PolyAgent(id="echoes-analyzer")

    prompt = f"""Analyze these personal notes and identify recurring themes, topics, or concerns that keep appearing.

Notes:
---
{notes}
---

Find 3-5 echoes (recurring themes) that appear across different entries. For each echo:
- Give it a short title (2-4 words)
- Explain what pattern you see
- Quote 2-3 specific examples from the notes

Format as a JSON array:
[
  {{"title": "...", "description": "...", "examples": ["quote1", "quote2", "quote3"]}},
  ...
]

Return ONLY the JSON array, no other text."""
    prompt += f"\n\n{language_instruction(language_code, 'All titles, descriptions, and examples should use this language. Keep the JSON keys the same.')}"

    result = agent.run(prompt, model=config.ECHO_ANALYSIS_MODEL, cli="no-tools", tracked=True)

    if not result.is_success or not result.content:
        return {"echoes": []}

    try:
        import json

        echoes = json.loads(result.content.strip())
        return {"echoes": echoes}
    except:
        return {"echoes": []}


@session_def(
    name="Analyze Traits",
    description="Identify personality traits and characteristics from user notes",
    params={
        "user_id": {"type": "int"},
        "language": {"type": "str"},
    },
    category="Analysis",
)
def analyze_traits(user_id: int, language: str = "en"):
    """Analyze personality traits from all notes."""
    notes = _load_all_notes_text(user_id)
    if not notes.strip():
        return {"traits": []}
    print(f"\n{'=' * 60}")
    print(f"👤 analyze_traits() called")
    language_code = normalize_language_code(language)
    print(f"   Language: {language_code}")
    print(f"{'=' * 60}\n")

    agent = PolyAgent(id="traits-analyzer")

    prompt = f"""Analyze these personal notes and identify personality traits and characteristics.

Notes:
---
{notes}
---

Identify 4-6 personality traits that are evident from the writing. For each trait:
- Give it a name (1-2 words)
- Rate the strength (1-5)
- Explain why you see this trait with specific examples

Format as a JSON array:
[
  {{"trait": "...", "strength": 4, "evidence": "..."}},
  ...
]

Return ONLY the JSON array, no other text."""
    prompt += f"\n\n{language_instruction(language_code, 'Use this language for trait names, explanations, and evidence (JSON keys stay in English).')}"

    result = agent.run(prompt, model=config.TRAIT_ANALYSIS_MODEL, cli="no-tools", tracked=True)

    if not result.is_success or not result.content:
        return {"traits": []}

    try:
        import json

        traits = json.loads(result.content.strip())
        return {"traits": traits}
    except:
        return {"traits": []}


@session_def(
    name="Analyze Patterns",
    description="Identify behavioral patterns and habits from user notes",
    params={
        "user_id": {"type": "int"},
        "language": {"type": "str"},
    },
    category="Analysis",
)
def analyze_patterns(
    user_id: int, language: str = "en"
):
    """Analyze behavioral patterns from all notes."""
    notes = _load_all_notes_text(user_id)
    if not notes.strip():
        return {"patterns": []}
    print(f"\n{'=' * 60}")
    print(f"🔍 analyze_patterns() called")
    language_code = normalize_language_code(language)
    print(f"   Language: {language_code}")
    print(f"{'=' * 60}\n")

    agent = PolyAgent(id="patterns-analyzer")

    prompt = f"""Analyze these personal notes and identify behavioral patterns or habits.

Notes:
---
{notes}
---

Identify 3-5 behavioral patterns or habits. For each pattern:
- Give it a descriptive name
- Describe the pattern
- Note the frequency/context when it appears

Format as a JSON array:
[
  {{"pattern": "...", "description": "...", "frequency": "..."}},
  ...
]

Return ONLY the JSON array, no other text."""
    prompt += f"\n\n{language_instruction(language_code, 'Use this language for pattern names, descriptions, and frequency notes (JSON keys stay in English).')}"

    result = agent.run(prompt, model=config.PATTERN_ANALYSIS_MODEL, cli="no-tools", tracked=True)

    if not result.is_success or not result.content:
        return {"patterns": []}

    try:
        import json

        patterns = json.loads(result.content.strip())
        return {"patterns": patterns}
    except:
        return {"patterns": []}


def _today_in_tz(tz_name: str) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz_name))
    return now.strftime("%Y-%m-%d")


def _generate_picture_for_date(
    user_id: int,
    target_date: Optional[str] = None,
    *,
    timezone: str = "Asia/Shanghai",
    notes_override: Optional[str] = None,
    skip_if_exists: bool = False,
    dry_run: bool = False,
):
    """
    Shared generator for daily pictures.

    - Extracts notes for the given date (or uses override).
    - Generates description + image.
    - Saves to DB unless dry_run=True.
    """
    target_date = target_date or _today_in_tz(timezone)
    target_date = target_date.strip()

    if skip_if_exists and not dry_run:
        existing = database.get_daily_pictures_range(user_id, target_date, target_date, limit=1)
        if existing:
            return {
                "skipped": True,
                "reason": "already exists",
                "date": target_date,
                "image_base64": existing[0]["base64"],
                "thumbnail_base64": existing[0]["base64"],
                "prompt": existing[0].get("prompt") or "",
            }

    notes = notes_override if notes_override is not None else database.extract_text_from_sessions_on_date(
        user_id, target_date, timezone
    )
    if not (notes or "").strip():
        return {
            "skipped": True,
            "reason": "no notes for date",
            "date": target_date,
            "image_base64": "",
            "thumbnail_base64": "",
            "prompt": "",
        }

    print(f"\n{'=' * 60}")
    print(f"🎨 generate_daily_picture() called")
    print(f"   Target date: {target_date}")
    print(f"{'=' * 60}\n")

    import requests

    # @@@ Fetch recent prompts to avoid duplication
    recent_prompts_text = ""
    try:
        db = database.get_db()
        recent_prompts = db.execute(
            "SELECT prompt FROM daily_pictures WHERE user_id = ? ORDER BY date DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        db.close()

        if recent_prompts:
            recent_prompts_list = [p[0] for p in recent_prompts if p[0]]
            if recent_prompts_list:
                recent_prompts_text = "\n\nPREVIOUS IMAGE DESCRIPTIONS (do NOT repeat these themes/settings/objects):\n"
                for i, prompt in enumerate(recent_prompts_list, 1):
                    recent_prompts_text += f"{i}. {prompt}\n"
                recent_prompts_text += "\n⚠️ IMPORTANT: Create something COMPLETELY DIFFERENT from all previous descriptions above!\n"
                recent_prompts_text += "⚠️ Use different: setting, objects, style, mood, time of day, colors, composition.\n"
                recent_prompts_text += "⚠️ Be creative and avoid repetition!\n"
                print(f"📋 Found {len(recent_prompts_list)} recent prompts to avoid duplication")
    except Exception as e:
        print(f"⚠️ Could not fetch recent prompts: {e}")

    # Step 1: Convert notes to artistic image description using Claude Haiku
    description_prompt = f"""Read these personal notes and create a MINIMAL, SIMPLE image description.

Notes:
---
{notes}
---
{recent_prompts_text}
Create an EXTREMELY SIMPLE image description (1-2 sentences):
- ONE single object or element only (e.g., "a bird", "edge of a house", "a leaf")
- Clean white or simple solid color background
- Simple lines, minimal details
- Soft, gentle colors
- Style: simple line drawing, watercolor, or minimalist illustration

IMPORTANT RULES:
- Maximum 1-2 objects in the entire image
- No complex scenes, no multiple elements
- No detailed backgrounds
- Think: "one bird on white background" or "corner of a window, white space"
- Embrace empty space and simplicity

Examples of GOOD descriptions:
- "A single sparrow perched, simple brushstrokes, white background"
- "Corner of a window frame, soft blue, minimal details"
- "One maple leaf, watercolor, pale background"

Examples of BAD descriptions (TOO COMPLEX):
- "A desk with notebook, tea cup, lamp, and books" ❌
- "A room with furniture and decorations" ❌
- "Multiple objects or detailed scene" ❌

Return ONLY the minimal image description, no other text."""

    print("🧠 Creating image description from notes with Claude Haiku...")

    claude_response = requests.post(
        f"{config.IMAGE_API_ENDPOINT}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.IMAGE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.IMAGE_DESCRIPTION_MODEL,
            "messages": [{"role": "user", "content": description_prompt}],
            "max_tokens": config.IMAGE_DESCRIPTION_MAX_TOKENS,
        },
        timeout=config.IMAGE_DESCRIPTION_TIMEOUT,
    )

    if claude_response.status_code != 200:
        return {"image_base64": None, "error": "Failed to create image description", "date": target_date}

    claude_data = claude_response.json()
    image_description = (
        claude_data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )

    if not image_description:
        return {"image_base64": None, "error": "Failed to create image description", "date": target_date}

    print(f"📝 Image description: {image_description}")

    # Step 2: Generate image from description with retry logic
    url = f"{config.IMAGE_API_ENDPOINT}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.IMAGE_GENERATION_MODEL,
        "messages": [{"role": "user", "content": image_description}],
        "max_tokens": config.IMAGE_MAX_TOKENS,
    }

    # Retry logic with increasing timeouts
    for attempt in range(1, config.IMAGE_RETRY_MAX_ATTEMPTS + 1):
        try:
            timeout_seconds = (
                config.IMAGE_RETRY_BASE_TIMEOUT
                + (attempt - 1) * config.IMAGE_RETRY_TIMEOUT_INCREMENT
            )
            print(f"🎨 Generating image (attempt {attempt}/{config.IMAGE_RETRY_MAX_ATTEMPTS}, timeout={timeout_seconds}s)...")
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )

            if response.status_code != 200:
                print(f"❌ Error: {response.status_code}")
                if attempt < config.IMAGE_RETRY_MAX_ATTEMPTS:
                    print("⏳ Retrying in 2 seconds...")
                    import time

                    time.sleep(2)
                    continue
                return {"image_base64": None, "error": "Image generation failed", "date": target_date}

            data = response.json()

            # Extract image from response
            if "choices" in data and len(data["choices"]) > 0:
                message = data["choices"][0].get("message", {})
                images = message.get("images", [])

                if images:
                    image_data = images[0].get("image_url", {}).get("url", "")

                    if image_data.startswith("data:image/png;base64,"):
                        # Extract base64 data (without the data URI prefix)
                        base64_data = image_data.split(",", 1)[1]

                        # @@@ Convert to JPEG and create thumbnail
                        try:
                            import base64
                            from io import BytesIO
                            from PIL import Image

                            # Decode PNG
                            img_bytes = base64.b64decode(base64_data)
                            img = Image.open(BytesIO(img_bytes))

                            # Convert to RGB (JPEG doesn't support transparency)
                            if img.mode in ("RGBA", "LA", "P"):
                                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                                if img.mode == "RGBA":
                                    rgb_img.paste(img, mask=img.split()[-1])
                                else:
                                    rgb_img.paste(img)
                                img = rgb_img

                            # Full JPEG (quality 85)
                            full_output = BytesIO()
                            img.save(full_output, format="JPEG", quality=85, optimize=True)
                            full_jpeg = base64.b64encode(full_output.getvalue()).decode("utf-8")

                            # Thumbnail JPEG (400px width, quality 60)
                            thumb_width = 400
                            thumb_height = int(img.height * (thumb_width / img.width))
                            thumb_img = img.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)

                            thumb_output = BytesIO()
                            thumb_img.save(thumb_output, format="JPEG", quality=60, optimize=True)
                            thumb_jpeg = base64.b64encode(thumb_output.getvalue()).decode("utf-8")

                            print("✅ Image generated successfully")
                            print(f"   Original PNG: {len(base64_data)} chars")
                            print(f"   Full JPEG: {len(full_jpeg)} chars ({100 * len(full_jpeg) / len(base64_data):.1f}%)")
                            print(f"   Thumbnail: {len(thumb_jpeg)} chars ({100 * len(thumb_jpeg) / len(base64_data):.1f}%)")

                            result = {
                                "image_base64": full_jpeg,
                                "thumbnail_base64": thumb_jpeg,
                                "prompt": image_description,
                                "date": target_date,
                            }
                            if not dry_run:
                                database.save_daily_picture(
                                    user_id=user_id,
                                    date=target_date,
                                    image_base64=full_jpeg,
                                    prompt=image_description,
                                    thumbnail_base64=thumb_jpeg,
                                )
                            return result
                        except Exception as e:
                            print(f"⚠️ JPEG conversion failed: {e}, using original PNG")
                            result = {
                                "image_base64": base64_data,
                                "thumbnail_base64": base64_data,  # Fallback to full image
                                "prompt": image_description,
                                "date": target_date,
                            }
                            if not dry_run:
                                database.save_daily_picture(
                                    user_id=user_id,
                                    date=target_date,
                                    image_base64=base64_data,
                                    prompt=image_description,
                                    thumbnail_base64=base64_data,
                                )
                            return result

            if attempt < config.IMAGE_RETRY_MAX_ATTEMPTS:
                print("⚠️ No image in response, retrying...")
                import time

                time.sleep(2)
                continue
            return {"image_base64": None, "error": "No image in response", "date": target_date}

        except Exception as e:
            print(f"❌ Exception on attempt {attempt}: {e}")
            if attempt < config.IMAGE_RETRY_MAX_ATTEMPTS:
                print("⏳ Retrying in 2 seconds...")
                import time

                time.sleep(2)
                continue
            return {"image_base64": None, "error": str(e), "date": target_date}

    return {"image_base64": None, "error": "All retry attempts failed", "date": target_date}


@session_def(
    name="Generate Daily Picture",
    description="Generate an artistic image based on user's daily notes",
    params={
        "user_id": {"type": "int"},
        "target_date": {"type": "str"},  # Optional: YYYY-MM-DD format
        "notes_override": {"type": "str"},
        "dry_run": {"type": "bool"},
        "skip_if_exists": {"type": "bool"},
    },
    category="Creative",
)
def generate_daily_picture(
    user_id: int,
    target_date: str = None,
    notes_override: str = None,
    dry_run: bool = True,
    skip_if_exists: bool = False,
    timezone: str = "Asia/Shanghai",
):
    """
    Generate an image for a specific date.

    Defaults to dry_run=True so the caller decides whether to persist.
    """
    result = _generate_picture_for_date(
        user_id=user_id,
        target_date=target_date,
        timezone=timezone,
        notes_override=notes_override,
        skip_if_exists=skip_if_exists,
        dry_run=dry_run,
    )
    # PolyCLI sessions should fail loudly if no image produced
    if result.get("skipped"):
        raise ValueError(result.get("reason") or "Generation skipped")
    if not result.get("image_base64"):
        raise ValueError(result.get("error") or "Generation returned no image")
    return result


# ========== FastAPI Application ==========

app = FastAPI(
    title="Ink & Memory API",
    description="Voice analysis and creative generation API",
    version="2.0.0",
)

http_bearer = HTTPBearer(auto_error=False)

print(f"🧾 Backend version: {BACKEND_VERSION}")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== Timeline Auto-Generation Scheduler ==========

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import scheduler as timeline_scheduler

# Create scheduler instance
timeline_gen_scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup_scheduler():
    """Start the timeline auto-generation scheduler on app startup."""
    print("\n" + "=" * 60)
    print("📅 Starting Timeline Auto-Generation Scheduler")
    print("   Schedule: Daily at 00:00 (midnight, Asia/Shanghai timezone)")
    print("   Generates timeline images for previous day")
    print("=" * 60 + "\n")

    # @@@ asyncio.run() creates new event loop for scheduler thread
    timeline_gen_scheduler.add_job(
        lambda: asyncio.run(timeline_scheduler.daily_generation_job()),
        "cron",
        hour=0,
        minute=0,
        timezone="Asia/Shanghai",
        id="daily_timeline_generation",
        name="Generate timeline images for yesterday",
        replace_existing=True,
    )

    timeline_gen_scheduler.start()
    print("✅ Scheduler started - next run at midnight (00:00 Asia/Shanghai)\n")


@app.on_event("shutdown")
async def shutdown_scheduler():
    """Shutdown the scheduler gracefully."""
    print("\n📅 Shutting down timeline scheduler...")
    timeline_gen_scheduler.shutdown(wait=False)
    print("✅ Scheduler shutdown complete\n")


# ========== Claude Agent Factory ==========

from claude_agent import ClaudeAgentThreadFactory

claude_agent_thread_factory = ClaudeAgentThreadFactory()


@app.on_event("startup")
async def startup_claude_agent():
    """Start the Claude Agent session pool sweeper."""
    claude_agent_thread_factory.start()
    print("✅ Claude Agent factory started\n")


@app.on_event("shutdown")
async def shutdown_claude_agent():
    """Gracefully close all Claude Agent sessions."""
    await claude_agent_thread_factory.aclose()
    print("✅ Claude Agent factory closed\n")


# ========== Request/Response Models ==========


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    token: str


class GeneratePictureRequest(BaseModel):
    target_date: Optional[str] = None
    notes_override: Optional[str] = None
    dry_run: bool = False
    skip_if_exists: bool = False
    timezone: Optional[str] = "Asia/Shanghai"


class ImportDataRequest(BaseModel):
    currentSession: Optional[str] = None
    calendarEntries: Optional[str] = None
    dailyPictures: Optional[str] = None
    voiceCustomizations: Optional[str] = None
    metaPrompt: Optional[str] = None
    stateConfig: Optional[str] = None
    selectedState: Optional[str] = None
    analysisReports: Optional[str] = None
    oldDocument: Optional[str] = None

class SessionBatchRequest(BaseModel):
    ids: List[str]


# ========== Auth Dependency ==========


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(http_bearer)) -> dict:
    """
    Dependency to extract and verify JWT token from Authorization header.

    Raises:
        HTTPException 401 if token is missing or invalid
    """
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    user_data = auth.verify_access_token(token)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user_data


# ========== Custom API Endpoints (Clean Interface) ==========


@app.get("/")
def root():
    """Root endpoint"""
    base = PUBLIC_BASE_URL.rstrip("/") + "/"
    return PlainTextResponse(
        f"The server is configured with a public base URL of {base}"
        f" - did you mean to visit {base}api/claude-agent/threads instead?"
    )


# ========== Auth Endpoints ==========


@app.post("/api/register", response_model=TokenResponse)
def register(request: RegisterRequest):
    """
    Register a new user.

    Returns JWT token and user info.
    """
    # Validate input
    if not request.email or not request.password:
        raise HTTPException(status_code=400, detail="Email and password required")

    if len(request.password) < 6:
        raise HTTPException(
            status_code=400, detail="Password must be at least 6 characters"
        )

    # Hash password
    password_hash = auth.hash_password(request.password)

    # Create user
    try:
        user_id = database.create_user(
            request.email, password_hash, request.display_name
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # @@@ Auto-fork all system decks for new user
    database.auto_fork_system_decks(user_id)

    # Generate token
    token = auth.create_access_token(user_id, request.email)

    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": request.email,
            "display_name": request.display_name,
        },
    }


@app.post("/api/login", response_model=TokenResponse)
def login(request: LoginRequest):
    """
    Login with email and password.

    Returns JWT token and user info.
    """
    # Get user by email
    user = database.get_user_by_email(request.email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Verify password
    if not auth.verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # @@@ Auto-fork system decks if user has no decks yet (handles existing users)
    user_decks = database.get_user_decks(user["id"])
    if len(user_decks) == 0:
        database.auto_fork_system_decks(user["id"])

    # Generate token
    token = auth.create_access_token(user["id"], user["email"])

    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
        },
    }


@app.get("/api/me")
def get_current_user_info(current_user: dict = Depends(get_current_user)):
    """
    Get current user info from token.

    Requires Authorization header with Bearer token.
    """
    user = database.get_user_by_id(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "created_at": user["created_at"],
    }


@app.post("/api/import-local-data")
def import_local_data(
    request: ImportDataRequest, current_user: dict = Depends(get_current_user)
):
    """
    Import localStorage data to database on first login.

    Extracts sessions, pictures, preferences, and reports from localStorage export.
    """
    import json

    user_id = current_user["user_id"]

    print(f"\n🔍 Migration request for user {user_id}:")
    print(
        f"  - currentSession: {len(request.currentSession) if request.currentSession else 0} chars"
    )
    print(
        f"  - calendarEntries: {len(request.calendarEntries) if request.calendarEntries else 0} chars"
    )
    print(
        f"  - dailyPictures: {len(request.dailyPictures) if request.dailyPictures else 0} chars"
    )
    print(
        f"  - oldDocument: {len(request.oldDocument) if request.oldDocument else 0} chars"
    )

    # Extract sessions
    sessions = []

    # 1. Current session
    if request.currentSession:
        try:
            current = json.loads(request.currentSession)
            sessions.append(
                {
                    "id": "current-session",
                    "name": "Current Session",
                    "editor_state": current,
                }
            )
            print(f"✅ Imported current session ({len(str(current))} chars)")
        except Exception as e:
            print(f"❌ Failed to parse current session: {e}")
            # Don't silently fail - this is critical data!

    # 2. Calendar entries
    if request.calendarEntries:
        try:
            calendar = json.loads(request.calendarEntries)
            print(f"📅 Parsed calendar with {len(calendar)} dates")
            for date, entries in calendar.items():
                print(f"  - {date}: {len(entries)} entries")
                for entry in entries:
                    sessions.append(
                        {
                            "id": entry["id"],
                            "name": f"{date} - {entry.get('firstLine', 'Untitled')}",
                            "editor_state": entry["state"],
                        }
                    )
        except Exception as e:
            print(f"❌ Failed to parse calendar entries: {e}")
            import traceback

            traceback.print_exc()

    # 3. Old document (if exists)
    if request.oldDocument:
        try:
            old_doc = json.loads(request.oldDocument)
            if old_doc and old_doc.get("document"):
                sessions.append(
                    {
                        "id": "old-document",
                        "name": "Old Document (migrated)",
                        "editor_state": {
                            "cells": [{"type": "text", "content": str(old_doc)}]
                        },
                    }
                )
        except:
            pass

    # Extract pictures
    pictures = []
    if request.dailyPictures:
        try:
            pics = json.loads(request.dailyPictures)
            for pic in pics:
                pictures.append(
                    {
                        "date": pic["date"],
                        "image_base64": pic["base64"],
                        "prompt": pic.get("prompt", ""),
                    }
                )
        except:
            pass

    # Extract preferences
    preferences = {}
    if request.voiceCustomizations:
        try:
            preferences["voice_configs"] = json.loads(request.voiceCustomizations)
        except:
            pass

    if request.metaPrompt:
        preferences["meta_prompt"] = request.metaPrompt

    if request.stateConfig:
        try:
            preferences["state_config"] = json.loads(request.stateConfig)
        except:
            pass

    if request.selectedState:
        preferences["selected_state"] = request.selectedState

    # Extract reports
    reports = []
    if request.analysisReports:
        try:
            report_list = json.loads(request.analysisReports)
            for report in report_list:
                reports.append(
                    {
                        "type": report.get("type", "unknown"),
                        "data": report.get("data", {}),
                        "allNotes": report.get("allNotes", ""),
                        "timestamp": report.get("timestamp", ""),
                    }
                )
        except:
            pass

    # Import to database
    database.import_user_data(user_id, sessions, pictures, preferences, reports)

    return {
        "success": True,
        "imported": {
            "sessions": len(sessions),
            "pictures": len(pictures),
            "preferences": len([k for k, v in preferences.items() if v]),
            "reports": len(reports),
        },
    }


# ========== Session Storage Endpoints ==========


@app.post("/api/sessions")
def save_session(request: dict, current_user: dict = Depends(get_current_user)):
    """
    Save or update a session.

    Request body:
    {
        "session_id": "string",
        "name": "optional string",
        "editor_state": {...}
    }
    """
    user_id = current_user["user_id"]
    session_id = request.get("session_id")
    editor_state = request.get("editor_state")
    name = request.get("name")

    if not session_id or not editor_state:
        raise HTTPException(
            status_code=400, detail="session_id and editor_state required"
        )

    database.save_session(user_id, session_id, editor_state, name)

    return {"success": True}


@app.post("/api/import-calendar-recovery")
def import_calendar_recovery(
    request: dict, current_user: dict = Depends(get_current_user)
):
    """
    Recovery endpoint to import calendar entries that were missed in initial migration.

    Request body:
    {
        "calendarEntries": "{\"2025-11-01\": [...]}"  # JSON string
    }
    """
    import json

    user_id = current_user["user_id"]
    calendar_json = request.get("calendarEntries")

    if not calendar_json:
        raise HTTPException(status_code=400, detail="calendarEntries required")

    sessions = []
    try:
        calendar = json.loads(calendar_json)
        print(f"📅 Recovery import: {len(calendar)} dates")
        for date, entries in calendar.items():
            print(f"  - {date}: {len(entries)} entries")
            for entry in entries:
                sessions.append(
                    {
                        "id": entry["id"],
                        "name": f"{date} - {entry.get('firstLine', 'Untitled')}",
                        "editor_state": entry["state"],
                    }
                )
    except Exception as e:
        print(f"❌ Failed to parse calendar: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=400, detail=f"Failed to parse calendar: {str(e)}"
        )

    # Import to database
    database.import_user_data(user_id, sessions, [], {}, [])

    return {"success": True, "imported": {"sessions": len(sessions)}}


def _clean_timestamp(ts_raw: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO parser for timestamps stored in DB."""
    if not ts_raw:
        return None
    try:
        cleaned = ts_raw.replace("Z", "+00:00")
        if "T" not in cleaned and " " in cleaned:
            cleaned = cleaned.replace(" ", "T")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


@app.get("/api/sessions")
def list_sessions(timezone: str = "Asia/Shanghai", current_user: dict = Depends(get_current_user)):
    """
    List all sessions for current user.
    Returns: Array of session metadata (without full editor state) plus local day key + first line.
    """
    return list_sessions_with_range(None, None, timezone, current_user)

def _validate_date_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")
    if len(value) != 10:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")
    return value

@app.get("/api/sessions/range")
def list_sessions_with_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    timezone: str = "Asia/Shanghai",
    current_user: dict = Depends(get_current_user)
):
    """
    List sessions within an optional date range.
    """
    user_id = current_user["user_id"]
    start_date = _validate_date_str(start_date)
    end_date = _validate_date_str(end_date)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    if start_date or end_date:
        sessions = database.list_sessions_in_range(user_id, start_date, end_date)
    else:
        sessions = database.list_sessions(user_id)

    enriched = []
    for s in sessions:
        dt = _clean_timestamp(s.get("created_at") or s.get("updated_at"))
        date_key = dt.astimezone(tz).strftime("%Y-%m-%d") if dt else None
        enriched.append({**s, "date_key": date_key})

    return {"sessions": enriched}

@app.post("/api/sessions/batch")
def get_sessions_batch(payload: SessionBatchRequest, current_user: dict = Depends(get_current_user)):
    """
    Fetch multiple sessions (with editor_state) in a single request.
    """
    user_id = current_user["user_id"]
    session_ids = payload.ids or []

    if not session_ids:
        return {"sessions": []}

    sessions = database.get_sessions_batch(user_id, session_ids)
    return {"sessions": sessions}

@app.get("/api/sessions/aggregate")
def get_sessions_aggregate(timezone: str = "Asia/Shanghai", current_user: dict = Depends(get_current_user)):
    """
    Aggregate stats across all sessions for the user.
    Returns stats only (no concatenated text) and per-session summaries.
    """
    user_id = current_user["user_id"]
    sessions = database.get_all_sessions_with_text(user_id)

    total_entries = 0
    total_words = 0
    days = set()

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    for s in sessions:
        text = s.get("text", "") or ""
        if text.strip():
            total_entries += 1
            total_words += _count_mixed_words(text)
        # derive local date from updated_at or created_at
        ts_raw = s.get("updated_at") or s.get("created_at")
        if ts_raw:
            try:
                cleaned = ts_raw.replace("Z", "+00:00")
                if "T" not in cleaned and " " in cleaned:
                    cleaned = cleaned.replace(" ", "T")
                dt = datetime.fromisoformat(cleaned)
                local_dt = dt.astimezone(tz)
                days.add(local_dt.strftime("%Y-%m-%d"))
            except Exception:
                continue

    stats = {
        "total_days": len(days),
        "total_entries": total_entries,
        "total_words": total_words,
    }

    # Strip text from response; include length hint only
    summaries = [
        {
            "id": s["id"],
            "name": s.get("name"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "has_text": bool((s.get("text") or "").strip()),
            "word_count": len((s.get("text") or "").split()) if s.get("text") else 0,
        }
        for s in sessions
    ]

    return {
        "stats": stats,
        "sessions": summaries,
        "timezone": timezone,
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, current_user: dict = Depends(get_current_user)):
    """
    Get a specific session by ID.

    Returns: Full session including editor_state
    """
    user_id = current_user["user_id"]
    session = database.get_session(user_id, session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session




@app.delete("/api/sessions/{session_id}")
def delete_session_endpoint(
    session_id: str, current_user: dict = Depends(get_current_user)
):
    """Delete a session."""
    user_id = current_user["user_id"]
    database.delete_session(user_id, session_id)
    return {"success": True}


# ========== Pictures Endpoints ==========


@app.get("/api/pictures")
def get_pictures(limit: int = 30, current_user: dict = Depends(get_current_user)):
    """
    Get recent daily pictures for current user (thumbnails only for fast loading).

    Query params:
    - limit: Max number of pictures to return (default 30)
    """
    user_id = current_user["user_id"]
    pictures = database.get_daily_pictures(user_id, limit)
    return {"pictures": pictures}


@app.post("/api/pictures/generate")
def generate_picture_endpoint(
    request: GeneratePictureRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Generate a picture for a specific date.
    - If dry_run=True, returns the image but does not save.
    - If skip_if_exists=True, returns existing image without regenerating.
    """
    user_id = current_user["user_id"]
    tz = request.timezone or "Asia/Shanghai"
    result = _generate_picture_for_date(
        user_id=user_id,
        target_date=request.target_date,
        timezone=tz,
        notes_override=request.notes_override,
        skip_if_exists=request.skip_if_exists,
        dry_run=request.dry_run,
    )
    return result

@app.get("/api/pictures/range")
def get_pictures_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """
    Get daily pictures within an optional date range.
    """
    user_id = current_user["user_id"]
    start_date = _validate_date_str(start_date)
    end_date = _validate_date_str(end_date)
    pictures = database.get_daily_pictures_range(user_id, start_date, end_date, limit)
    return {"pictures": pictures}


@app.get("/api/pictures/{date}/full")
def get_picture_full(date: str, current_user: dict = Depends(get_current_user)):
    """
    Get full resolution image for a specific date (on-demand loading).

    Path params:
    - date: Date in YYYY-MM-DD format
    """
    user_id = current_user["user_id"]
    full_image = database.get_daily_picture_full(user_id, date)

    if not full_image:
        raise HTTPException(status_code=404, detail="Picture not found for this date")

    return {"image_base64": full_image}


@app.get("/api/friends/{friend_id}/pictures/{date}/full")
def get_friend_picture_full_endpoint(
    friend_id: int, date: str, current_user: dict = Depends(get_current_user)
):
    """Get full resolution image for a friend's specific date (only if users are friends)."""
    user_id = current_user["user_id"]
    full_image = database.get_friend_picture_full(user_id, friend_id, date)

    if not full_image:
        raise HTTPException(
            status_code=404, detail="Picture not found or not accessible"
        )

    return {"image_base64": full_image}


@app.post("/api/pictures")
def save_picture(request: dict, current_user: dict = Depends(get_current_user)):
    """
    Save a daily picture.

    Request body:
    {
        "date": "YYYY-MM-DD",
        "image_base64": "base64 string",
        "prompt": "optional prompt"
    }
    """
    user_id = current_user["user_id"]
    date = request.get("date")
    image_base64 = request.get("image_base64")
    thumbnail_base64 = request.get("thumbnail_base64")
    prompt = request.get("prompt", "")

    if not date or not image_base64:
        raise HTTPException(status_code=400, detail="date and image_base64 required")

    database.save_daily_picture(user_id, date, image_base64, prompt, thumbnail_base64)
    return {"success": True}


# ========== Preferences Endpoints ==========


@app.get("/api/preferences")
def get_preferences(current_user: dict = Depends(get_current_user)):
    """Get user preferences."""
    user_id = current_user["user_id"]
    preferences = database.get_preferences(user_id)
    return preferences or {}


@app.post("/api/preferences")
def save_preferences_endpoint(
    request: dict, current_user: dict = Depends(get_current_user)
):
    """
    Save user preferences.

    Request body can contain any of:
    - voice_configs: dict
    - meta_prompt: str
    - state_config: dict
    - selected_state: str
    """
    user_id = current_user["user_id"]

    database.save_preferences(
        user_id,
        voice_configs=request.get("voice_configs"),
        meta_prompt=request.get("meta_prompt"),
        state_config=request.get("state_config"),
        selected_state=request.get("selected_state"),
        timezone=request.get("timezone"),
    )

    return {"success": True}


# @@@ Removed /api/suggest wrapper - frontend now calls /polycli/api/trigger-sync directly


@app.post("/api/mark-first-login-completed")
def mark_first_login_completed(current_user: dict = Depends(get_current_user)):
    """
    Mark user's first login as completed.
    Called after migration dialog is shown (migrate or skip).
    """
    user_id = current_user["user_id"]
    database.set_first_login_completed(user_id)
    return {"success": True}


# ========== Analysis Reports Endpoints ==========


@app.get("/api/reports")
def get_reports(limit: int = 10, current_user: dict = Depends(get_current_user)):
    """Get recent analysis reports."""
    user_id = current_user["user_id"]
    reports = database.get_analysis_reports(user_id, limit)
    return {"reports": reports}


@app.post("/api/reports")
def save_report(request: dict, current_user: dict = Depends(get_current_user)):
    """
    Save an analysis report.

    Request body:
    {
        "report_type": "echoes" | "traits" | "patterns",
        "report_data": {...},
        "all_notes_text": "optional text"
    }
    """
    user_id = current_user["user_id"]
    report_type = request.get("report_type")
    report_data = request.get("report_data")
    all_notes_text = request.get("all_notes_text", "")

    if not report_type or not report_data:
        raise HTTPException(
            status_code=400, detail="report_type and report_data required"
        )

    database.save_analysis_report(user_id, report_type, report_data, all_notes_text)
    return {"success": True}


@app.get("/api/default-voices")
def get_default_voices():
    """Get default voice configurations"""
    return config.VOICE_ARCHETYPES


@app.post("/api/admin/trigger-timeline-generation")
async def trigger_timeline_generation(
    date: str = None, timezone: str = "Asia/Shanghai"
):
    """
    Manually trigger timeline image generation for a specific date (testing/admin).

    Args:
        date: Target date in YYYY-MM-DD format (defaults to yesterday)
        timezone: Timezone name (default: Asia/Shanghai)

    Returns:
        Generation statistics: total, success, failed, skipped
    """
    if date is None:
        date = timeline_scheduler.get_previous_day(timezone)

    print(f"🔧 Manual trigger: Generating timeline images for {date}")

    try:
        result = await timeline_scheduler.generate_timeline_images_for_date(
            date, timezone
        )
        return {"success": True, "date": date, "timezone": timezone, **result}
    except Exception as e:
        import traceback

        traceback.print_exc()
        return {"success": False, "error": str(e), "date": date, "timezone": timezone}


# ========== Deck & Voice Management ==========


class DeckCreateRequest(BaseModel):
    name: str
    description: str = None
    name_zh: str = None
    name_en: str = None
    description_zh: str = None
    description_en: str = None
    icon: str = None
    color: str = None


class DeckUpdateRequest(BaseModel):
    name: str = None
    description: str = None
    name_zh: str = None
    name_en: str = None
    description_zh: str = None
    description_en: str = None
    icon: str = None
    color: str = None
    enabled: bool = None
    order_index: int = None


class VoiceCreateRequest(BaseModel):
    deck_id: str
    name: str
    system_prompt: str
    name_zh: str = None
    name_en: str = None
    icon: str = None
    color: str = None


class VoiceUpdateRequest(BaseModel):
    name: str = None
    system_prompt: str = None
    name_zh: str = None
    name_en: str = None
    icon: str = None
    color: str = None
    enabled: bool = None
    order_index: int = None


class VoiceForkRequest(BaseModel):
    target_deck_id: str


# ========== Friend System Models ==========


class UseInviteCodeRequest(BaseModel):
    code: str


class FriendRequestActionRequest(BaseModel):
    pass  # No body needed, just request_id in URL


@app.get("/api/decks")
def list_decks(published: bool = False, current_user: dict = Depends(get_current_user)):
    """Get decks - either user's own or published community decks"""
    if published:
        # Get all published decks (community store)
        decks = database.get_published_decks()
    else:
        # Get user's own decks
        user_id = current_user["user_id"]
        decks = database.get_user_decks(user_id)
    return {"decks": decks}


@app.get("/api/decks/{deck_id}")
def get_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    """Get deck with all voices"""
    user_id = current_user["user_id"]
    deck = database.get_deck_with_voices(user_id, deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    return deck


@app.post("/api/decks")
def create_deck(
    request: DeckCreateRequest, current_user: dict = Depends(get_current_user)
):
    """Create a new user deck"""
    user_id = current_user["user_id"]
    deck_id = database.create_deck(
        user_id,
        name=request.name,
        description=request.description,
        name_zh=request.name_zh,
        name_en=request.name_en,
        description_zh=request.description_zh,
        description_en=request.description_en,
        icon=request.icon,
        color=request.color,
    )
    return {"deck_id": deck_id}


@app.put("/api/decks/{deck_id}")
def update_deck(
    deck_id: str,
    request: DeckUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update a user deck"""
    user_id = current_user["user_id"]

    # Convert request to dict, exclude None values
    updates = {k: v for k, v in request.dict().items() if v is not None}

    success = database.update_deck(user_id, deck_id, updates)
    if not success:
        raise HTTPException(
            status_code=404, detail="Deck not found or permission denied"
        )
    return {"success": True}


@app.delete("/api/decks/{deck_id}")
def delete_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a user deck (cascades to voices)"""
    user_id = current_user["user_id"]
    success = database.delete_deck(user_id, deck_id)
    if not success:
        raise HTTPException(
            status_code=404, detail="Deck not found or permission denied"
        )
    return {"success": True}


@app.post("/api/decks/{deck_id}/fork")
def fork_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    """Fork a deck (system or published community deck) to create user's own copy"""
    user_id = current_user["user_id"]
    try:
        new_deck_id = database.fork_deck(user_id, deck_id)
        # @@@ Increment install count if forking from published deck
        database.increment_deck_install_count(deck_id)
        return {"deck_id": new_deck_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/decks/{deck_id}/publish")
def publish_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    """
    Publish/unpublish a deck to community store.
    @@@ Warning: Publishing breaks parent_id chain (deck becomes standalone)
    """
    user_id = current_user["user_id"]
    try:
        # Check if deck is currently published
        deck = database.get_deck_with_voices(user_id, deck_id)
        if not deck:
            raise HTTPException(
                status_code=404, detail="Deck not found or not owned by user"
            )

        # Toggle published status
        if deck.get("published"):
            database.unpublish_deck(deck_id, user_id)
            return {"success": True, "published": False}
        else:
            database.publish_deck(deck_id, user_id)
            return {"success": True, "published": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/decks/{deck_id}/sync")
def sync_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    """Sync user's forked deck with parent template (force overwrites local changes)"""
    user_id = current_user["user_id"]
    try:
        result = database.sync_deck_with_parent(user_id, deck_id, force=True)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/voices")
def create_voice(
    request: VoiceCreateRequest, current_user: dict = Depends(get_current_user)
):
    """Create a new voice in a user deck"""
    user_id = current_user["user_id"]
    try:
        voice_id = database.create_voice(
            user_id,
            deck_id=request.deck_id,
            name=request.name,
            system_prompt=request.system_prompt,
            name_zh=request.name_zh,
            name_en=request.name_en,
            icon=request.icon,
            color=request.color,
        )
        return {"voice_id": voice_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/voices/{voice_id}")
def update_voice(
    voice_id: str,
    request: VoiceUpdateRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update a user voice"""
    user_id = current_user["user_id"]

    # Convert request to dict, exclude None values
    updates = {k: v for k, v in request.dict().items() if v is not None}

    success = database.update_voice(user_id, voice_id, updates)
    if not success:
        raise HTTPException(
            status_code=404, detail="Voice not found or permission denied"
        )
    return {"success": True}


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a user voice"""
    user_id = current_user["user_id"]
    success = database.delete_voice(user_id, voice_id)
    if not success:
        raise HTTPException(
            status_code=404, detail="Voice not found or permission denied"
        )
    return {"success": True}


@app.post("/api/voices/{voice_id}/fork")
def fork_voice(
    voice_id: str,
    request: VoiceForkRequest,
    current_user: dict = Depends(get_current_user),
):
    """Fork a voice to a user deck"""
    user_id = current_user["user_id"]
    try:
        new_voice_id = database.fork_voice(user_id, voice_id, request.target_deck_id)
        return {"voice_id": new_voice_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ========== Friend System Endpoints ==========


@app.post("/api/friends/invite/generate")
def generate_friend_invite(current_user: dict = Depends(get_current_user)):
    """Generate a new friend invite code (6 chars, 7 days validity)"""
    user_id = current_user["user_id"]
    result = database.generate_invite_code(user_id)
    return result


@app.post("/api/friends/invite/use")
def use_friend_invite(
    request: UseInviteCodeRequest, current_user: dict = Depends(get_current_user)
):
    """Use an invite code to send a friend request"""
    user_id = current_user["user_id"]
    result = database.use_invite_code(request.code, user_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/api/friends/requests")
def get_friend_requests(current_user: dict = Depends(get_current_user)):
    """Get all pending friend requests for current user"""
    user_id = current_user["user_id"]
    requests = database.get_friend_requests(user_id)
    return {"requests": requests}


@app.post("/api/friends/requests/{request_id}/accept")
def accept_friend_request(
    request_id: int, current_user: dict = Depends(get_current_user)
):
    """Accept a friend request"""
    user_id = current_user["user_id"]
    result = database.accept_friend_request(request_id, user_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/friends/requests/{request_id}/reject")
def reject_friend_request(
    request_id: int, current_user: dict = Depends(get_current_user)
):
    """Reject a friend request"""
    user_id = current_user["user_id"]
    result = database.reject_friend_request(request_id, user_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/api/friends")
def get_friends(current_user: dict = Depends(get_current_user)):
    """Get all accepted friends for current user"""
    user_id = current_user["user_id"]
    friends = database.get_friends(user_id)
    return {"friends": friends}


@app.delete("/api/friends/{friend_id}")
def remove_friend(friend_id: int, current_user: dict = Depends(get_current_user)):
    """Remove a friend"""
    user_id = current_user["user_id"]
    result = database.remove_friend(user_id, friend_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/api/friends/{friend_id}/timeline")
def get_friend_timeline(
    friend_id: int, limit: int = 30, current_user: dict = Depends(get_current_user)
):
    """Get a friend's timeline pictures (only if friends)"""
    user_id = current_user["user_id"]
    timeline = database.get_friend_timeline(user_id, friend_id, limit)
    if timeline is None:
        raise HTTPException(status_code=403, detail="Not friends or friend not found")
    return {"pictures": timeline}


# ========== Claude Agent Routes ==========
# Isolated from the PolyCLI agent sessions; no cross-module state.

from fastapi.responses import StreamingResponse
from claude_agent import ClaudeAgentRunRequest


def _extract_message_text(message: Any) -> str:
    """Extract plain text from a message value.

    Accepts either a plain ``str`` or a Vercel AI SDK ``UIMessage`` dict
    (has ``parts`` list with ``{type: 'text', text: '...'}`` entries).
    """
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        parts = message.get("parts") or []
        texts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = " ".join(t for t in texts if t).strip()
        # Fallback: try plain content field
        if not text:
            text = str(message.get("content") or "").strip()
        return text
    return str(message) if message else ""


class ClaudeAgentRequestBody(BaseModel):
    thread_id: Optional[str] = None
    id: Optional[str] = None  # alias sent by the Vercel AI SDK (maps to thread_id)
    message: Any  # str or UIMessage dict from Vercel AI SDK
    resume: bool = False
    tool_choice: str = "auto"
    chatModel: Optional[dict] = None
    model: Optional[str] = None
    max_turns: int = 100
    cwd: Optional[str] = None

    def get_thread_id(self) -> Optional[str]:
        return self.thread_id or self.id

    def get_message_text(self) -> str:
        return _extract_message_text(self.message)

    def get_message_id(self) -> Optional[str]:
        """Return the frontend-assigned message id from a UIMessage dict, if present."""
        if isinstance(self.message, dict):
            msg_id = self.message.get("id")
            return str(msg_id) if msg_id else None
        return None


class ToolConfirmRequestBody(BaseModel):
    thread_id: str
    tool_call_id: str
    approved: bool
    reason: Optional[str] = None
    answers: Optional[dict] = None


@app.post("/api/claude-agent")
async def claude_agent_stream(
    body: ClaudeAgentRequestBody,
    current_user: dict = Depends(get_current_user),
):
    """SSE streaming endpoint for Claude Agent.

    Returns ``text/event-stream``; each frame is a JSON object:
    ``{"type": "text-delta"|"tool-event"|"message-final"|"finish"|"error", ...}``

    Requires a ``thread_id`` (created via ``POST /api/claude-agent/threads``).
    """
    user_id = current_user["user_id"]
    thread_id = body.get_thread_id()
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")

    # Validate thread belongs to the authenticated user
    thread = database.get_chat_thread(thread_id, user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    message_text = body.get_message_text()
    if not message_text:
        raise HTTPException(status_code=400, detail="message text is required")

    request = ClaudeAgentRunRequest(
        user_id=str(user_id),
        thread_id=thread_id,
        message=message_text,
        message_id=body.get_message_id(),
        resume=body.resume,
        tool_choice=body.tool_choice,
        model=body.model,
        max_turns=body.max_turns,
        cwd=body.cwd,
    )

    async def generate():
        async for frame in claude_agent_thread_factory.run_streaming(request):
            yield frame

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/claude-agent/chat-history")
async def claude_agent_chat_history(
    current_user: dict = Depends(get_current_user),
):
    """Return chat thread history for the authenticated user.

    Returns the list of chat threads (newest first) so the frontend
    can display the user's past conversations.
    """
    user_id = current_user["user_id"]
    threads = database.list_chat_threads(user_id)
    return {"threads": threads or []}


# ========== Claude Agent Thread Management ==========


class CreateThreadResponseBody(BaseModel):
    thread_id: str


@app.post("/api/claude-agent/threads", response_model=CreateThreadResponseBody)
async def claude_agent_create_thread(
    current_user: dict = Depends(get_current_user),
):
    """Create a new chat thread and return its ``thread_id``.

    Call this endpoint when the user clicks "New Chat".  The returned
    ``thread_id`` must be included in every subsequent
    ``POST /api/claude-agent`` request for that conversation.
    """
    user_id = current_user["user_id"]
    thread_id = database.create_chat_thread(user_id)
    return {"thread_id": thread_id}


@app.get("/api/claude-agent/threads")
async def claude_agent_list_threads(
    current_user: dict = Depends(get_current_user),
):
    """Return all chat threads for the authenticated user, newest first."""
    user_id = current_user["user_id"]
    threads = database.list_chat_threads(user_id)
    return {"threads": threads}


@app.get("/api/claude-agent/threads/{thread_id}/messages")
async def claude_agent_thread_messages(
    thread_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Return all persisted messages for *thread_id* in chronological order.

    Returns 404 if the thread does not exist or belongs to another user.
    """
    user_id = current_user["user_id"]
    thread = database.get_chat_thread(thread_id, user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = database.list_chat_messages(thread_id)
    return {"thread": thread, "messages": messages}


@app.delete("/api/claude-agent/threads/{thread_id}")
async def claude_agent_delete_thread(
    thread_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a chat thread and all its messages."""
    user_id = current_user["user_id"]
    deleted = database.delete_chat_thread(thread_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Also close the in-memory session if it is still alive
    claude_agent_thread_factory.close_thread(thread_id)
    return {"ok": True}


@app.post("/api/claude-agent/message-latency")
async def claude_agent_message_latency(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """Record browser-side latency metrics for a Claude Agent message.

    Stored as extra metadata on the session record when available;
    silently ignored if the referenced session is not found.
    """
    # Lightweight pass-through: latency data is logged but not yet persisted.
    import logging as _logging
    _logging.getLogger("claude_agent.latency").info(
        "message-latency user_id=%s data=%s",
        current_user.get("user_id"),
        body,
    )
    return {"ok": True}


@app.get("/api/claude-agent/session")
async def claude_agent_session_status(
    session_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Return the keepalive snapshot for the caller's active session.

    *session_id* must be a valid ``thread_id``.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id (thread_id) is required")
    snapshot = claude_agent_thread_factory.session_snapshot(session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No active session found")
    return snapshot


@app.delete("/api/claude-agent/session")
async def claude_agent_session_close(
    session_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Explicitly close (destroy) the caller's Claude Agent session.

    Triggers Phase 4 lifecycle hooks; the next request will start a fresh session.
    *session_id* must be a valid ``thread_id``.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id (thread_id) is required")
    claude_agent_thread_factory.close_thread(session_id)
    return {"ok": True, "session_id": session_id}


@app.post("/api/claude-agent/tool-confirm")
async def claude_agent_tool_confirm(
    body: ToolConfirmRequestBody,
    current_user: dict = Depends(get_current_user),
):
    """Resolve a pending tool confirmation from the frontend.

    Must be called while the SSE stream is still open and the agent is
    awaiting approval in its ``on_tool_confirmation_request`` callback.
    ``body.thread_id`` must be the ``thread_id`` of the active conversation.
    """
    session_id = body.thread_id
    if not session_id:
        raise HTTPException(status_code=400, detail="thread_id is required")
    resolved = claude_agent_thread_factory.confirm_tool(
        session_id=session_id,
        tool_call_id=body.tool_call_id,
        approved=body.approved,
        reason=body.reason,
        answers=body.answers,
    )
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail=f"No pending confirmation for tool_call_id={body.tool_call_id}",
        )
    return {"ok": True, "approved": body.approved}


@app.websocket("/ws/speech-recognition")
async def speech_recognition(websocket: WebSocket):
    # TODO: find a way of authentication for websocket
    await websocket.accept()
    await init_speech_recognition(websocket)


registry = get_registry()
# @@@ Pass auth_callback to enable authentication for /polycli/api/trigger-sync
mount_control_panel(
    app, registry, prefix="/polycli", auth_callback=auth.verify_access_token
)

# ========== Main ==========

if __name__ == "__main__":
    import uvicorn

    print("\n" + "=" * 60)
    print("🎭 Ink & Memory FastAPI Server")
    print(f"🧾 Version: {BACKEND_VERSION}")
    print("=" * 60)
    print("\n📚 API Endpoints:")
    print("  Auth & User:")
    print("    POST /api/register        - Register new user")
    print("    POST /api/login           - Login")
    print("    GET  /api/me              - Get current user")
    print("  Data Storage:")
    print("    POST /api/sessions        - Save session")
    print("    GET  /api/sessions        - List sessions")
    print("    GET  /api/sessions/{id}   - Get session")
    print("    DELETE /api/sessions/{id} - Delete session")
    print("    POST /api/pictures        - Save daily picture")
    print("    GET  /api/pictures        - List pictures")
    print("    GET  /api/pictures/{date}/full - Get full picture by date")
    print("    GET  /api/preferences     - Get user preferences")
    print("    POST /api/preferences     - Save preferences")
    print("    GET  /api/reports         - Get analysis reports")
    print("    POST /api/reports         - Save report")
    print("  Configuration:")
    print("    GET  /api/default-voices  - Get default voice configs")
    print("  Deck & Voice Management:")
    print("    GET  /api/decks           - List all decks")
    print("    GET  /api/decks/{id}      - Get deck with voices")
    print("    POST /api/decks           - Create deck")
    print("    PUT  /api/decks/{id}      - Update deck")
    print("    DELETE /api/decks/{id}    - Delete deck")
    print("    POST /api/decks/{id}/fork - Fork deck")
    print("    POST /api/voices          - Create voice")
    print("    PUT  /api/voices/{id}     - Update voice")
    print("    DELETE /api/voices/{id}   - Delete voice")
    print("    POST /api/voices/{id}/fork - Fork voice")
    print("  Friend System:")
    print("    POST /api/friends/invite/generate - Generate invite code")
    print("    POST /api/friends/invite/use      - Use invite code")
    print("    GET  /api/friends/requests        - Get friend requests")
    print("    POST /api/friends/requests/{id}/accept - Accept request")
    print("    POST /api/friends/requests/{id}/reject - Reject request")
    print("    GET  /api/friends                 - Get friends list")
    print("    DELETE /api/friends/{id}          - Remove friend")
    print("    GET  /api/friends/{id}/timeline   - Get friend's timeline")
    print("    GET  /api/friends/{id}/pictures/{date}/full - Get friend's full picture")
    print("\n  Claude Agent:")
    print("    POST /api/claude-agent                 - Stream agent response (SSE)")
    print("    GET  /api/claude-agent/chat-history    - Get recent sessions for context")
    print("    POST /api/claude-agent/message-latency - Record message latency metrics")
    print("    GET  /api/claude-agent/session         - Get active session snapshot")
    print("    DELETE /api/claude-agent/session       - Close active session")
    print("    POST /api/claude-agent/tool-confirm    - Resolve pending tool confirmation")
    print("\n  PolyCLI (AI Functions):")
    print("    /polycli                  - Control panel UI")
    print("    /polycli/api/trigger-sync - Direct sync API")
    print("       Sessions: analyze_text, chat_with_voice,")
    print("                 get_writing_suggestion, analyze_echoes,")
    print("                 analyze_traits, analyze_patterns,")
    print("                 generate_daily_picture")
    print("\n  Documentation:")
    print("    /docs                     - Auto-generated API docs")
    print("=" * 60 + "\n")

    uvicorn.run(app, host="127.0.0.1", port=8765)
