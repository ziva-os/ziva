export interface RightPanelTab {
  id: string;
  type: "review" | "plan" | "terminal" | "browser" | "files" | "agent-browser";
  title: string;
  // For browser tabs only: URL to load on creation. renderBrowserTab
  // defers the loadURL call until the webview's `did-attach` event
  // fires (the webContents isn't ready for loadURL any earlier —
  // calling it synchronously after appendChild silently no-ops).
  initialUrl?: string;
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

// A single queued message item (queue-based, supports multiple per session)
export interface PendingItem {
  id: string;
  text: string;
  retries: number;
  images?: PendingAttachment[];
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
  pendingMessages: Record<string, PendingItem[]>;
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
  // Secondary session IDs shown in side-by-side panes alongside the active session.
  splitSessions: string[];
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

// The singleton app store. Lives here (not in main.ts) so every module can
// import it directly instead of receiving it via dependency injection.
export const store = new Store<AppState>({
  sessions: [],
  activeSid: null,
  // See `AppState` — keyed by session id so a background session running its
  // own turn doesn't taint the active session's input and queue bar.
  runningSessions: {},
  pendingMessages: {},
  promptDrafts: {},
  compactingSessions: {},
  questionPending: false,
  config: { model: "unknown", models: [], modelDetails: [], approval: "suggest", workspace: "", tools: [], contextWindow: 200000 },
  recentWorkspaces: [],
  connected: false,
  tokenUsage: null,
  latencyMs: null,
  sidebarOpen: true,
  diffPanelOpen: false,
  rightPanelOpen: false,
  rightPanelTabs: [],
  activeRightTabId: null,
  theme: (document.documentElement.getAttribute("data-theme") as "dark" | "light") || "dark",
  autoScroll: true,
  splitSessions: [],
});

// ---- Persist per-session input state (pending queue + drafts) ----
// A page refresh would otherwise lose everything the user typed but hasn't
// sent yet. Save pendingMessages + promptDrafts to localStorage on every
// store change and restore them on load.
const PERSIST_KEY = "ziva:input-state-v1";
try {
  const saved = localStorage.getItem(PERSIST_KEY);
  if (saved) {
    const obj = JSON.parse(saved);
    const hasPending = obj.pendingMessages && Object.keys(obj.pendingMessages).length > 0;
    const hasDrafts = obj.promptDrafts && Object.keys(obj.promptDrafts).length > 0;
    if (hasPending || hasDrafts) {
      store.set({ pendingMessages: obj.pendingMessages || {}, promptDrafts: obj.promptDrafts || {} });
    }
  }
} catch { /* corrupt or localStorage unavailable — start fresh */ }

store.subscribe(() => {
  const s = store.get();
  try {
    localStorage.setItem(PERSIST_KEY, JSON.stringify({
      pendingMessages: s.pendingMessages,
      promptDrafts: s.promptDrafts,
    }));
  } catch { /* quota exceeded (large images) — best effort */ }
});
