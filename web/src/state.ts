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
  // Sessions whose turn the user has just stopped. While a sid is in
  // this set, routeSSEEvent drops tail events (delta / reasoning_delta /
  // tool_start / usage_update) so the UI doesn't keep growing the
  // assistant bubble or pulse the typing indicator between stop click
  // and the real turn_cancelled SSE arriving from the server. Cleared
  // by turn_cancelled / turn_failed handlers in main.ts.
  cancellingSids: string[];
  pendingMessages: Record<string, PendingItem[]>;
  // Per-session in-progress prompt content (textarea text + attached
  // images). Stashed on switchSession and restored when the user
  // comes back, so each session keeps its own draft.
  promptDrafts: Record<string, { text: string; images: PendingAttachment[] }>;
  compactingSessions: Record<string, boolean>;
  // Latest plan steps per session, populated either from a live
  // `update_plan` SSE event or from the in-session `renderPlanTab`
  // server fetch. Keyed by sid so two sessions running in parallel
  // (e.g. sidebar + a split pane) don't overwrite each other's plan.
  // Not persisted to localStorage on purpose — the server's session
  // JSONL is the durable record; this is just a per-session render
  // cache for the Plan tab.
  currentPlanSteps: Record<string, Array<{ id?: string; description?: string; status?: string }>>;
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
  cancellingSids: [],
  pendingMessages: {},
  promptDrafts: {},
  compactingSessions: {},
  currentPlanSteps: {},
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
//
// Important: PendingAttachment.thumbUrl is a *session-local* blob: URL
// (URL.createObjectURL) and is therefore dead after a page reload. We
// must strip it before serializing or every queued attachment re-renders
// as a broken image after refresh. The on-disk path stays valid — the
// server has it under ~/.ziva/sessions/<pid>/attachments/<sid>/ and
// serves it back via GET /attachments?path=... (see
// src/ziva/transports/desktop_api/server.py). The renderer falls back
// to attachmentUrl(img.path) when thumbUrl is missing/dies, so removing
// the blob URL here is safe.
function sanitizeAttachment(a: any): any {
  if (!a || typeof a !== "object") return a;
  const out = { ...a };
  if (typeof out.thumbUrl === "string" && out.thumbUrl.startsWith("blob:")) {
    delete out.thumbUrl;
  }
  return out;
}
function sanitizeTree(node: any): any {
  if (!node || typeof node !== "object") return node;
  if (Array.isArray(node)) return node.map(sanitizeTree);
  const out: any = { ...node };
  if (Array.isArray(out.images)) out.images = out.images.map(sanitizeAttachment);
  return out;
}
const PERSIST_KEY = "ziva:input-state-v1";
try {
  const saved = localStorage.getItem(PERSIST_KEY);
  if (saved) {
    const obj = JSON.parse(saved);
    const hasPending = obj.pendingMessages && Object.keys(obj.pendingMessages).length > 0;
    const hasDrafts = obj.promptDrafts && Object.keys(obj.promptDrafts).length > 0;
    if (hasPending || hasDrafts) {
      store.set({
        pendingMessages: sanitizeTree(obj.pendingMessages || {}),
        promptDrafts: sanitizeTree(obj.promptDrafts || {}),
      });
    }
  }
} catch { /* corrupt or localStorage unavailable — start fresh */ }

store.subscribe(() => {
  const s = store.get();
  try {
    // Drop dead blob: URLs before writing so a future refresh stays
    // clean. (See sanitizeAttachment above for why.)
    localStorage.setItem(PERSIST_KEY, JSON.stringify({
      pendingMessages: sanitizeTree(s.pendingMessages),
      promptDrafts: sanitizeTree(s.promptDrafts),
    }));
  } catch { /* quota exceeded (large images) — best effort */ }
});
