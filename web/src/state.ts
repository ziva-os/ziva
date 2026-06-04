export interface Session {
  id: string;
  preview?: string;
  turnCount?: number;
  status?: "idle" | "running" | "done" | "failed";
  time?: { created: number; updated: number };
}

// A question card the user has already answered. Stored per-session so
// that switching to another session and back doesn't drop the
// "answered" state — loadHistory re-renders the chat container from
// scratch, so without this map the answered card would silently revert
// to the unanswered form.
export interface AnsweredQuestion {
  question: string;
  options: string[];
  multiSelect: boolean;
  // Either a single selected option (string) or a JSON array for
  // multi-select. We store as a free-form string to keep the type
  // simple — the renderer just displays it verbatim.
  answer: string;
  // First 100 chars of the user message that triggered this
  // question. Used during session-switch restore to find the right
  // position via content matching (compaction can invalidate a
  // stored index, but text matching is robust).
  userMsgText: string;
}

export interface AppState {
  sessions: Session[];
  activeSid: string | null;
  // Per-session transient flags keyed by session id, so a session
  // running in the background (e.g. user opened a different session
  // in the sidebar while a turn is still streaming) doesn't leak
  // its "is running" or queued-input state into the newly active
  // session. The render / input layer consults these by activeSid.
  runningSessions: Record<string, boolean>;
  pendingMessages: Record<string, string>;
  // Per-session answered question cards, in order of completion. The
  // chat DOM is rebuilt from history on session switch, so the only
  // way to keep an answered question card visible after the user
  // switches away and back is to store it here and re-insert it
  // during loadHistory.
  answeredQuestions: Record<string, AnsweredQuestion[]>;
  config: {
    model: string;
    models: string[];
    approval: string;
    workspace: string;
    tools: string[];
  };
  connected: boolean;
  tokenUsage: { input: number; output: number } | null;
  latencyMs: number | null;
  sidebarOpen: boolean;
  diffPanelOpen: boolean;
  theme: "dark" | "light";
  autoScroll: boolean;
}

type Listener = () => void;

export class Store<T = AppState> {
  private state: T;
  private listeners: Set<Listener> = new Set();

  constructor(initial: T) {
    this.state = initial;
  }

  get(): T {
    return this.state;
  }

  set(partial: Partial<T>): void {
    this.state = { ...this.state, ...partial };
    for (const fn of this.listeners) {
      try { fn(); } catch { /* ignore */ }
    }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }
}
