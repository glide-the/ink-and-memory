# [Input] Consume libs/claude_agent_kit/types.py, libs/claude_agent_kit/runner.py,
#         claude_agent/context_builder.py, claude_agent/tool_confirmation_store.py.
#         Reads database module for session persistence.
# [Output] Provide ClaudeAgentRunRequest, ClaudeAgentService to thread_factory.py.
# [Pos] core-business node in backend/claude_agent
# [Sync] 2026-05-22: adapted from Pawkeyland application/claude_agent/service.py.
#                    Removed: pet/persona/mem0/sticker_filter/IdentityService.
#                    Session context provided by ClaudeAgentContextBuilder.
# [Sync] 2026-05-24: align SSE frame format with Pawkeyland protocol:
#                    text-delta.delta (was .text), text-start/end (was text-done),
#                    tool-input-start + tool-input-available + tool-output-available
#                    (was unified tool-event), error.errorText (was .message),
#                    finish.finishReason (was .reason).
# [Sync] 2026-05-24: migrate on_tool_event to Pawkeyland event.type dispatch:
#                    registered_tool_call_ids + emitted_tool_input_ids dedup sets added
#                    to _TurnContext; handles tool_use/tool_use_start, tool_input_available,
#                    tool_result with defensive auto-register fallback.
# [Sync] 2026-05-24: enable thinking mode — migrate thinking_delta/thinking/content_block_stop
#                    branches from Pawkeyland on_tool_event; _TurnContext gains
#                    current_reasoning_id/has_thinking_delta/completed_streamed_reasoning_texts;
#                    emits reasoning-start/delta/end SSE frames.

