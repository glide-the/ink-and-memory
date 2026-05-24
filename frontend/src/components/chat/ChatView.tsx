import { useMemo, useState, useEffect, useCallback } from 'react';
import '../../styles/markdown.css';
import { WorkspaceProvider } from '../../contexts/WorkspaceContext';
import FileSidebar from '../dashboard/FileSidebar';
import QuickActionCard from '../dashboard/QuickActionCard';
import { QUICK_ACTION_CARDS, type QuickActionCardItem } from '../dashboard/const';
import VerticalNav from '../dashboard/VerticalNav';
import ChatPanel from './ChatPanel';
import type { Attachment, ContextCustomer } from './AIInputDock';
import type { UIMessage } from 'ai';
import { getAuthToken } from '../../contexts/AuthContext';

const API_BASE = '/ink-and-memory';

interface ChatThread {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

interface RawChatMessage {
  id: string;
  role: string;
  content: string;
  parts_json?: string;
  created_at: string;
}

interface ChatViewProps {
  title?: string;
  threadId?: string;
  contextCustomerId?: string;
  contextCustomers?: ContextCustomer[];
  onNewChat?: () => void;
  onNavigateToSettings?: () => void;
  quickActions?: QuickActionCardItem[];
}

async function createThread(): Promise<string | null> {
  try {
    const res = await fetch(`${API_BASE}/api/claude-agent/threads`, { method: 'POST', headers: { 'Authorization': `Bearer ${getAuthToken()}` } });
    if (!res.ok) return null;
    const data = await res.json() as { thread_id: string };
    return data.thread_id ?? null;
  } catch {
    return null;
  }
}

async function fetchThreads(): Promise<ChatThread[]> {
  try {
    const res = await fetch(`${API_BASE}/api/claude-agent/threads`, { headers: { 'Authorization': `Bearer ${getAuthToken()}` } });
    if (!res.ok) return [];
    const data = await res.json() as { threads: ChatThread[] };
    return data.threads ?? [];
  } catch {
    return [];
  }
}

async function fetchThreadMessages(threadId: string): Promise<UIMessage[]> {
  try {
    const res = await fetch(`${API_BASE}/api/claude-agent/threads/${encodeURIComponent(threadId)}/messages`, { headers: { 'Authorization': `Bearer ${getAuthToken()}` } });
    if (!res.ok) return [];
    const data = await res.json() as { messages?: RawChatMessage[] };
    const msgs = data.messages ?? [];
    return msgs.map((m) => {
      let parts: UIMessage['parts'] = [{ type: 'text', text: m.content }];
      if (m.parts_json) {
        try { parts = JSON.parse(m.parts_json) as UIMessage['parts']; } catch { /* fallback */ }
      }
      return {
        id: m.id,
        role: m.role as UIMessage['role'],
        parts,
        createdAt: new Date(m.created_at),
      };
    });
  } catch {
    return [];
  }
}

async function deleteThread(threadId: string): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/claude-agent/threads/${encodeURIComponent(threadId)}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${getAuthToken()}` } });
    return res.ok;
  } catch {
    return false;
  }
}

export default function ChatView({
  title: _title,
  threadId: initialThreadId,
  contextCustomerId,
  contextCustomers = [],
  onNewChat,
  onNavigateToSettings,
  quickActions = QUICK_ACTION_CARDS,
}: ChatViewProps) {
  const [fileSidebarOpen, setFileSidebarOpen] = useState(false);
  const [queuedPrompt, setQueuedPrompt] = useState('');
  const [queuedAttachments, setQueuedAttachments] = useState<Attachment[]>([]);
  const [queuedPromptNonce, setQueuedPromptNonce] = useState(0);
  const [hasConversationStarted, setHasConversationStarted] = useState(false);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(initialThreadId ?? null);
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [threadSidebarOpen, setThreadSidebarOpen] = useState(false);
  const [isCreatingThread, setIsCreatingThread] = useState(false);
  const [threadMessages, setThreadMessages] = useState<UIMessage[] | null>(null);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);

  // Create an initial thread if none provided
  useEffect(() => {
    if (activeThreadId) return;
    let cancelled = false;
    void (async () => {
      const id = await createThread();
      if (!cancelled && id) setActiveThreadId(id);
    })();
    return () => { cancelled = true; };
  }, [activeThreadId]);

  // Load thread list
  const reloadThreads = useCallback(async () => {
    const list = await fetchThreads();
    setThreads(list);
  }, []);

  useEffect(() => {
    void reloadThreads();
  }, [reloadThreads]);

  // Fetch messages for the active thread (following better-chatbot pattern:
  // parent fetches history and passes as initialMessages to the chat component)
  useEffect(() => {
    if (!activeThreadId) return;
    let cancelled = false;
    setIsLoadingMessages(true);
    setThreadMessages(null);
    void (async () => {
      const msgs = await fetchThreadMessages(activeThreadId);
      if (!cancelled) {
        setThreadMessages(msgs);
        setIsLoadingMessages(false);
      }
    })();
    return () => { cancelled = true; };
  }, [activeThreadId]);

  const handleNewChat = useCallback(async () => {
    setIsCreatingThread(true);
    const id = await createThread();
    setIsCreatingThread(false);
    if (id) {
      setActiveThreadId(id);
      setHasConversationStarted(false);
      setQueuedPrompt('');
      setQueuedAttachments([]);
      void reloadThreads();
    }
    onNewChat?.();
  }, [onNewChat, reloadThreads]);

  const handleSelectThread = useCallback((threadId: string) => {
    // Clear threadMessages synchronously so the remounting ChatPanel receives
    // `initialMessages = undefined` (not the previous thread's messages).
    // React 18 batches all these setState calls into one render.
    setThreadMessages(null);
    setIsLoadingMessages(true);
    setActiveThreadId(threadId);
    setHasConversationStarted(true);
    setThreadSidebarOpen(false);
  }, []);

  const handleDeleteThread = useCallback(async (threadId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const ok = await deleteThread(threadId);
    if (ok) {
      const remaining = threads.filter((t) => t.id !== threadId);
      setThreads(remaining);
      if (threadId === activeThreadId) {
        if (remaining.length > 0) {
          // Clear stale messages synchronously before switching threads
          setThreadMessages(null);
          setIsLoadingMessages(true);
          setQueuedPrompt('');
          setQueuedAttachments([]);
          setActiveThreadId(remaining[0].id);
          setHasConversationStarted(true);
        } else {
          setActiveThreadId(null);
          setHasConversationStarted(false);
        }
      }
    }
  }, [activeThreadId, threads]);

  const activeThread = threads.find((t) => t.id === activeThreadId);
  const displayTitle = activeThread?.title ?? 'New conversation';

  const quickActionsGrid = useMemo(() => quickActions.length > 0, [quickActions.length]);

  if (!activeThreadId) {
    return (
      <WorkspaceProvider>
        <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', background: 'var(--color-bg-app)', color: 'var(--color-text-muted)' }}>
          {isCreatingThread ? 'Starting chat…' : 'Initializing…'}
        </div>
      </WorkspaceProvider>
    );
  }

  return (
    <WorkspaceProvider>
      <div style={{ display: 'flex', height: '100%', overflow: 'hidden', background: 'var(--color-bg-app)', color: 'var(--color-text-primary)', fontFamily: "'Excalifont', 'Xiaolai', Georgia, serif" }}>
        <VerticalNav onToggleFileSidebar={() => setFileSidebarOpen((value) => !value)} onNavigateToSettings={onNavigateToSettings} unreadCount={0} />

        {/* Thread history sidebar */}
        {threadSidebarOpen && (
          <aside style={{ width: '240px', flexShrink: 0, borderRight: '1px solid var(--color-border-paper)', background: 'var(--color-bg-paper)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <div style={{ padding: '0.75rem 1rem', borderBottom: '1px solid var(--color-border-paper)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: '0.85rem' }}>Chat History</span>
              <button type="button" onClick={() => setThreadSidebarOpen(false)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-muted)', fontSize: '1rem' }}>✕</button>
            </div>
            <div style={{ overflowY: 'auto', flex: 1 }}>
              {threads.length === 0 && (
                <div style={{ padding: '1rem', color: 'var(--color-text-muted)', fontSize: '0.8rem' }}>No conversations yet</div>
              )}
              {threads.map((thread) => (
                <div
                  key={thread.id}
                  onClick={() => handleSelectThread(thread.id)}
                  style={{ padding: '0.6rem 1rem', cursor: 'pointer', borderBottom: '1px solid var(--color-border-paper)', background: thread.id === activeThreadId ? 'var(--color-bg-app)' : 'transparent', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem' }}
                >
                  <span style={{ fontSize: '0.82rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{thread.title ?? 'New conversation'}</span>
                  <button
                    type="button"
                    onClick={(e) => void handleDeleteThread(thread.id, e)}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-text-muted)', fontSize: '0.75rem', flexShrink: 0 }}
                    title="Delete"
                  >🗑</button>
                </div>
              ))}
            </div>
          </aside>
        )}

        <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', padding: '1rem', gap: '1rem', overflow: 'hidden' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem', padding: '0.25rem 0.25rem 0 0.25rem', flexShrink: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <button type="button" onClick={() => setThreadSidebarOpen((v) => !v)} title="Chat history" style={{ border: '1px solid var(--color-border-paper)', borderRadius: '8px', padding: '0.4rem 0.6rem', background: 'var(--color-bg-paper)', color: 'var(--color-text-primary)', cursor: 'pointer' }}>☰</button>
              <div>
                <div style={{ fontSize: '0.74rem', letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--color-text-muted)' }}>Session</div>
                <h1 style={{ margin: '0.2rem 0 0', fontSize: '1.35rem', color: 'var(--color-text-primary)' }}>{displayTitle}</h1>
              </div>
            </div>
            <button type="button" onClick={() => void handleNewChat()} disabled={isCreatingThread} style={{ border: '1px solid var(--color-border-paper)', borderRadius: '999px', padding: '0.7rem 1rem', background: 'var(--color-bg-paper)', color: 'var(--color-text-primary)', fontWeight: 600, cursor: isCreatingThread ? 'wait' : 'pointer' }}>
              {isCreatingThread ? '…' : 'New chat'}
            </button>
          </div>

          {quickActionsGrid && !hasConversationStarted ? (
            <div style={{ display: 'grid', gap: '1rem', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', flexShrink: 0 }}>
              {quickActions.map((item) => (
                <QuickActionCard
                  key={item.title}
                  item={item}
                  onClick={(prompt) => {
                    setHasConversationStarted(true);
                    setQueuedPrompt(prompt);
                    setQueuedAttachments([]);
                    setQueuedPromptNonce((value) => value + 1);
                  }}
                />
              ))}
            </div>
          ) : null}

          <section style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <ChatPanel
              key={activeThreadId}
              threadId={activeThreadId}
              contextCustomerId={contextCustomerId}
              contextCustomers={contextCustomers}
              initialMessages={threadMessages ?? undefined}
              isLoading={isLoadingMessages}
              queuedPrompt={queuedPrompt}
              queuedAttachments={queuedAttachments}
              queuedPromptNonce={queuedPromptNonce}
              inputPlaceholder="Ask Ink & Memory…"
              onConversationStart={() => {
                setHasConversationStarted(true);
                void reloadThreads();
              }}
            />
          </section>
        </main>

        <FileSidebar sessionId={activeThreadId} open={fileSidebarOpen} onClose={() => setFileSidebarOpen(false)} />
      </div>
    </WorkspaceProvider>
  );
}

