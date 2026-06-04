export interface Session {
  id: string;
  preview?: string;
  turnCount?: number;
  status?: "idle" | "running" | "done" | "failed";
  time?: { created: number; updated: number };
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
  config: {
    model: string;
    models: string[];
    modelDetails: Array<{ name: string; supports_image: boolean }>;
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