"""Claude Agent Service — core business logic for Ink & Memory.

Responsibilities:
- ``assemble_context``: Phase 1 — build system prompt + run options for the turn.
- ``execute_session``: Phase 3 — stream the agent turn, emit SSE events, persist (optional).

SSE event schema (aligned with Pawkeyland)::

    data: {"type": "message-metadata", "sessionId": "...", "turnIndex": 0}
    data: {"type": "text-start",     "id": "..."}
    data: {"type": "text-delta",     "id": "...", "delta": "..."}
    data: {"type": "text-end",       "id": "..."}
    data: {"type": "tool-input-start",     "toolCallId": "...", "toolName": "..."}
    data: {"type": "tool-input-available", "toolCallId": "...", "toolName": "...", "input": {...}}
    data: {"type": "tool-output-available","toolCallId": "...", "output": ..., "isError": false}
    data: {"type": "tool-approval-request","toolCallId": "...", "toolName": "...", "input": {...}}
    data: {"type": "message-final",  "text": "...", "usage": {...}, "sessionId": "..."}
    data: {"type": "finish",         "finishReason": "stop"|"error"}
    data: {"type": "error",          "errorText": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

from claude_agent.context_builder import ClaudeAgentContextBuilder
from libs.claude_agent_kit.server.agent_runner import ClaudeAgentRunner
from claude_agent.thread_pool import AgentRunState
from claude_agent.tool_confirmation_store import ToolConfirmationResult, ToolConfirmationStore
from libs.claude_agent_kit.types import AgentRunOptions, AgentStreamingCallbacks, ToolEventPayload

logger = logging.getLogger(__name__)

# Keepalive interval for SSE comments (seconds).
_SSE_KEEPALIVE_S: float = float(os.getenv("INK_AGENT_SSE_KEEPALIVE_S", "15") or "15")

# Maximum characters to use when auto-titling a thread from the first user message.
MAX_THREAD_TITLE_LENGTH: int = 50


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


@dataclass
class ClaudeAgentRunRequest:
    """Validated request for a single Claude Agent turn.

    All string IDs are validated by the factory before this dataclass is built.
    """

    user_id: str
    thread_id: str
    message: str
    message_id: Optional[str] = None  # frontend-assigned UIMessage id
    resume: bool = False
    tool_choice: str = "auto"
    model: Optional[str] = None
    max_turns: int = int(os.getenv("INK_AGENT_MAX_TURNS", "100") or "100")
    cwd: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Turn context (extrinsic state bundle)
# ---------------------------------------------------------------------------


_TEXT_PART_ID = "text-0"


@dataclass
class _TurnContext:
    """Mutable state bundle for a single agent turn."""

    queue: asyncio.Queue
    confirmation_store: ToolConfirmationStore
    pending_tool_call_ids: set = field(default_factory=set)
    full_text_accumulator: list[str] = field(default_factory=list)
    turn_start_ts: float = field(default_factory=time.monotonic)
    # Tracks whether text-start has been emitted for the current text block
    text_started: bool = False
    # Dedup sets for tool events (mirrors Pawkeyland registered/emitted_tool_input_ids)
    registered_tool_call_ids: set = field(default_factory=set)
    emitted_tool_input_ids: set = field(default_factory=set)
    # Thinking / reasoning tracking (mirrors Pawkeyland thinking state)
    current_reasoning_id: Optional[str] = None
    has_thinking_delta: bool = False
    completed_streamed_reasoning_texts: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ClaudeAgentService:
    """Core business service for the Claude Agent module.

    One shared instance is created by ``ClaudeAgentThreadFactory`` at startup.
    """

    def __init__(
        self,
        context_builder: Optional[ClaudeAgentContextBuilder] = None,
    ) -> None:
        self._context_builder = context_builder or ClaudeAgentContextBuilder()

    # ------------------------------------------------------------------
    # Phase 1: Context Assembly
    # ------------------------------------------------------------------

    async def assemble_context(
        self,
        request: ClaudeAgentRunRequest,
        *,
        state: AgentRunState,
        queue: asyncio.Queue,
        runner: ClaudeAgentRunner,
    ) -> "_TurnExecution":
        """Build context for the upcoming turn.

        On the first turn of a session: loads DB context and constructs the
        system prompt (expensive).  On subsequent turns within the keepalive
        window: reuses the cached ``state.system_prompt``.

        Returns a ``_TurnExecution`` ready to pass to ``execute_session``.
        """
        if not state.is_context_initialized:
            logger.debug(
                "Phase 1: building system_prompt for session_id=%s", state.session_id
            )
            system_prompt = await self._context_builder.build_system_prompt(
                request.user_id
            )
            state.with_system_prompt(system_prompt)
            state.is_context_initialized = True
        else:
            logger.debug(
                "Phase 1: reusing cached system_prompt for session_id=%s", state.session_id
            )

        user_message = self._context_builder.build_user_message(request.message)

        run_options = AgentRunOptions(
            thread_id=state.session_id,
            user_message=user_message,
            resume=request.resume,
            model=request.model,
            cwd=request.cwd or state.cwd or None,
            max_turns=request.max_turns,
            tool_choice=request.tool_choice,  # type: ignore[arg-type]
            system_prompt=state.system_prompt,
        )

        confirmation_store = ToolConfirmationStore()
        turn_ctx = _TurnContext(
            queue=queue,
            confirmation_store=confirmation_store,
        )
        state.turn_context = turn_ctx

        return _TurnExecution(
            request=request,
            state=state,
            runner=runner,
            run_options=run_options,
            turn_context=turn_ctx,
        )

    # ------------------------------------------------------------------
    # Phase 3: Session Execution
    # ------------------------------------------------------------------

    async def execute_session(self, execution: "_TurnExecution") -> None:
        """Stream the agent turn and emit SSE events via the queue."""
        queue = execution.turn_context.queue
        store = execution.turn_context.confirmation_store

        # Emit session metadata header
        await queue.put(
            _sse("message-metadata", {"sessionId": execution.state.session_id, "turnIndex": execution.state.turn_count})
        )

        callbacks = AgentStreamingCallbacks(
            on_text_delta=self._make_text_delta_cb(queue, execution.turn_context),
            on_text_done=self._make_text_done_cb(queue, execution.turn_context),
            on_tool_event=self._make_tool_event_cb(queue, execution.turn_context),
            on_tool_confirmation_request=self._make_tool_confirm_cb(queue, store, execution.turn_context),
            on_error=self._make_error_cb(queue),
        )

        result = await execution.runner.run_streaming(execution.run_options, callbacks)

        if result.success:
            full_text = result.full_text
            # Ensure any open text block is closed before message-final
            if execution.turn_context.text_started:
                await queue.put(_sse("text-end", {"id": _TEXT_PART_ID}))
                execution.turn_context.text_started = False
            await queue.put(
                _sse("message-final", {
                    "text": full_text,
                    "usage": result.usage,
                    "sessionId": result.session_id,
                })
            )
            await queue.put(_sse("finish", {"finishReason": "stop"}))
            # Persist user and assistant messages to the database
            await self._persist_turn(execution, full_text)
        else:
            error_msg = str(result.error) if result.error else "Unknown error"
            await queue.put(_sse("error", {"errorText": error_msg}))
            await queue.put(_sse("finish", {"finishReason": "error"}))

        await queue.put(None)  # Sentinel: end of stream

    async def _persist_turn(
        self, execution: "_TurnExecution", assistant_text: str
    ) -> None:
        """Save user and assistant messages to the database after a successful turn."""
        import asyncio
        import database

        thread_id = execution.request.thread_id
        user_text = execution.request.message
        user_message_id = execution.request.message_id

        loop = asyncio.get_running_loop()

        def _save() -> None:
            user_parts_json = json.dumps([{"type": "text", "text": user_text}])
            database.save_chat_message(
                thread_id,
                "user",
                user_text,
                parts_json=user_parts_json,
                message_id=user_message_id,
            )
            assistant_parts_json = json.dumps([{"type": "text", "text": assistant_text}])
            database.save_chat_message(
                thread_id,
                "assistant",
                assistant_text,
                parts_json=assistant_parts_json,
            )
            # Auto-fill thread title from first user message if still NULL
            thread = database.get_chat_thread(thread_id, int(execution.request.user_id))
            if thread and not thread.get("title"):
                title = user_text.strip()[:MAX_THREAD_TITLE_LENGTH]
                database.update_chat_thread_title(thread_id, title)

        try:
            await loop.run_in_executor(None, _save)
        except Exception:
            logger.exception(
                "Failed to persist messages for thread_id=%s", thread_id
            )

    # ------------------------------------------------------------------
    # Tool confirmation (called from HTTP endpoint via factory)
    # ------------------------------------------------------------------

    def confirm_tool(
        self,
        state: AgentRunState,
        tool_call_id: str,
        approved: bool,
        reason: Optional[str] = None,
        answers: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Resolve a pending tool confirmation."""
        turn_ctx = state.turn_context
        if turn_ctx is None:
            logger.warning(
                "confirm_tool: no active turn_context for session_id=%s", state.session_id
            )
            return False
        result = ToolConfirmationResult(approved=approved, reason=reason, answers=answers)
        return turn_ctx.confirmation_store.resolve(tool_call_id, result)

    # ------------------------------------------------------------------
    # SSE callback factories (aligned with Pawkeyland SSE protocol)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_text_delta_cb(
        queue: asyncio.Queue, turn_ctx: _TurnContext
    ):
        async def on_text_delta(delta: str) -> None:
            turn_ctx.full_text_accumulator.append(delta)
            if not turn_ctx.text_started:
                await queue.put(_sse("text-start", {"id": _TEXT_PART_ID}))
                turn_ctx.text_started = True
            await queue.put(_sse("text-delta", {"id": _TEXT_PART_ID, "delta": delta}))

        return on_text_delta

    @staticmethod
    def _make_text_done_cb(queue: asyncio.Queue, turn_ctx: _TurnContext):
        async def on_text_done(full_text: str) -> None:  # noqa: ARG001
            if turn_ctx.text_started:
                await queue.put(_sse("text-end", {"id": _TEXT_PART_ID}))
                turn_ctx.text_started = False

        return on_text_done

    @staticmethod
    def _make_tool_event_cb(queue: asyncio.Queue, turn_ctx: _TurnContext):
        """Emit Pawkeyland-style separated tool SSE events.

        Dispatches on ``ToolEventPayload.type`` (Pawkeyland convention) rather
        than the legacy ``payload.state`` approach, which could emit spurious
        ``tool-output-available`` frames for ``result`` events (tool_call_id=None).

        Handled event types:
          ``tool_use`` / ``tool_use_start``  → tool-input-start (+ input if present)
          ``tool_input_available``           → tool-input-start (dedup) + tool-input-available
          ``tool_result``                    → tool-output-available (with defensive auto-register)

        Intentionally ignored:
          ``result``, ``thinking``, ``thinking_delta``, ``content_block_stop``,
          ``message_start``, ``message_delta``, ``message_stop``, ``tool_progress``,
          ``tool_use_summary``, ``text_block_start``, ``tool_input_delta``
          — not applicable to Ink & Memory.
        """
        async def on_tool_event(payload: ToolEventPayload) -> None:
            # Close any open text block before tool events (matches Pawkeyland on_tool_event)
            if turn_ctx.text_started:
                await queue.put(_sse("text-end", {"id": _TEXT_PART_ID}))
                turn_ctx.text_started = False

            tool_call_id = payload.tool_call_id
            tool_name = payload.tool_name
            event_type = payload.type

            # --- thinking_delta: incremental reasoning stream ---
            if event_type == "thinking_delta" and payload.output:
                if not turn_ctx.current_reasoning_id:
                    turn_ctx.current_reasoning_id = str(uuid4())
                    await queue.put(_sse("reasoning-start", {"id": turn_ctx.current_reasoning_id}))
                turn_ctx.has_thinking_delta = True
                await queue.put(_sse("reasoning-delta", {
                    "id": turn_ctx.current_reasoning_id,
                    "delta": str(payload.output),
                }))
                return

            # --- content_block_stop for streamed thinking ---
            if event_type == "content_block_stop" and isinstance(payload.output, dict):
                content_block = payload.output.get("content_block")
                if (
                    isinstance(content_block, dict)
                    and content_block.get("type") == "thinking"
                    and turn_ctx.has_thinking_delta
                    and turn_ctx.current_reasoning_id
                ):
                    await queue.put(_sse("reasoning-end", {"id": turn_ctx.current_reasoning_id}))
                    turn_ctx.completed_streamed_reasoning_texts.append(
                        str(content_block.get("thinking") or "")
                    )
                    turn_ctx.current_reasoning_id = None
                    turn_ctx.has_thinking_delta = False
                    return

            # --- thinking: complete reasoning block (non-streamed or dedup guard) ---
            if event_type == "thinking" and payload.output:
                thinking_output = str(payload.output)
                if (
                    turn_ctx.completed_streamed_reasoning_texts
                    and thinking_output == turn_ctx.completed_streamed_reasoning_texts[0]
                ):
                    # Already emitted via thinking_delta stream — skip duplicate
                    turn_ctx.completed_streamed_reasoning_texts.pop(0)
                    return
                if turn_ctx.has_thinking_delta and turn_ctx.current_reasoning_id:
                    # thinking_delta already streamed content — close the open block
                    await queue.put(_sse("reasoning-end", {"id": turn_ctx.current_reasoning_id}))
                    turn_ctx.current_reasoning_id = None
                    turn_ctx.has_thinking_delta = False
                    return
                # No prior thinking_delta — emit full reasoning block atomically
                reasoning_id = str(uuid4())
                await queue.put(_sse("reasoning-start", {"id": reasoning_id}))
                await queue.put(_sse("reasoning-delta", {"id": reasoning_id, "delta": thinking_output}))
                await queue.put(_sse("reasoning-end", {"id": reasoning_id}))
                return

            # --- tool_use / tool_use_start: new tool call beginning ---
            if event_type in ("tool_use", "tool_use_start") and tool_call_id and tool_name:
                if tool_call_id not in turn_ctx.registered_tool_call_ids:
                    turn_ctx.registered_tool_call_ids.add(tool_call_id)
                    await queue.put(_sse("tool-input-start", {
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                    }))
                if (
                    payload.input is not None
                    and tool_call_id not in turn_ctx.emitted_tool_input_ids
                ):
                    turn_ctx.emitted_tool_input_ids.add(tool_call_id)
                    await queue.put(_sse("tool-input-available", {
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": payload.input,
                    }))
                return

            # --- tool_input_available: complete streamed JSON input ready ---
            if event_type == "tool_input_available" and tool_call_id and tool_name:
                if tool_call_id not in turn_ctx.registered_tool_call_ids:
                    turn_ctx.registered_tool_call_ids.add(tool_call_id)
                    await queue.put(_sse("tool-input-start", {
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                    }))
                if tool_call_id not in turn_ctx.emitted_tool_input_ids:
                    turn_ctx.emitted_tool_input_ids.add(tool_call_id)
                    await queue.put(_sse("tool-input-available", {
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": payload.input or {},
                    }))
                return

            # --- tool_result: tool execution result from user message ---
            if event_type == "tool_result" and tool_call_id:
                # Defensive: ensure tool-input-start was sent before
                # tool-output-available (AI SDK requires this ordering).
                if tool_call_id not in turn_ctx.registered_tool_call_ids:
                    fallback_name = tool_name or "unknown"
                    logger.warning(
                        "tool_result for unregistered toolCallId=%s (toolName=%s). "
                        "Auto-registering to prevent stream error.",
                        tool_call_id,
                        fallback_name,
                    )
                    turn_ctx.registered_tool_call_ids.add(tool_call_id)
                    await queue.put(_sse("tool-input-start", {
                        "toolCallId": tool_call_id,
                        "toolName": fallback_name,
                    }))
                    await queue.put(_sse("tool-input-available", {
                        "toolCallId": tool_call_id,
                        "toolName": fallback_name,
                        "input": {},
                    }))
                    turn_ctx.emitted_tool_input_ids.add(tool_call_id)
                await queue.put(_sse("tool-output-available", {
                    "toolCallId": tool_call_id,
                    "output": payload.output,
                    "isError": bool(payload.is_error),
                }))
                return

            # All other types (result, thinking, message_*, tool_progress, etc.)
            # are intentionally ignored for Ink & Memory.

        return on_tool_event

    @staticmethod
    def _make_tool_confirm_cb(
        queue: asyncio.Queue,
        store: ToolConfirmationStore,
        turn_ctx: _TurnContext,
    ):
        async def on_tool_confirmation_request(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
            tool_call_id: str = payload.get("tool_call_id", "") or payload.get("toolCallId", "")
            tool_name: str = payload.get("tool_name", "") or payload.get("toolName", "")
            tool_input: dict[str, Any] = payload.get("input") or {}

            # Step 0: register Future before any SSE that can trigger an immediate
            # POST /tool-confirm (fast clients must not resolve ahead of registration).
            store.begin_pending(tool_call_id)

            # Step 1: register and emit input events (dedup against on_tool_event).
            turn_ctx.registered_tool_call_ids.add(tool_call_id)
            await queue.put(_sse("tool-input-start", {
                "toolCallId": tool_call_id,
                "toolName": tool_name,
            }))
            turn_ctx.emitted_tool_input_ids.add(tool_call_id)
            await queue.put(_sse("tool-input-available", {
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": tool_input,
            }))

            # Step 2: emit approval-request event.
            await queue.put(_sse("tool-approval-request", {
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": tool_input,
            }))

            # Step 3 & 4: block until user responds.
            try:
                result = await store.await_pending(tool_call_id, tool_name=tool_name)
                return {"approved": result.approved, "reason": result.reason}
            except TimeoutError:
                logger.warning(
                    "Tool confirmation timed out: tool_call_id=%s tool_name=%s",
                    tool_call_id,
                    tool_name,
                )
                return {"approved": False, "reason": "timeout"}
            except asyncio.CancelledError:
                store.cancel_pending(tool_call_id)
                raise

        return on_tool_confirmation_request

    @staticmethod
    def _make_error_cb(queue: asyncio.Queue):
        async def on_error(exc: Exception) -> None:
            await queue.put(_sse("error", {"errorText": str(exc)}))

        return on_error



# ---------------------------------------------------------------------------
# Turn execution bundle
# ---------------------------------------------------------------------------


@dataclass
class _TurnExecution:
    request: ClaudeAgentRunRequest
    state: AgentRunState
    runner: ClaudeAgentRunner
    run_options: AgentRunOptions
    turn_context: _TurnContext


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse(event_type: str, data: dict[str, Any]) -> str:
    """Format a single SSE data frame."""
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"
