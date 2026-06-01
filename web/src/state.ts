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
  isRunning: boolean;
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

export class Store {
  private state: AppState;
  private listeners: Set<Listener> = new Set();

  constructor(initial: AppState) {
    this.state = initial;
  }

  get(): AppState {
    return this.state;
  }

  set(partial: Partial<AppState>): void {
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
