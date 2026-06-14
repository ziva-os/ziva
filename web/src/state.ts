export interface RightPanelTab {
  id: string;
  type: "review" | "terminal" | "browser" | "files";
  title: string;
}

export interface Session {
  id: string;
  preview?: string;
  turnCount?: number;
  status?: "idle" | "running" | "done" | "failed";
  time?: { created: number; updated: number };
  // Absolute path of the workspace (project) this session belongs to.
  // Sessions are still per-workspace on disk, but the sidebar groups them
  // by workspace so a single list view can show every project at once.
  workspace?: string;
}

export interface PendingAttachment {
  path: string;
  mime: string;
  size: number;
  name: string;
  // Local blob URL for the in-input preview thumbnail. Never sent
  // to the server; the wire payload embeds `path` instead.
  thumbUrl: string;
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
  pendingMessages: Record<string, { text: string; retries: number }>;
  pendingSessionImages: Record<string, PendingAttachment[]>;
  // Per-session in-progress prompt content (textarea text + attached
  // images). Stashed on switchSession and restored when the user
  // comes back, so each session keeps its own draft.
  promptDrafts: Record<string, { text: string; images: PendingAttachment[] }>;
  compactingSessions: Record<string, boolean>;
  questionPending: boolean;
  config: {
    model: string;
    models: string[];
    modelDetails: Array<{ name: string; capabilities?: { vision?: boolean; thinking?: boolean; tools?: boolean } }>;
    approval: string;
    workspace: string;
    tools: string[];
    contextWindow: number;
  };
  recentWorkspaces: string[];
  connected: boolean;
  tokenUsage: { input: number; output: number } | null;
  latencyMs: number | null;
  sidebarOpen: boolean;
  diffPanelOpen: boolean;
  rightPanelOpen: boolean;
  rightPanelTabs: RightPanelTab[];
  activeRightTabId: string | null;
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
