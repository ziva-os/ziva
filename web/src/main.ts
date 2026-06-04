import "./styles/base.css";
import "./styles/theme-dark.css";
import "./styles/theme-light.css";
import "./styles/components.css";
import * as api from "./api";
import { SSEPool } from "./sse";
import { renderMarkdown, addCopyButtons, highlightCode, extractThinking } from "./markdown";
import { Store } from "./state";
import type { AppState } from "./state";

// ---- Helpers ----
function esc(s: string): string {
  const d = document.createElement("span");
  d.textContent = s;
  return d.innerHTML;
}

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

// ---- State ----
const store = new Store<AppState>({
  sessions: [],
  activeSid: null,
  // See `AppState` — keyed by session id so a background session
  // running its own turn doesn't taint the active session's input
  // and queue bar.
  runningSessions: {},
  pendingMessages: {},
  questionPending: false,
  config: { model: "unknown", models: [], approval: "suggest", workspace: "", tools: [] },
  connected: false,
  tokenUsage: null,
  latencyMs: null,
  sidebarOpen: true,
  diffPanelOpen: false,
  theme: (document.documentElement.getAttribute("data-theme") as "dark" | "light") || "dark",
  autoScroll: true,
});

// ---- Per-session state helpers ----
// Reading the running flag for the active session. Other sessions'
// values are kept in the map but only matter for background turns
// (e.g. when a question card is answered in a non-active session —
// handled in the SSE event path).
function isActiveRunning(): boolean {
  const { activeSid, runningSessions } = store.get();
  return !!activeSid && !!runningSessions[activeSid];
}

function getActivePending(): string | null {
  const { activeSid, pendingMessages } = store.get();
  if (!activeSid) return null;
  return pendingMessages[activeSid] ?? null;
}

function setActivePending(text: string | null) {
  const { activeSid, pendingMessages } = store.get();
  if (!activeSid) return;
  const next = { ...pendingMessages };
  if (text == null) delete next[activeSid];
  else next[activeSid] = text;
  store.set({ pendingMessages: next });
}

function setActiveRunning(running: boolean) {
  const { activeSid, runningSessions } = store.get();
  if (!activeSid) return;
  const next = { ...runningSessions, [activeSid]: running };
  if (!running) delete next[activeSid];
  store.set({ runningSessions: next });
}

const sse = new SSEPool();
const pendingTools = new Map<string, HTMLElement>();
let currentAssistantEl: HTMLElement | null = null;
let currentTextParts: { thinking: string; main: string } = { thinking: "", main: "" };

// ---- Empty State ----
function showEmptyState(show: boolean) {
  const center = document.querySelector(".ziva-center");
  if (center) center.classList.toggle("has-messages", !show);
  $("messages").style.display = show ? "none" : "block";
}

// ---- Relative time formatting ----
function formatRelativeTime(ts?: number): string {
  if (!ts) return "";
  const now = Date.now();
  const diff = now - ts;
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  if (hours < 24) return `${hours}h`;
  if (days < 7) return `${days}d`;
  if (days < 30) return `${Math.floor(days / 7)}w`;
  return `${Math.floor(days / 30)}mo`;
}

// ---- DOM Bootstrap — Ziva layout ----
function init() {
  const app = $("app");
  app.innerHTML = `
    <div class="ziva-layout">
      <aside class="ziva-sidebar" id="sidebar">
        <div class="sidebar-header">
          <span class="sidebar-title">Ziva</span>
          <button class="sidebar-toggle-btn" id="btnToggleSidebar" title="Toggle sidebar" aria-label="Toggle sidebar">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
        </div>
        <div class="sidebar-top">
          <button id="btnNewSession" class="sidebar-btn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            <span>New conversation</span>
          </button>
        </div>
        <div class="sidebar-nav">
          <button class="sidebar-nav-item" id="btnSkills">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
            <span>Skills</span>
          </button>
          <button class="sidebar-nav-item" id="btnScheduled">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
            <span>自动化</span>
          </button>
        </div>
        <div class="sidebar-section-header">
          <span>Projects</span>
          <div class="section-actions">
            <button class="section-action-btn" id="btnFilterSessions" title="Filter"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></button>
            <button class="section-action-btn" id="btnSelectMode" title="Select all">☐</button>
            <button class="section-action-btn delete-selected-btn" id="batchDeleteBtn" title="Delete selected" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg></button>
          </div>
        </div>
        <div class="sidebar-search" id="sessionSearch" style="display:none">
          <input type="text" id="sessionSearchInput" placeholder="Search conversations..." />
        </div>
        <div class="sessions-list" id="sessionList"></div>
        <div class="sidebar-bottom">
          <div class="mcp-status" id="mcpStatus" style="display:none">
            <span class="mcp-indicator">⚡</span>
            <span class="mcp-label">MCP:</span>
            <span class="mcp-detail" id="mcpDetail"></span>
          </div>
          <button class="sidebar-nav-item" id="btnTheme">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
            <span>Theme</span>
          </button>
          <button class="sidebar-nav-item" id="btnRightPanel">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path></svg>
            <span>Changes</span>
          </button>
          <button class="sidebar-nav-item" id="btnSettings">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            <span>Settings</span>
          </button>
        </div>
      </aside>
      <button class="sidebar-open-btn" id="btnOpenSidebar" title="Open sidebar" aria-label="Open sidebar">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
      </button>
      <main class="ziva-center">
        <div class="empty-state" id="emptyState">
          <div class="workspace-context show" id="workspaceContext">
            <span class="context-label">New conversation in</span>
            <span class="context-workspace" id="contextWorkspace">
              <span id="workspaceName">ziva</span>
              <span class="context-chevron">▾</span>
            </span>
          </div>
        </div>
        <div class="messages" id="messages" style="display:none"></div>
        <div class="ziva-composer-wrapper" id="composerWrapper">
          <div class="ziva-composer">
            <div class="pending-bar" id="pendingBar" hidden>
              <span class="pending-bar-label">排队中</span>
              <span class="pending-bar-text" id="pendingBarText"></span>
              <button class="pending-bar-clear" id="pendingBarClear" title="取消排队" type="button">×</button>
            </div>
            <div class="image-previews" id="imagePreviews" style="display:none"></div>
            <input type="file" id="imageFileInput" accept="image/*" multiple style="display:none" />
            <textarea id="prompt" placeholder="Ask anything, @ to mention, / for workflows" rows="1"></textarea>
            <div class="slash-menu" id="slashMenu" style="display:none"></div>
            <div class="composer-toolbar">
              <div class="toolbar-left">
                <button class="composer-action-btn" id="btnAttach" title="Attach image">📎</button>
                <select id="approvalSelect" title="Mode">
                  <option value="suggest">Fast</option>
                  <option value="auto-edit">Auto Edit</option>
                  <option value="full-auto">Full Auto</option>
                </select>
                <select id="modelSelect" title="Model"></select>
              </div>
              <div class="toolbar-right">
                <span class="char-count" id="charCount"></span>
                <div class="context-ring" id="contextRing" title="Context usage">
                  <svg viewBox="0 0 24 24" width="28" height="28">
                    <circle cx="12" cy="12" r="11" fill="none" stroke="var(--line)" stroke-width="2.5" />
                    <circle cx="12" cy="12" r="11" fill="none" stroke="var(--accent)" stroke-width="2.5"
                      stroke-dasharray="69.12" stroke-dashoffset="69.12" stroke-linecap="round"
                      transform="rotate(-90 12 12)" id="contextArc" />
                  </svg>
                  <span class="context-pct" id="contextPct"></span>
                </div>
                <button id="btnSend" class="send-btn" title="Send">→</button>
              </div>
            </div>
          </div>
          <div class="composer-footer" id="composerFooter">
            <a id="linkEditor"><span class="footer-icon">&lt;&gt;</span> Open editor</a>
          </div>
        </div>
      </main>
      <aside class="ziva-right-panel" id="rightPanel">
        <div class="right-panel-header">
          <span>Changes</span>
          <span class="stats" id="diffStats"></span>
          <button id="btnCloseRight">&times;</button>
        </div>
        <div class="right-panel-body" id="diffBody">
          <div class="diff-empty">No changes yet</div>
        </div>
      </aside>
    </div>`;

  bindEvents();
  refreshStatus();
  refreshMCPStatus();
  refreshConfig();
  refreshSessions().then(() => {
    const s = store.get();
    if (s.sessions.length > 0) {
      switchSession(s.sessions[0].id);
    } else {
      createSession();
    }
  });
}

// ---- Event Bindings ----
function bindEvents() {
  const promptEl = $("prompt");
  promptEl.addEventListener("input", () => {
    promptEl.style.height = "auto";
    promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + "px";
    const chars = promptEl.value.length;
    const tokens = Math.round(chars / 4);
    $("charCount").textContent = chars > 0 ? `${chars}` : "";
    const text = promptEl.value;
    if (text.startsWith("/")) {
      showSlashMenu(text);
    } else {
      hideSlashMenu();
    }
  });
  promptEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      if (isSlashMenuVisible()) {
        e.preventDefault();
        selectSlashCommand();
        return;
      }
      e.preventDefault();
      if (isActiveRunning()) { queuePromptMessage(); } else { sendMessage(); }
    }
    if (e.key === "Escape") {
      hideSlashMenu();
      if (isActiveRunning()) { cancelTurn(); }
    }
    if (e.key === "ArrowDown" && isSlashMenuVisible()) { e.preventDefault(); moveSlashSelection(1); }
    if (e.key === "ArrowUp" && isSlashMenuVisible()) { e.preventDefault(); moveSlashSelection(-1); }
  });

  $("btnSend").onclick = () => {
    if (isActiveRunning()) { cancelTurn(); } else { sendMessage(); }
  };
  // Pending-message bar: clicking the text brings the queued content
  // back into the prompt for editing; the × button drops it.
  const pendingBarText = $("pendingBarText");
  const pendingBarClear = $("pendingBarClear");
  if (pendingBarText) pendingBarText.onclick = editPendingMessage;
  if (pendingBarClear) pendingBarClear.onclick = clearPendingMessage;
  $("btnNewSession").onclick = () => createSession();
  $("btnRightPanel").onclick = toggleDiff;
  $("btnCloseRight").onclick = toggleDiff;

  $("btnSkills").onclick = () => openSkillsBrowser();
  $("btnScheduled").onclick = () => openAutomationsModal();
  $("btnSettings").onclick = () => openSettingsModal();

  $("btnTheme").onclick = () => {
    const current = store.get().theme;
    const next = current === "dark" ? "light" : "dark";
    store.set({ theme: next });
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("ziva-theme", next);
  };

  $("btnFilterSessions").onclick = () => {
    const searchDiv = $("sessionSearch");
    const visible = searchDiv.style.display !== "none";
    searchDiv.style.display = visible ? "none" : "block";
    if (!visible) ($("sessionSearchInput") as HTMLInputElement).focus();
  };

  $("sessionSearchInput").addEventListener("input", () => renderSessions());

  $("btnSelectMode").onclick = () => {
    const list = $("sessionList");
    const on = list.dataset.selectMode !== "true";
    list.dataset.selectMode = on ? "true" : "false";
    $("btnSelectMode").textContent = on ? "☑" : "☐";
    ($("batchDeleteBtn") as HTMLElement).style.display = on ? "flex" : "none";
    renderSessions();
    if (on) {
      list.querySelectorAll<HTMLInputElement>(".session-checkbox").forEach(cb => cb.checked = true);
    }
  };

  $("batchDeleteBtn").onclick = async () => {
    const checked = $("sessionList").querySelectorAll<HTMLInputElement>(".session-checkbox:checked");
    const ids = Array.from(checked).map(cb => cb.dataset.sid!);
    if (ids.length === 0) return;
    if (!confirm(`Delete ${ids.length} sessions?`)) return;
    const { activeSid } = store.get();
    for (const sid of ids) {
      await api.deleteSession(sid);
      if (sid === activeSid) {
        store.set({ activeSid: null });
        $("messages").innerHTML = "";
        showEmptyState(true);
      }
    }
    $("sessionList").dataset.selectMode = "false";
    $("btnSelectMode").textContent = "☐";
    ($("batchDeleteBtn") as HTMLElement).style.display = "none";
    await refreshSessions();
    // Switch to first remaining session if current was deleted
    if (ids.includes(activeSid || "")) {
      const sessions = store.get().sessions;
      if (sessions.length > 0) {
        switchSession(sessions[0].id);
      }
    }
  };

  $("modelSelect").onchange = async () => {
    const model = ($("modelSelect") as HTMLSelectElement).value;
    await api.updateConfig({ model: { name: model } });
    store.set({ config: { ...store.get().config, model } });
  };

  $("approvalSelect").onchange = async () => {
    const policy = ($("approvalSelect") as HTMLSelectElement).value;
    await api.updateConfig({ approval: { policy } });
    store.set({ config: { ...store.get().config, approval: policy } });
  };

  // Sidebar collapse/expand — driven by toggling `.sidebar-collapsed` on
  // the layout container. The CSS hides the sidebar's contents and
  // reveals the small `.sidebar-open-btn` floating at the sidebar's old
  // position so the user can re-open it. Cmd/Ctrl+B is the keyboard
  // shortcut; the click handlers below mirror it for the two buttons.
  const toggleSidebar = () => {
    const layout = document.querySelector(".ziva-layout") as HTMLElement | null;
    if (!layout) return;
    layout.classList.toggle("sidebar-collapsed");
    const collapsed = layout.classList.contains("sidebar-collapsed");
    localStorage.setItem("ziva-sidebar-collapsed", collapsed ? "1" : "0");
    // Keep the fullpage overlay's left edge in sync with the live
    // sidebar width (it spans from the sidebar's right edge to the
    // viewport's right edge).
    document.documentElement.style.setProperty(
      "--sidebar-width",
      collapsed ? "0px" : "var(--sidebar-real-width, 260px)"
    );
  };
  $("btnToggleSidebar").onclick = toggleSidebar;
  $("btnOpenSidebar").onclick = toggleSidebar;
  // Restore previous state
  if (localStorage.getItem("ziva-sidebar-collapsed") === "1") {
    document.querySelector(".ziva-layout")?.classList.add("sidebar-collapsed");
  }
  // Initialize --sidebar-width so the fullpage overlay's left edge
  // aligns with the live sidebar width.
  const initialCollapsed = localStorage.getItem("ziva-sidebar-collapsed") === "1";
  document.documentElement.style.setProperty(
    "--sidebar-width",
    initialCollapsed ? "0px" : "260px"
  );

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "d") { e.preventDefault(); toggleDiff(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "b") { e.preventDefault(); toggleSidebar(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "n") { e.preventDefault(); createSession(); }
    if (e.key === "Escape") {
      if (document.getElementById("skillsModalBackdrop")) {
        e.preventDefault();
        closeSkillViewer();
      } else if (document.getElementById("automationsModalBackdrop")) {
        e.preventDefault();
        closeAutomationsModal();
      }
    }
  });

  $("messages").addEventListener("scroll", () => {
    const el = $("messages");
    store.set({ autoScroll: el.scrollTop + el.clientHeight >= el.scrollHeight - 50 });
  });

  // Image upload: attach button, paste, drag-and-drop
  $("btnAttach").onclick = () => ($("imageFileInput") as HTMLInputElement).click();
  ($("imageFileInput") as HTMLInputElement).onchange = (e) => {
    const files = (e.target as HTMLInputElement).files;
    if (files) for (const f of files) addImageFile(f);
    (e.target as HTMLInputElement).value = "";
  };
  promptEl.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        addImageFile(item.getAsFile()!);
      }
    }
  });
  promptEl.addEventListener("dragover", (e) => { e.preventDefault(); });
  promptEl.addEventListener("drop", (e) => {
    e.preventDefault();
    const files = e.dataTransfer?.files;
    if (files) for (const f of files) { if (f.type.startsWith("image/")) addImageFile(f); }
  });

  const savedTheme = localStorage.getItem("ziva-theme") as "dark" | "light" | null;
  if (savedTheme) {
    store.set({ theme: savedTheme });
    document.documentElement.setAttribute("data-theme", savedTheme);
  }
}

// ---- Image Attachments ----
let pendingImages: Array<{ dataUrl: string; name: string }> = [];

function addImageFile(file: File) {
  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = reader.result as string;
    pendingImages.push({ dataUrl, name: file.name });
    renderImagePreviews();
  };
  reader.readAsDataURL(file);
}

function renderImagePreviews() {
  const container = $("imagePreviews") as HTMLElement;
  if (pendingImages.length === 0) {
    container.style.display = "none";
    container.innerHTML = "";
    return;
  }
  container.style.display = "flex";
  container.innerHTML = pendingImages.map((img, i) =>
    `<div class="image-preview-item">
      <img src="${img.dataUrl}" alt="${esc(img.name)}" />
      <button class="image-preview-remove" data-idx="${i}" title="Remove">×</button>
    </div>`
  ).join("");
  container.querySelectorAll(".image-preview-remove").forEach(btn => {
    (btn as HTMLElement).onclick = () => {
      const idx = parseInt((btn as HTMLElement).dataset.idx || "0");
      pendingImages.splice(idx, 1);
      renderImagePreviews();
    };
  });
}

// ---- Skills panel + viewer modal ----
// The sidebar has a "Skills" button that toggles a panel listing the
// skills the runtime loaded at startup. Clicking a skill opens a modal
// with the SKILL.md body rendered as markdown. Relative file links
// inside the rendered markdown (e.g. `references/snapshot-refs.md`)
// are intercepted and re-loaded into the same modal, so users can
// navigate the skill's reference tree without leaving the chat surface.

let skillsCache: api.Skill[] | null = null;
let skillsBrowserState: { query: string; category: string | null } = { query: "", category: null };
// Navigation history for the skill viewer. Pushing a page adds to the
// stack; clicking back pops the top and renders the new top. The
// stack is cleared whenever the user opens a different skill from
// the list (so back from the first page of a skill returns to the
// list, not to a previously viewed skill).
let skillNavStack: { name: string; path: string }[] = [];

async function openSkillsBrowser() {
  closeAllFullpageOverlays();
  skillNavStack = [];
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "skillsModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">📚 Skills</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-toolbar">
        <div class="skills-search-box">
          <span class="skills-search-icon">🔍</span>
          <input type="text" id="skillsSearchInput" placeholder="Search by name or description..." />
        </div>
        <div class="skills-category-tabs" id="skillsCategoryTabs"></div>
      </div>
      <div class="fullpage-body" id="skillsModalBody">
        <div class="skills-modal-loading">Loading skills...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  (backdrop.querySelector("#skillsSearchInput") as HTMLInputElement).oninput = (e) => {
    skillsBrowserState.query = (e.target as HTMLInputElement).value;
    renderSkillsBrowserBody();
  };

  try {
    if (!skillsCache) skillsCache = await api.listSkills();
    renderSkillsBrowser();
  } catch (e) {
    const body = backdrop.querySelector("#skillsModalBody") as HTMLElement;
    body.innerHTML = `<div class="skills-modal-error">Failed to load: ${esc((e as Error).message)}</div>`;
  }
}

function renderSkillsBrowser() {
  renderSkillsCategoryTabs();
  renderSkillsBrowserBody();
}

function renderSkillsCategoryTabs() {
  const tabs = document.getElementById("skillsCategoryTabs");
  if (!tabs || !skillsCache) return;
  const counts = new Map<string, number>();
  for (const s of skillsCache) {
    const c = s.category || "其他";
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  // Stable sort by name, then by count desc
  const sorted = Array.from(counts.entries()).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  const total = skillsCache.length;
  const active = skillsBrowserState.category;
  tabs.innerHTML = `
    <button class="skills-category-tab ${active === null ? "active" : ""}" data-cat="">
      全部 <span class="skills-cat-count">${total}</span>
    </button>` +
    sorted.map(([cat, n]) => `
      <button class="skills-category-tab ${active === cat ? "active" : ""}" data-cat="${esc(cat)}">
        ${esc(cat)} <span class="skills-cat-count">${n}</span>
      </button>`).join("");
  tabs.querySelectorAll<HTMLElement>(".skills-category-tab").forEach((btn) => {
    btn.onclick = () => {
      const cat = btn.dataset.cat || null;
      skillsBrowserState.category = cat;
      renderSkillsBrowser();
    };
  });
}

function renderSkillsBrowserBody() {
  const body = document.getElementById("skillsModalBody");
  if (!body || !skillsCache) return;
  const q = skillsBrowserState.query.trim().toLowerCase();
  const cat = skillsBrowserState.category;
  const filtered = skillsCache.filter((s) => {
    if (cat && (s.category || "其他") !== cat) return false;
    if (!q) return true;
    return s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q);
  });

  if (filtered.length === 0) {
    body.innerHTML = '<div class="skills-empty">No skills match your search.</div>';
    return;
  }

  // Group by category for the visual layout
  const groups = new Map<string, api.Skill[]>();
  for (const s of filtered) {
    const c = s.category || "其他";
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c)!.push(s);
  }
  // Use the same category order shown in the tabs
  const orderedCats = cat ? [cat] : Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));

  let html = "";
  for (const c of orderedCats) {
    const items = groups.get(c) || [];
    html += `<div class="skills-group">`;
    html += `<div class="skills-group-header">${esc(c)} <span class="skills-group-count">${items.length}</span></div>`;
    html += `<div class="skills-grid">`;
    for (const s of items) {
      html += `
        <div class="skill-card" data-skill-path="${esc(s.path)}" data-skill-name="${esc(s.name)}">
          <div class="skill-card-name">${esc(s.name)}</div>
          <div class="skill-card-desc">${esc(s.description || "(no description)")}</div>
          <div class="skill-card-footer">
            <span class="skill-card-cat">${esc(s.category || "其他")}</span>
          </div>
        </div>`;
    }
    html += `</div></div>`;
  }
  body.innerHTML = html;
  body.querySelectorAll<HTMLElement>(".skill-card").forEach((el) => {
    el.onclick = () => {
      const path = el.dataset.skillPath!;
      const name = el.dataset.skillName!;
      // Clear any prior skill's history so back from the first page
      // goes to the list, not to a previously viewed skill.
      skillNavStack = [];
      openSkillViewer(name, path, /*pushToStack*/ true);
    };
  });
}

// Open the skill viewer modal on a specific file. `pushToStack` controls
// whether this navigation becomes a new history entry: true when the
// user clicked forward (skill card, reference link), false when
// restoring from the back stack.
function openSkillViewer(displayName: string, filePath: string, pushToStack: boolean = true) {
  if (pushToStack) {
    skillNavStack.push({ name: displayName, path: filePath });
  }
  closeSkillViewer();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "skillsModalBackdrop";
  const showBack = skillNavStack.length > 0;
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <button class="fullpage-back" id="skillsModalBack" style="display:${showBack ? "flex" : "none"}">
          <span class="back-arrow">←</span>
          <span>back</span>
        </button>
        <div class="fullpage-title" id="skillsModalTitle">${esc(displayName)}</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body fullpage-body-wide" id="skillsModalBody">
        <div class="skills-modal-loading">Loading ${esc(displayName)}...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  (backdrop.querySelector("#skillsModalBack") as HTMLElement).onclick = () => {
    // Pop the current page; if there's still a previous page, render
    // it. If the stack is now empty, fall back to the skill list.
    skillNavStack.pop();
    const prev = skillNavStack[skillNavStack.length - 1];
    if (prev) {
      openSkillViewer(prev.name, prev.path, /*pushToStack*/ false);
    } else {
      openSkillsBrowser();
    }
  };

  loadSkillFileIntoModal(displayName, filePath);
}

function closeSkillViewer() {
  document.getElementById("skillsModalBackdrop")?.remove();
}

// Close every fullpage overlay currently on screen. Called when the
// user picks a destination from the sidebar (a session, a nav item
// other than the current one) so the chat surface is restored.
function closeAllFullpageOverlays() {
  closeSkillViewer();
  closeAutomationsModal();
  closeSettingsModal();
}

// Fetch a skill file and render its markdown body into the modal.
// Relative `.md`/`.markdown`/text links in the rendered HTML are
// re-wired to a click handler that re-enters this function with the
// resolved absolute path, so users can navigate within a skill's
// reference tree without leaving the chat surface.
async function loadSkillFileIntoModal(displayName: string, filePath: string) {
  const body = document.getElementById("skillsModalBody");
  const title = document.getElementById("skillsModalTitle");
  if (!body || !title) return;
  body.innerHTML = '<div class="skills-modal-loading">Loading...</div>';
  if (title) title.textContent = displayName;
  try {
    const data = await api.readSkillFile(filePath);
    // Strip the YAML frontmatter for display — the sidebar list already
    // shows the description, and the raw frontmatter adds noise.
    const content = stripFrontmatter(data.content);
    body.innerHTML = `<div class="md">${renderMarkdown(content)}</div>`;
    addCopyButtons(body);
    highlightCode(body);
    interceptSkillLinks(body, data.path);
    // Scroll the modal to the top whenever a new file is loaded
    body.scrollTop = 0;
  } catch (e) {
    const msg = (e as any)?.error || (e as Error).message;
    body.innerHTML = `<div class="skills-modal-error">Failed to load: ${esc(msg)}</div>`;
  }
}

// Walk the rendered markdown container and turn any relative link
// pointing to a file under the same skill directory into a click
// handler that loads that file inline. External / absolute links
// remain normal `<a>` elements (still openable in a new tab, etc.).
function interceptSkillLinks(container: HTMLElement, currentFilePath: string) {
  const links = container.querySelectorAll<HTMLAnchorElement>("a[href]");
  const baseDir = currentFilePath.replace(/[^/]+$/, "");
  for (const a of links) {
    const href = a.getAttribute("href") || "";
    // Skip external links, anchors, mailto, etc.
    if (!href || href.startsWith("http") || href.startsWith("https") ||
        href.startsWith("mailto:") || href.startsWith("#") ||
        href.startsWith("/")) {
      // For absolute paths that point into the skill roots, still intercept
      if (href.startsWith("/") && isPathInSkillRoots(href)) {
        a.classList.add("skill-file-link");
        a.onclick = (e) => {
          e.preventDefault();
          const name = href.split("/").pop() || href;
          openSkillViewer(name, href, /*pushToStack*/ true);
        };
      }
      continue;
    }
    // Strip any anchor fragment for the file resolution
    const [rel] = href.split("#");
    if (!rel) continue;
    // Only intercept .md / .markdown / .txt / no-extension references —
    // everything else (images, binaries) is left as a plain link.
    const isLikelyDoc = /\.(md|markdown|txt)$/i.test(rel) || !/\.[a-z0-9]+$/i.test(rel);
    if (!isLikelyDoc) continue;
    const resolved = baseDir + rel;
    a.classList.add("skill-file-link");
    a.onclick = (e) => {
      e.preventDefault();
      const name = rel.split("/").pop() || rel;
      openSkillViewer(name, resolved, /*pushToStack*/ true);
    };
  }
}

function isPathInSkillRoots(p: string): boolean {
  // Best-effort client-side check — the server is the final authority.
  // We don't know the skill roots here, so allow anything that looks
  // like a markdown file and let the server reject if it's outside.
  return /\.(md|markdown|txt)$/i.test(p);
}

function stripFrontmatter(content: string): string {
  // YAML frontmatter is delimited by `---` lines at the very top of the
  // file. We strip it for display so the user sees only the body of
  // the skill, not its metadata block.
  if (!content.startsWith("---")) return content;
  const end = content.indexOf("\n---", 3);
  if (end < 0) return content;
  // Skip past the closing `---` and any trailing newline
  let rest = content.slice(end + 4);
  if (rest.startsWith("\n")) rest = rest.slice(1);
  return rest;
}

// ---- Config ----
async function refreshConfig() {
  try {
    const cfg = await api.getConfig();
    const modelDetails = (cfg.model as any).models || (cfg.model.available || []).map((m: string) => ({ name: m, supports_image: false }));
    store.set({ config: { ...store.get().config, model: cfg.model.current, models: cfg.model.available, modelDetails, approval: cfg.approval.current } });
    const sel = $("modelSelect") as HTMLSelectElement;
    sel.innerHTML = cfg.model.available.map((m: string) => `<option value="${esc(m)}" ${m === cfg.model.current ? "selected" : ""}>${esc(m)}</option>`).join("");
    ($("approvalSelect") as HTMLSelectElement).value = cfg.approval.current;
    updateImageSupport();
  } catch { /* server not running */ }
}

function updateImageSupport() {
  const { config } = store.get();
  const details = config.modelDetails || [];
  const current = details.find((m: any) => m.name === config.model);
  const supportsImage = current?.supports_image ?? false;
  const attachBtn = $("btnAttach") as HTMLElement | null;
  if (attachBtn) attachBtn.style.display = supportsImage ? "" : "none";
}

// ---- Sessions ----
async function refreshSessions() {
  const raw = await api.listSessions();
  const currentSessions = store.get().sessions;
  const existingMap = new Map(currentSessions.map(s => [s.id, s]));

  const sessions = raw.map(s => {
    const existing = existingMap.get(s.id);
    return {
      ...s,
      preview: existing?.preview || s.id.slice(0, 8) + "...",
      turnCount: existing?.turnCount || 0,
      status: (existing?.status as api.Session["status"]) || "idle",
      time: s.time || undefined,
    };
  });

  if (currentSessions.length > 0) {
    // Preserve existing order; put new sessions at the top
    const currentIds = new Set(currentSessions.map(s => s.id));
    const newSessions = sessions.filter(s => !currentIds.has(s.id));
    const oldOrdered = currentSessions.map(s => sessions.find(ns => ns.id === s.id)!).filter(Boolean);
    sessions.length = 0;
    sessions.push(...newSessions, ...oldOrdered);
  } else {
    // Initial load: sort by creation time
    sessions.sort((a, b) => (b.time?.created || 0) - (a.time?.created || 0));
  }

  store.set({ sessions });
  renderSessions();
  const toEnrich = sessions.slice(0, 10);
  for (const s of toEnrich) {
    try {
      const msgData = await api.getMessages(s.id);
      const msgs = msgData.messages || [];
      const userMsg = msgs.find(m => m.role === "user");
      s.preview = userMsg ? userMsg.content.slice(0, 60) : "Empty session";
      const turns = await api.getTurns(s.id);
      s.turnCount = turns.length;
      const hasRunning = turns.some(t => t.status === "running");
      s.status = hasRunning ? "running" : turns.length > 0 ? "done" : "idle";
    } catch {
      s.preview = "Session";
    }
  }
  renderSessions();
}

// Background reconciliation. The SSE pool pushes turn_start / turn_end
// for every session in real time, so this loop's only remaining job is
// to fill in the bits the event payload doesn't carry: the user message
// used as the sidebar title. Without it, a fresh session that just had
// its first turn_start via the pool would keep showing the id stub until
// the user manually switched to it. We re-query the messages endpoint
// for any non-active session whose title is still the id-shaped stub.
const ID_STUB_RE = /^[0-9a-f]{8}\.\.\.$/;
setInterval(async () => {
  const { sessions, activeSid } = store.get();
  const needTitle = sessions.filter(s =>
    s.id !== activeSid && (!s.preview || ID_STUB_RE.test(s.preview))
  );
  for (const s of needTitle) {
    await refreshSessionPreview(s.id);
  }
}, 5000);

interface SessionGroup { label: string; sessions: api.Session[] }

function renderSessions() {
  const list = $("sessionList");
  const selectMode = list.dataset.selectMode === "true";
  // Preserve checked states before rebuild
  const checkedSids = new Set<string>();
  if (selectMode) {
    list.querySelectorAll<HTMLInputElement>(".session-checkbox:checked").forEach(cb => {
      checkedSids.add(cb.dataset.sid!);
    });
  }
  list.innerHTML = "";
  const { activeSid, sessions, config } = store.get();
  const showAll = list.dataset.showAll === "true";
  const filterEl = $("sessionSearchInput") as HTMLInputElement;
  const filter = filterEl ? filterEl.value.toLowerCase().trim() : "";

  // Filter
  const filtered = filter
    ? sessions.filter(s => (s.preview || s.id).toLowerCase().includes(filter))
    : sessions;

  const MAX_COLLAPSED = 15;
  const toShow = (!showAll && !filter) ? filtered.slice(0, MAX_COLLAPSED) : filtered;

  // Project folder (single workspace)
  const workspaceName = (config.workspace || "ziva").split("/").pop() || "Project";
  const projectDiv = document.createElement("div");
  projectDiv.className = "session-project-group";
  projectDiv.innerHTML = `
    <details open>
      <summary class="project-summary">
        <span class="project-chevron">▸</span>
        <span class="project-name">${esc(workspaceName)}</span>
        <span class="project-count">${filtered.length}</span>
      </summary>
      <div class="project-sessions"></div>
    </details>`;

  const sessionsContainer = projectDiv.querySelector(".project-sessions")!;

  for (const s of toShow) {
    const div = document.createElement("div");
    div.className = "session-item" + (s.id === activeSid ? " active" : "");
    const timeStr = formatRelativeTime(s.time?.updated || s.time?.created);
    div.innerHTML = `
      ${selectMode ? `<input type="checkbox" class="session-checkbox" data-sid="${s.id}" />` : ""}
      <span class="session-chevron">›</span>
      <span class="session-name">${esc(s.preview || s.id)}</span>
      <span class="session-time">${timeStr}</span>
      ${!selectMode ? `<span class="del-btn" data-sid="${s.id}">&times;</span>` : ""}`;
    div.onclick = (e) => {
      if ((e.target as HTMLElement).classList.contains("del-btn")) return;
      if ((e.target as HTMLElement).classList.contains("session-checkbox")) return;
      switchSession(s.id);
    };
    const nameEl = div.querySelector(".session-name") as HTMLElement;
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      nameEl.contentEditable = "true";
      nameEl.focus();
      const range = document.createRange();
      range.selectNodeContents(nameEl);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
    });
    nameEl.addEventListener("blur", async () => {
      nameEl.contentEditable = "false";
      const newName = nameEl.textContent?.trim();
      if (newName && newName !== s.preview) {
        try { await api.updateSession(s.id, { name: newName }); } catch { /* ignore */ }
        s.preview = newName;
      }
    });
    nameEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); nameEl.blur(); }
      if (e.key === "Escape") { nameEl.textContent = s.preview || s.id; nameEl.blur(); }
    });
    const delBtn = div.querySelector(".del-btn");
    if (delBtn) {
      (delBtn as HTMLElement).onclick = (e) => {
        e.stopPropagation();
        if (confirm("Delete this session?")) deleteSession(s.id);
      };
    }
    sessionsContainer.appendChild(div);
  }

  // Restore checked states after rebuild
  if (selectMode && checkedSids.size > 0) {
    sessionsContainer.querySelectorAll<HTMLInputElement>(".session-checkbox").forEach(cb => {
      if (checkedSids.has(cb.dataset.sid!)) cb.checked = true;
    });
  }

  list.appendChild(projectDiv);

  if (!showAll && !filter && filtered.length > MAX_COLLAPSED) {
    const btn = document.createElement("button");
    btn.className = "show-all-btn";
    btn.textContent = `Show all ${filtered.length} conversations`;
    btn.onclick = () => { list.dataset.showAll = "true"; renderSessions(); };
    list.appendChild(btn);
  } else if (showAll && !filter) {
    const btn = document.createElement("button");
    btn.className = "show-all-btn";
    btn.textContent = "Show recent only";
    btn.onclick = () => { list.dataset.showAll = "false"; renderSessions(); };
    list.appendChild(btn);
  }
}

async function createSession() {
  const id = await api.createSession();
  const sessions = [...store.get().sessions];
  sessions.unshift({ id, turnCount: 0, status: "idle", preview: "Empty session" });
  store.set({ sessions });
  renderSessions();
  await switchSession(id);
}

async function switchSession(sid: string) {
  closeAllFullpageOverlays();
  // Per-session state (running / pending) lives in maps keyed by
  // sid, so switching sessions doesn't lose background work and
  // can't leak the previous session's flags into the new one. Only
  // activeSid + questionPending (which is question-card specific)
  // get reset.
  store.set({ activeSid: sid, questionPending: false });
  renderSessions();
  $("messages").innerHTML = "";
  currentAssistantEl = null;
  currentTextParts = { thinking: "", main: "" };
  pendingTools.forEach(c => c.remove());
  pendingTools.clear();
  renderPendingBar();
  await loadHistory(sid);

  try {
    const turns = await api.getTurns(sid);
    const activeTurn = turns.find(t => t.status === "running");
    if (activeTurn) {
      setActiveRunning(true);
      // Detect pending ask_user calls from the message history.
      // An ask_user is pending if there's an assistant message with an
      // ask_user tool_call but no following tool result for that call_id.
      // renderMessages already renders answered cards from tool results;
      // here we only handle the interactive case (unanswered question).
      const msgs = (await api.getMessages(sid)).messages || [];
      const answeredIds = new Set<string>();
      for (const m of msgs) {
        if (m.role === "tool" && m.name === "ask_user" && m.tool_call_id) {
          answeredIds.add(m.tool_call_id);
        }
      }
      for (const m of msgs) {
        const tcs = m.tool_calls || [];
        for (const tc of tcs) {
          if (tc.name === "ask_user" && tc.id && !answeredIds.has(tc.id)) {
            const args = tc.arguments || {};
            appendQuestionCard(
              String(args.question || ""),
              (args.options as string[]) || [],
              !!args.multi_select,
              tc.id,
            );
          }
        }
      }
      // The turn is still running — show the typing indicator so the
      // user sees the session as active, not idle.
      appendTyping();
    }
  } catch (e) {
    console.error("Failed to fetch running turn events:", e);
  }

  updateSendStopButton();
  refreshPlan();
  if ($("rightPanel").classList.contains("show")) refreshDiff();
}

async function deleteSession(sid: string) {
  await api.deleteSession(sid);
  const sessions = store.get().sessions.filter(s => s.id !== sid);
  store.set({ sessions });
  if (store.get().activeSid === sid) {
    store.set({ activeSid: null });
    $("messages").innerHTML = "";
    showEmptyState(true);
  }
  renderSessions();
}

async function loadHistory(sid: string) {
  // Always load the full history (include_dropped=true) so the collapse bar
  // can count how many pre-compact messages were dropped. The filtered view
  // (post-/compact) returns only the summary message, which is what we want
  // to render at the top of the chat.
  const [filteredData, fullData] = await Promise.all([
    api.getMessages(sid),
    api.getMessages(sid, { includeDropped: true }),
  ]);
  const msgs = filteredData.messages || [];
  const fullMsgs = fullData.messages || [];
  const hasContent = msgs.length > 0;
  showEmptyState(!hasContent);

  // Clear existing messages before rebuilding
  $("messages").innerHTML = "";
  currentAssistantEl = null;
  currentTextParts = { thinking: "", main: "" };
  pendingTools.forEach(c => c.remove());
  pendingTools.clear();

  // Restore context ring from persisted usage
  if (filteredData.last_usage?.prompt_tokens) {
    const pct = Math.min(filteredData.last_usage.prompt_tokens / 200000, 1);
    updateContextProgress(pct, filteredData.last_usage.prompt_tokens);
  } else {
    updateContextProgress(0, 0);
  }

  // If the session has been compacted, the on-disk layout is
  //   [summary, u1, a1, u2, a2, ...]  (summary at index 0, originals after)
  // and the filtered view returns just the summary. We render a collapse
  // bar above it counting ALL pre-/compact messages (the compacted
  // originals), so the visible UI shows only the summary until the user
  // expands the bar.
  const firstSummaryIdx = fullMsgs.findIndex(m => (m as any)._compaction_summary);
  if (firstSummaryIdx >= 0) {
    const droppedCount = fullMsgs.length - 1;
    appendCompactBoundary(sid, droppedCount, firstSummaryIdx);
  }

  renderMessages($("messages"), msgs);
  scrollBottom();
}

// Render a list of messages into a target container using the same DOM
// construction as the live chat (user / assistant / tool cards). Used
// both by `loadHistory` (target = #messages) and by the compact-history
// expand affordance (target = .compact-dropped inside the collapse bar),
// so the folded messages look identical to the live chat — just visually
// scaled down via the wrapper's CSS.
function renderMessages(target: HTMLElement, msgs: any[]): void {
  let pendingToolCalls: { id: string; name: string; arguments: Record<string, unknown> }[] = [];

  for (const m of msgs) {
    const isSub = (m as any)._subagent === true;

    if (m.role === "user") {
      appendUserMsg(m.content, target);
    } else if (m.role === "assistant") {
      if (isSub) {
        continue;
      }
      const toolCalls = (m as any).tool_calls as { id: string; name: string; arguments: Record<string, unknown> }[] | undefined;
      if (toolCalls && toolCalls.length > 0) {
        pendingToolCalls = toolCalls;
        const { thinking } = extractThinking(m.content);
        if (thinking) {
          const thinkDiv = document.createElement("div");
          thinkDiv.className = "thinking-card-inline";
          thinkDiv.innerHTML = `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
          target.appendChild(thinkDiv);
        }
      } else {
        appendAssistantMsg(m.content, target);
        pendingToolCalls = [];
      }
    } else if (m.role === "tool") {
      const toolName = (m as any).name || "unknown";

      if (isSub) {
        continue;
      }

      const toolCallId = (m as any).tool_call_id as string | undefined;
      let args: Record<string, unknown> = {};
      if (toolCallId) {
        const match = pendingToolCalls.find(tc => tc.id === toolCallId);
        if (match) args = match.arguments;
      }
      let output: unknown = m.content;
      // Pruned tool messages keep the call structure but collapse the payload
      // to a placeholder string. Render them as a special state rather than
      // trying to parse "[pruned]" as JSON.
      const isPruned = typeof m.content === "string" && m.content === "[pruned]";
      if (!isPruned) {
        try { output = JSON.parse(m.content); } catch {}
      }

      let subagentTools: string[] | undefined;
      if (!isPruned && toolName === "spawn_agent" && typeof output === "object" && output !== null) {
        subagentTools = (output as any).tools;
      }

      // ask_user is rendered as an answered question card, not a tool card.
      // The tool result content is like {"status":"answered","answer":"..."}.
      // We pull question/options/multiSelect from the matching tool_call args.
      if (toolName === "ask_user") {
        let answer = "";
        if (typeof output === "object" && output !== null) {
          answer = String((output as any).answer || "");
        } else if (typeof m.content === "string") {
          try { answer = JSON.parse(m.content).answer || ""; } catch {}
        }
        const q = String(args.question || "");
        const opts = (args.options as string[]) || [];
        const ms = !!args.multi_select;
        if (q) {
          const card = document.createElement("div");
          card.className = "question-card question-card-answered";
          card.innerHTML = `<div class="question-text">${esc(q)}</div><div class="question-reply">You: ${esc(answer)}</div>`;
          target.appendChild(card);
        }
        continue;
      }

      appendToolCard(toolName, args, "success", output, subagentTools, isPruned, target);
    }
  }
}

// Render a collapse bar above a compaction summary message. On expand,
// it fetches the full history (with include_dropped=true) and renders
// the compacted originals inline above the bar using the same DOM as
// the live chat (renderMessages → appendUserMsg / appendAssistantMsg /
// appendToolCard), so the folded messages look identical to the live
// chat — just visually scaled down via the wrapper's CSS. The summary
// itself stays in its own bubble below the bar.
function appendCompactBoundary(
  sid: string,
  droppedCount: number,
  summaryIdx: number,
): void {
  const wrapper = document.createElement("div");
  wrapper.className = "compact-boundary";

  const bar = document.createElement("details");
  bar.className = "compact-collapse";
  const sum = document.createElement("summary");
  sum.textContent = `📚 之前 ${droppedCount} 条消息已压缩为摘要`;
  bar.appendChild(sum);

  const dropZone = document.createElement("div");
  dropZone.className = "compact-dropped";
  dropZone.textContent = "展开以查看原文…";
  bar.appendChild(dropZone);

  bar.addEventListener("toggle", async () => {
    if (!bar.open || dropZone.dataset.loaded === "1") return;
    try {
      const data = await api.getMessages(sid, { includeDropped: true });
      const fullMsgs = data.messages || [];
      // On-disk layout is [summary, ...compacted_originals]. Skip the
      // summary at summaryIdx and render every compacted original using
      // the same DOM as the live chat. The wrapper's CSS makes them
      // slightly indented and smaller so they're visually distinct.
      dropZone.innerHTML = "";
      const originals = fullMsgs.filter((_, i) => i !== summaryIdx);
      renderMessages(dropZone, originals);
      dropZone.dataset.loaded = "1";
    } catch (e) {
      dropZone.textContent = `加载失败: ${(e as Error).message}`;
    }
  });

  wrapper.appendChild(bar);
  $("messages").appendChild(wrapper);
}

// ---- Chat Rendering ----
// `target` defaults to `#messages` for the live streaming path. The
// compact-history expand affordance passes a different container so the
// folded messages reuse the same DOM (and styling) as the live chat,
// just visually scaled down via a wrapper class.
function appendUserMsg(text: string, target: HTMLElement = $("messages")) {
  showEmptyState(false);
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> You</div><div class="md">${renderMarkdown(text)}</div></div>`;
  target.appendChild(div);
  currentAssistantEl = null;
  highlightCode(div);
}

function appendAssistantMsg(text: string, target: HTMLElement = $("messages")) {
  const div = document.createElement("div");
  div.className = "msg assistant";
  const { thinking, main } = extractThinking(text);
  let content = "";
  if (thinking) {
    content += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
  }
  content += `<div class="md">${renderMarkdown(main)}</div>`;
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div>${content}</div>`;
  target.appendChild(div);
  addCopyButtons(div);
  highlightCode(div);
  currentAssistantEl = null;
}

function getOrCreateAssistantEl(): HTMLElement {
  if (!currentAssistantEl) {
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div><div class="md"></div></div>`;
    $("messages").appendChild(div);
    currentAssistantEl = div.querySelector(".md") as HTMLElement;
    currentTextParts = { thinking: "", main: "" };
    (currentAssistantEl as any)._main = "";
  }
  return currentAssistantEl!;
}

function appendTyping() {
  if ($("messages").querySelector(".typing-indicator")) return;
  const el = document.createElement("div");
  el.className = "typing-indicator";
  el.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div> Thinking...';
  $("messages").appendChild(el);
}

function removeTyping() {
  const el = $("messages").querySelector(".typing-indicator");
  if (el) el.remove();
}

function getAbbreviatedArg(args: Record<string, unknown>): string {
  const truncate = (s: string, max = 60) => s.length > max ? s.slice(0, max) + "..." : s;
  if (args.command) return truncate(String(args.command));
  if (args.file_path) return truncate(String(args.file_path));
  if (args.pattern) return truncate(String(args.pattern));
  if (args.query) return truncate(String(args.query));
  if (args.url) return truncate(String(args.url));
  if (args.prompt) {
    const p = String(args.prompt);
    return p.length > 40 ? p.slice(0, 40) + "..." : p;
  }
  if (args.task) return truncate(String(args.task));
  if (args.name) return truncate(String(args.name));
  return JSON.stringify(args).slice(0, 50);
}

function appendToolCard(
  toolName: string,
  args: Record<string, unknown>,
  status: string,
  output?: unknown,
  subagentTools?: string[],
  isPruned: boolean = false,
  target: HTMLElement = $("messages"),
): HTMLElement {
  const card = document.createElement("div");
  card.className = "tool-card" + (status === "running" ? " open" : "") + (isPruned ? " pruned" : "");
  const statusClass = isPruned ? "pruned" : (status === "error" ? "error" : status === "running" ? "running" : "success");
  const statusText = isPruned ? "pruned" : (status === "error" ? "error" : status === "running" ? "running..." : "done");
  const abbrevArg = getAbbreviatedArg(args);

  let body = "";
  if (Object.keys(args).length > 0) {
    body += `<div class="section-label">Input</div>`;
    body += `<div class="section-content"><code>${esc(JSON.stringify(args, null, 2))}</code></div>`;
  }
  if (isPruned) {
    body += `<div class="section-label">Output</div>`;
    body += `<div class="section-content pruned-output">Output pruned to save context — re-run the tool to see fresh results.</div>`;
  } else if (output !== undefined) {
    if (toolName === "spawn_agent" && typeof output === "object" && output !== null && (output as any).result) {
      const resultText = String((output as any).result);
      body += `<div class="section-label">Output</div>`;
      body += `<div class="section-content">${renderMarkdown(resultText)}</div>`;
    } else {
      const outStr = typeof output === "string" ? output : JSON.stringify(output, null, 2);
      body += `<div class="section-label">Output</div>`;
      body += `<div class="section-content"><code>${esc(outStr)}</code></div>`;
    }
  }
  if (subagentTools && subagentTools.length > 0) {
    body += `<div class="section-label">used tools</div>`;
    body += `<div class="section-content subagent-tools">${subagentTools.map((t) => `<div class="subagent-tool-item"><span>⚙ ${esc(t)}</span></div>`).join("")}</div>`;
  }

  card.innerHTML = `
    <div class="tool-card-header">
      <span class="tool-icon">⚙</span>
      <span class="tool-name">${esc(toolName)}</span>
      <span class="tool-args">${esc(abbrevArg)}</span>
      <span class="tool-status"><span class="status-dot ${statusClass}"></span>${statusText}</span>
      <button class="copy-tool-btn" title="Copy">⧉</button>
    </div>
    <div class="tool-card-body">${body}</div>`;

  (card.querySelector(".tool-card-header") as HTMLElement).onclick = () => card.classList.toggle("open");
  (card.querySelector(".copy-tool-btn") as HTMLElement).onclick = (e) => {
    e.stopPropagation();
    const copyText = `${toolName} - ${JSON.stringify(args)}${output !== undefined ? "\nOutput: " + (typeof output === "string" ? output : JSON.stringify(output)) : ""}`;
    navigator.clipboard.writeText(copyText);
  };

  target.appendChild(card);
  currentAssistantEl = null;
  return card;
}

function appendApprovalCard(requestId: string, toolName: string, args: Record<string, unknown>) {
  const card = document.createElement("div");
  card.className = "approval-card";
  const argsStr = JSON.stringify(args, null, 2);
  card.innerHTML = `
    <div class="approval-header">! Approval Required</div>
    <div class="approval-detail">Tool: <strong>${esc(toolName)}</strong><br/><pre>${esc(argsStr.slice(0, 500))}${argsStr.length > 500 ? "..." : ""}</pre></div>
    <div class="approval-actions">
      <button class="approve-once">Allow Once</button>
      <button class="approve-always">Allow Always</button>
      <button class="deny">Deny</button>
    </div>`;
  (card.querySelector(".approve-once") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "once");
    card.remove();
  };
  (card.querySelector(".approve-always") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "always_session");
    card.remove();
  };
  (card.querySelector(".deny") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "reject");
    card.remove();
  };
  $("messages").appendChild(card);
  currentAssistantEl = null;
}

function appendQuestionCard(question: string, options: string[], multiSelect: boolean = false, callId?: string) {
  showEmptyState(false);
  const card = document.createElement("div");
  card.className = "question-card";
  let html = `<div class="question-text">${esc(question)}</div>`;
  if (options.length > 0) {
    if (multiSelect) {
      html += `<div class="question-options">${options.map((o, i) =>
        `<label class="question-checkbox-label"><input type="checkbox" class="question-checkbox" data-opt="${i}" value="${esc(o)}" /><span>${esc(o)}</span></label>`
      ).join("")}</div>`;
      html += `<div class="question-input-row question-other-row">
        <input type="text" class="question-input" placeholder="Or type your own answer..." />
        <button class="question-submit" aria-label="Submit">↑</button>
      </div>`;
    } else {
      html += `<div class="question-options">${options.map((o, i) =>
        `<button class="question-option-btn" data-opt="${i}">${esc(o)}</button>`
      ).join("")}</div>`;
      html += `<div class="question-input-row question-other-row">
        <input type="text" class="question-input" placeholder="Or type your own answer..." />
        <button class="question-submit" aria-label="Send">↑</button>
      </div>`;
    }
  } else {
    html += `<div class="question-input-row">
      <input type="text" class="question-input" placeholder="Type your answer..." />
      <button class="question-submit" aria-label="Send">↑</button>
    </div>`;
  }
  // "Chat about this" affordance — lets the user back out of the
  // question entirely and re-discuss with the agent. We render it
  // as a small secondary button below the input row: visible enough
  // that users notice it, but visually subordinate to the main
  // options so the option click-rate stays high.
  html += `<div class="question-footer">
    <button class="question-chat-about" type="button">
      <svg class="question-chat-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <span>还是聊聊吧</span>
    </button>
  </div>`;
  card.innerHTML = html;

  let submitted = false;
  const lockCard = (answer: string) => {
    submitted = true;
    card.querySelectorAll(".question-input-row").forEach(el => el.remove());
    card.querySelector(".question-options")?.remove();
    card.querySelector(".question-footer")?.remove();
    card.classList.add("question-card-answered");
    const replyDiv = document.createElement("div");
    replyDiv.className = "question-reply";
    replyDiv.textContent = `You: ${answer}`;
    card.appendChild(replyDiv);
  };

  const submit = (answer: string) => {
    if (submitted) return;
    const trimmed = answer.trim();
    if (!trimmed) return;
    const activeSid = store.get().activeSid;
    if (!activeSid) return;
    // Resolve the pending ask_user future on the backend instead of
    // starting a brand-new turn — the original model round is still
    // waiting for our answer.
    api.replyQuestion(activeSid, trimmed, callId).catch((e) => {
      console.error("replyQuestion failed:", e);
    });
    lockCard(trimmed);
  };

  if (multiSelect && options.length > 0) {
    const sendBtn = card.querySelector(".question-submit") as HTMLElement;
    const input = card.querySelector(".question-input") as HTMLInputElement;
    const doSubmit = () => {
      if (submitted) return;
      const text = (input?.value || "").trim();
      if (text) { submit(text); return; }
      const checked = card.querySelectorAll<HTMLInputElement>(".question-checkbox:checked");
      const selected = Array.from(checked).map(cb => cb.value);
      submit(JSON.stringify(selected));
    };
    sendBtn.onclick = doSubmit;
    input?.addEventListener("keydown", (e) => { if (e.key === "Enter") doSubmit(); });
  } else if (options.length > 0) {
    card.querySelectorAll<HTMLElement>(".question-option-btn").forEach((btn) => {
      btn.addEventListener("click", () => submit(btn.textContent || ""));
    });
  }
  if (!(multiSelect && options.length > 0)) {
    const input = card.querySelector(".question-input") as HTMLInputElement;
    const sendBtn = card.querySelector(".question-submit") as HTMLElement;
    if (sendBtn && input) {
      sendBtn.onclick = () => submit(input.value);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(input.value); });
    }
    input?.focus();
  } else {
    card.querySelector(".question-input")?.focus();
  }

  const chatAboutBtn = card.querySelector(".question-chat-about") as HTMLElement;
  chatAboutBtn.onclick = () => {
    if (submitted) return;
    const activeSid = store.get().activeSid;
    if (!activeSid) return;
    api.replyQuestion(activeSid, "（用户放弃当前选项，希望直接讨论这个话题）", callId).catch((e) => {
      console.error("replyQuestion failed:", e);
    });
    lockCard("还是聊聊吧");
    // Focus the chat composer so the user can immediately type
    // their new direction.
    setTimeout(() => {
      const prompt = document.getElementById("prompt") as HTMLTextAreaElement | null;
      prompt?.focus();
    }, 50);
  };

  $("messages").appendChild(card);
  // Mark the turn as still running: the model round is suspended
  // waiting on the user, not idle. questionPending lets the Stop
  // button know to resolve the question as "user abandoned" rather
  // than cancelling the entire turn.
  store.set({ questionPending: true });
  setActiveRunning(true);
  const clearPending = () => store.set({ questionPending: false });
  // Watch the card for the .question-card-answered / .question-card-cancelled
  // classes — once applied, drop the questionPending flag.
  const observer = new MutationObserver(() => {
    if (card.classList.contains("question-card-answered") ||
        card.classList.contains("question-card-cancelled")) {
      clearPending();
      observer.disconnect();
    }
  });
  observer.observe(card, { attributes: true, attributeFilter: ["class"] });
  // Safety net — if the card is removed from the DOM without ever
  // getting answered (e.g. session switched), clear the flag too.
  const removalObserver = new MutationObserver(() => {
    if (!card.isConnected) {
      clearPending();
      removalObserver.disconnect();
    }
  });
  removalObserver.observe($("messages"), { childList: true });
  updateSendStopButton();
  scrollBottom();
}

/**
 * Re-insert answered question cards after `loadHistory` rebuilds the
 * chat DOM. For each answered question we saved on this session, we
 * look up the user message whose text starts with the snippet we
 * stored at lockCard time and place the answered card right after
 * it. If no match is found (e.g. the user message was compacted
 * away), we append the card to the end so the user still sees that
 * the question was answered.
 */
function appendError(msg: string) {
  const div = document.createElement("div");
  div.className = "error-card";
  div.textContent = "Error: " + msg;
  $("messages").appendChild(div);
  currentAssistantEl = null;
}

// ---- Compact progress toast (aicoder-aligned) ----
// Fixed-position popup with circular spinner shown while /compact runs.
// Replaces the previous in-flow `.msg.compacting` element.
function ensureCompactToast(): HTMLElement {
  let toast = document.getElementById("compact-toast");
  if (toast) {
    toast.classList.remove("hidden");
    return toast;
  }
  toast = document.createElement("div");
  toast.id = "compact-toast";
  toast.className = "compact-toast";
  toast.innerHTML = `
    <div class="compact-spinner"></div>
    <span class="compact-toast-message"></span>
  `;
  // Append inside the message column so `position: absolute` centers the
  // toast over the chat area instead of the whole viewport (which would
  // put it on top of the sidebar).
  const host = document.querySelector(".ziva-center") || document.body;
  host.appendChild(toast);
  return toast;
}

function setCompactToastState(state: "loading" | "success" | "error", message: string): void {
  const toast = ensureCompactToast();
  const spinner = toast.querySelector(".compact-spinner") as HTMLElement | null;
  const msg = toast.querySelector(".compact-toast-message") as HTMLElement | null;
  if (spinner) {
    spinner.classList.remove("success", "error");
    if (state !== "loading") spinner.classList.add(state);
  }
  if (msg) msg.textContent = message;
}

function hideCompactToast(): void {
  const toast = document.getElementById("compact-toast");
  if (toast) toast.classList.add("hidden");
}

function scrollBottom() {
  if (store.get().autoScroll) $("messages").scrollTop = $("messages").scrollHeight;
}

// ---- SSE Event Handling ----

// Single global SSE connection delivers events for every session. Each
// event carries a `session_id` field (set by the server's runtime._emit),
// which we use to route to the right handler. For the active session we
// render live; for background sessions we only sync the sidebar + per-
// session running flag — the chat DOM is rebuilt from history on switch.
function routeSSEEvent(ev: api.Event) {
  const sid = (ev as any).session_id as string | undefined;
  if (!sid) return;
  const { activeSid } = store.get();
  if (sid === activeSid) {
    handleEvent(ev, true);
  } else {
    syncBackgroundSession(sid, ev);
  }
}

function syncBackgroundSession(sid: string, ev: api.Event) {
  const t = ev.type as string;
  const { sessions, runningSessions } = store.get();
  const s = sessions.find(x => x.id === sid);
  if (!s) return;

  if (t === "turn_start") {
    s.status = "running";
    const next = { ...runningSessions, [sid]: true };
    store.set({ sessions: [...sessions], runningSessions: next });
    renderSessions();
    // The server's `turn_start` event doesn't carry the user message
    // body, so we can't update the sidebar title from the event
    // payload. Fetch the session's first user message and use it as
    // the preview so the sidebar shows the actual question, not the
    // session id stub.
    refreshSessionPreview(sid);
  } else if (t === "turn_end" || t === "turn_cancelled" || t === "turn_failed") {
    s.status = t === "turn_failed" ? "failed" : (t === "turn_cancelled" ? "idle" : "done");
    const next = { ...runningSessions };
    delete next[sid];
    // If the user queued a message in this background session, the
    // active-session turn_end flush path can't reach it (we're not on
    // its sid). Drop the queue chip here so the user doesn't see
    // "排队中" persisting forever on a session they may have switched
    // away from. The text is gone — they can re-type if they still
    // need it after switching back.
    const { pendingMessages } = store.get();
    let nextPending = pendingMessages;
    if (pendingMessages[sid] != null) {
      nextPending = { ...pendingMessages };
      delete nextPending[sid];
    }
    store.set({ sessions: [...sessions], runningSessions: next, pendingMessages: nextPending });
    renderSessions();
  }
}

async function refreshSessionPreview(sid: string) {
  try {
    const data = await api.getMessages(sid);
    const userMsg = (data.messages || []).find(m => m.role === "user");
    if (!userMsg) return;
    const preview = userMsg.content.slice(0, 60);
    const { sessions } = store.get();
    const s = sessions.find(x => x.id === sid);
    if (!s || s.preview === preview) return;
    s.preview = preview;
    store.set({ sessions: [...sessions] });
    renderSessions();
  } catch { /* ignore — preview is best-effort */ }
}

// Replay any persisted events from a still-running turn on switch
// (the live global stream will then keep streaming new events for that sid).
async function replayRunningTurn(sid: string) {
  try {
    const turns = await api.getTurns(sid);
    const activeTurn = turns.find(t => t.status === "running");
    if (activeTurn) {
      setActiveRunning(true);
      if (activeTurn.events) {
        for (const ev of activeTurn.events) {
          handleEvent(ev, false);
        }
        scrollBottom();
      }
    }
  } catch (e) {
    console.error("Failed to fetch running turn events:", e);
  }
}

// One global subscription covers every session. Events for the active
// sid render live; events for any other sid drive sidebar status only.
sse.subscribe(routeSSEEvent);

function handleEvent(ev: api.Event, updateScroll: boolean = true) {
  // Skip all sub-agent events — they are shown in a collapsed card, not individually
  if ((ev as any)._subagent) return;

  const t = ev.type as string;

  if (t === "turn_start") {
    setActiveRunning(true);
    const { sessions, activeSid } = store.get();
    const active = sessions.find(s => s.id === activeSid);
    if (active) {
      active.status = "running";
      store.set({ sessions: [...sessions] });
      renderSessions();
    }
    updateSendStopButton();
  } else if (t === "delta") {
    removeTyping();
    showEmptyState(false);
    const el = getOrCreateAssistantEl();
    const content = (ev.content as string) || "";
    (el as any)._main += content;
    const { thinking, main } = extractThinking((el as any)._main);
    let html = "";
    if (thinking) {
      html += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
    }
    html += renderMarkdown(main);
    el.innerHTML = html;
    addCopyButtons(el.parentElement!);
    highlightCode(el.parentElement!);
    if (updateScroll) scrollBottom();
  } else if (t === "model_response") {
    // Final full response; ensure _main matches exactly to avoid drift from deltas
    removeTyping();
    showEmptyState(false);
    const el = getOrCreateAssistantEl();
    const content = (ev.content as string) || "";
    (el as any)._main = content;
    const { thinking, main } = extractThinking((el as any)._main);
    let html = "";
    if (thinking) {
      html += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
    }
    html += renderMarkdown(main);
    el.innerHTML = html;
    addCopyButtons(el.parentElement!);
    highlightCode(el.parentElement!);
    if (updateScroll) scrollBottom();
  } else if (t === "ask_user_question") {
    removeTyping();
    const q = String((ev.question as string) || "");
    const opts = ((ev.options as string[]) || []) as string[];
    const ms = !!ev.multi_select;
    const cid = (ev.call_id as string) || undefined;
    // Skip if renderMessages already rendered an answered card for this
    // question (happens when replaying a running turn whose earlier
    // ask_user calls have already been answered and persisted as tool
    // results in the message history).
    const existing = $("messages").querySelectorAll(".question-card-answered .question-text");
    const alreadyAnswered = Array.from(existing).some(el => (el.textContent || "").trim() === q);
    if (!alreadyAnswered) {
      appendQuestionCard(q, opts, ms, cid);
    }
    if (updateScroll) scrollBottom();
  } else if (t === "tool_start") {
    removeTyping();
    const key = `${ev.round}:${ev.call_id || ev.tool}`;
    const card = appendToolCard(ev.tool as string, (ev.arguments || {}) as Record<string, unknown>, "running");
    pendingTools.set(key, card);
    if (updateScroll) scrollBottom();
  } else if (t === "tool_end") {
    const key = `${ev.round}:${ev.call_id || ev.tool}`;
    const pending = pendingTools.get(key);
    if (pending) {
      pendingTools.delete(key);
      pending.remove();
    }
    if (ev.tool === "ask_user") {
      // Card is already on screen via the earlier `ask_user_question`
      // event. Don't create a second one — the user has already (or
      // is about to) answer it.
    } else {
      const status = ev.error_class ? "error" : "success";
      let subagentTools: string[] | undefined;
      if (ev.tool === "spawn_agent") {
        const output = ev.output || {};
        subagentTools = output.tools;
      }
      appendToolCard(ev.tool as string, (ev.arguments || {}) as Record<string, unknown>, status, ev.output, subagentTools);
    }
    if (updateScroll) scrollBottom();
  } else if (t === "permission_request" || t === "approval_request") {
    removeTyping();
    const req = (ev.request || ev) as Record<string, unknown>;
    const tool = (req.tool || {}) as Record<string, unknown>;
    const requestId = (req.id || req.request_id || "") as string;
    const toolName = (tool.name || req.tool_name || "unknown") as string;
    const args = (tool.arguments || req.arguments || {}) as Record<string, unknown>;
    appendApprovalCard(requestId, toolName, args);
    if (updateScroll) scrollBottom();
  } else if (t === "turn_end") {
    // Trust the live streaming events to have already rendered the new
    // user / assistant / tool messages. We previously called `loadHistory`
    // here to "reconcile" with disk, but that wiped the entire message
    // container and re-rendered from scratch on every turn — a jarring
    // "flash" of the full conversation. We only refresh lightweight
    // sidebar / context surfaces that don't drive the chat display.
    removeTyping();
    // Any question card still on screen is now abandoned — the round
    // closed without an answer (probably cancelled). Lock its inputs
    // so the user can't submit a reply that will land in a new turn.
    document.querySelectorAll(".question-card:not(.question-card-answered)").forEach((el) => {
      el.classList.add("question-card-cancelled");
      (el.querySelectorAll("input, button") as NodeListOf<HTMLElement>).forEach((b) => {
        b.disabled = true;
      });
    });
    setActiveRunning(false);
    currentAssistantEl = null;
    updateSendStopButton();
    refreshPlan();
    refreshSessions();
    if ($("rightPanel").classList.contains("show")) refreshDiff();
    // Codex-style: flush this session's queued prompt now that the
    // turn has closed. Look up by the active sid (the SSE stream is
    // per-session, so we never flush another session's queue here).
    const pending = getActivePending();
    if (pending != null) {
      const promptEl = $("prompt") as HTMLTextAreaElement;
      promptEl.value = pending;
      promptEl.style.height = "auto";
      promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + "px";
      $("charCount").textContent = String(promptEl.value.length);
      setActivePending(null);
      renderPendingBar();
      setTimeout(() => { sendMessage(); }, 30);
    }
  } else if (t === "round_complete") {
    currentAssistantEl = null;
    const usage = ev.usage as { prompt_tokens?: number; completion_tokens?: number } | undefined;
    if (usage?.prompt_tokens) {
      const contextWindow = 200000;
      const pct = Math.min(usage.prompt_tokens / contextWindow, 1);
      updateContextProgress(pct, usage.prompt_tokens);
    }
  } else if (t === "context_compacted") {
    console.log("Context compacted at round", ev.round);
  } else if (t === "doom_loop_detected") {
    removeTyping();
  } else if (t === "turn_error") {
    removeTyping();
    setActiveRunning(false);
    updateSendStopButton();
    appendError(ev.error as string || "Unknown error");
  }

  updateConnStatus(sse.isConnected(store.get().activeSid || ""));
}

function updateConnStatus(connected: boolean) {
  store.set({ connected });
}

function updateSendStopButton() {
  const btn = $("btnSend");
  if (isActiveRunning()) {
    btn.textContent = "■";
    btn.className = "stop-btn";
    btn.title = "Stop";
  } else {
    btn.textContent = "→";
    btn.className = "send-btn";
    btn.title = "Send";
  }
}

function updateContextProgress(pct: number, tokens: number) {
  const arc = $("contextArc");
  const pctLabel = $("contextPct");
  const normalizedPct = Math.max(0, Math.min(pct, 1));
  const circumference = 69.12; // 2 * π * 11
  const offset = circumference * (1 - normalizedPct);
  arc.setAttribute("stroke-dashoffset", String(offset));
  // Color: green → yellow → red
  if (normalizedPct > 0.85) arc.setAttribute("stroke", "var(--red)");
  else if (normalizedPct > 0.6) arc.setAttribute("stroke", "var(--orange)");
  else arc.setAttribute("stroke", "var(--accent)");
  pctLabel.textContent = Math.round(normalizedPct * 100) + "%";
}

// ---- Queue (Codex-style) ----
// While a turn is running, Enter / send-button stashes the typed text
// into the active session's queue instead of opening a parallel turn.
// The `turn_end` event flushes it. The user sees a chip above the
// composer with a one-click edit / clear affordance. Per-session —
// background sessions keep their own queues untouched.
function queuePromptMessage() {
  const promptEl = $("prompt") as HTMLTextAreaElement;
  const text = promptEl.value;
  const trimmed = text.trim();
  if (!trimmed) return;
  setActivePending(text);
  promptEl.value = "";
  promptEl.style.height = "auto";
  $("charCount").textContent = "";
  renderPendingBar();
}

function clearPendingMessage() {
  setActivePending(null);
  renderPendingBar();
}

function editPendingMessage() {
  const pending = getActivePending();
  if (pending == null) return;
  const promptEl = $("prompt") as HTMLTextAreaElement;
  promptEl.value = pending;
  promptEl.style.height = "auto";
  promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + "px";
  $("charCount").textContent = String(promptEl.value.length);
  setActivePending(null);
  renderPendingBar();
  promptEl.focus();
}

function renderPendingBar() {
  const bar = $("pendingBar");
  const text = $("pendingBarText");
  if (!bar || !text) return;
  const pending = getActivePending();
  if (pending == null) {
    bar.hidden = true;
    text.textContent = "";
    return;
  }
  // Truncate the preview so a long queued message doesn't blow up
  // the composer's height. Full content is in the prompt when the
  // user clicks the chip to edit.
  const preview = pending.length > 80
    ? pending.slice(0, 80) + "…"
    : pending;
  text.textContent = preview;
  text.title = pending;
  bar.hidden = false;
}

// ---- Cancel ----
async function cancelTurn() {
  const { activeSid } = store.get();
  if (!activeSid) return;
  // Cancel the turn outright. Lock any pending question cards so
  // the user sees them as cancelled, then send the cancel API call.
  const pendingCards = document.querySelectorAll(".question-card:not(.question-card-answered):not(.question-card-cancelled)");
  pendingCards.forEach(card => {
    (card as HTMLElement).querySelector(".question-input-row")?.remove();
    (card as HTMLElement).querySelector(".question-options")?.remove();
    (card as HTMLElement).querySelector(".question-footer")?.remove();
    card.classList.add("question-card-cancelled");
  });
  store.set({ questionPending: false });
  if (getActivePending() != null) clearPendingMessage();
  try { await api.cancelTurn(activeSid); } catch { /* ignore */ }
  setActiveRunning(false);
  removeTyping();
  updateSendStopButton();
}

// ---- Send ----
async function sendMessage() {
  const text = ($("prompt") as HTMLTextAreaElement).value.trim();
  if (!text) return;
  if (!store.get().activeSid) await createSession();
  const sid = store.get().activeSid!;

  // Intercept /compact and /prune commands — both use the same toast UI,
  // just hit different endpoints. /prune is a cheap no-model operation;
  // /compact always calls the model to generate a summary.
  const trimmedCmd = text.trim();
  if (trimmedCmd === "/compact" || trimmedCmd === "/prune") {
    ($("prompt") as HTMLTextAreaElement).value = "";
    ($("prompt") as HTMLTextAreaElement).style.height = "auto";
    $("charCount").textContent = "";

    const isPrune = trimmedCmd === "/prune";
    const loadingMsg = isPrune ? "Pruning tool outputs..." : "Compacting context...";
    const successMsg = isPrune ? "Tool outputs pruned" : "Context compacted successfully";
    const errorLabel = isPrune ? "Prune" : "Compaction";

    ensureCompactToast();
    setCompactToastState("loading", loadingMsg);

    try {
      const startTime = Date.now();
      const result = isPrune
        ? await api.pruneSession(sid)
        : await api.compactSession(sid);
      // /prune is essentially instant; /compact may take seconds. The
      // minimum-display-threshold keeps the spinner visible long enough
      // to read on fast operations.
      const minMs = isPrune ? 300 : 600;
      const elapsed = Date.now() - startTime;
      if (elapsed < minMs) await new Promise(r => setTimeout(r, minMs - elapsed));

      if (result.last_usage?.prompt_tokens !== undefined) {
        const contextWindow = 200000;
        const pct = Math.min(result.last_usage.prompt_tokens / contextWindow, 1);
        updateContextProgress(pct, result.last_usage.prompt_tokens);
      }
      await loadHistory(sid);

      // /compact can come back as a no-op when there's nothing to compress
      // (e.g. only 1 user message in the session, or the model returned
      // empty). Treat it as success but show a clear "nothing to compact"
      // message instead of pretending the context shrank.
      if (!isPrune && (result as any).noop) {
        setCompactToastState("success", "Nothing to compact — context is already minimal");
      } else {
        setCompactToastState("success", successMsg);
      }
      setTimeout(() => hideCompactToast(), 3000);
    } catch (e: any) {
      setCompactToastState("error", `${errorLabel} failed: ${e.message || "unknown error"}`);
      setTimeout(() => hideCompactToast(), 5000);
    }
    return;
  }

  ($("prompt") as HTMLTextAreaElement).value = "";
  ($("prompt") as HTMLTextAreaElement).style.height = "auto";
  $("charCount").textContent = "";

  const { config } = store.get();
  const details = config.modelDetails || [];
  const currentModel = details.find((m: any) => m.name === config.model);
  const supportsImage = currentModel?.supports_image ?? false;

  if (pendingImages.length > 0 && supportsImage) {
    const parts: unknown[] = [];
    if (text) parts.push({ type: "text", text });
    for (const img of pendingImages) {
      parts.push({ type: "image_url", image_url: { url: img.dataUrl } });
    }
    appendUserMsg(text || "(image)");
    pendingImages = [];
    renderImagePreviews();
    appendTyping();
    scrollBottom();
    await api.createTurn(sid, parts);
  } else {
    appendUserMsg(text);
    pendingImages = [];
    renderImagePreviews();
    appendTyping();
    scrollBottom();
    await api.createTurn(sid, text);
  }
}

// ---- Plan ----
async function refreshPlan() {
  const { activeSid } = store.get();
  if (!activeSid) return;
  try {
    await api.getPlan(activeSid);
  } catch { /* plan UI removed */ }
}

// ---- Automations ----
// The "Scheduled Tasks" nav button opens a modal with a list of running
// automations (name, interval, last run, last result) and a "+ New
// automation" affordance. New automations take a name, a prompt, and
// an interval (in seconds) — the server schedules a background task
// that re-sends the prompt to the runtime every `interval` seconds.
async function openAutomationsModal() {
  closeAllFullpageOverlays();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "automationsModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">⏰ 自动化</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body" id="automationsModalBody">
        <div class="skills-modal-loading">Loading automations...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  await loadAutomationsIntoModal();
}

function closeAutomationsModal() {
  document.getElementById("automationsModalBackdrop")?.remove();
}

function closeSettingsModal() {
  document.getElementById("settingsModalBackdrop")?.remove();
}

async function loadAutomationsIntoModal() {
  const body = document.getElementById("automationsModalBody");
  if (!body) return;
  let automations: api.Automation[] = [];
  try {
    automations = await api.listAutomations();
  } catch (e) {
    body.innerHTML = `<div class="skills-modal-error">Failed to load: ${esc((e as Error).message)}</div>`;
    return;
  }
  let html = "";
  if (automations.length === 0) {
    html = '<div class="automations-empty">No scheduled tasks yet. Add one below to have the agent re-run a prompt on a timer.</div>';
  } else {
    html = '<div class="automations-list">' + automations
      .map((a) => renderAutomationRow(a))
      .join("") + '</div>';
  }
  html += `
    <div class="automation-create-form" id="automationCreateForm">
      <div class="automation-create-header">+ New scheduled task</div>
      <label class="automation-label">Name<input type="text" class="automation-input" id="automationNameInput" placeholder="Daily standup summary" /></label>
      <label class="automation-label">Prompt<textarea class="automation-input automation-textarea" id="automationPromptInput" placeholder="What should the agent do each time the timer fires?"></textarea></label>
      <div class="automation-label-row">
        <label class="automation-label">Interval
          <select class="automation-input" id="automationIntervalInput">
            <option value="60">Every 1 minute</option>
            <option value="300" selected>Every 5 minutes</option>
            <option value="900">Every 15 minutes</option>
            <option value="3600">Every hour</option>
            <option value="21600">Every 6 hours</option>
            <option value="86400">Once a day</option>
            <option value="604800">Once a week</option>
          </select>
        </label>
      </div>
      <div class="automation-form-actions">
        <button class="automation-submit-btn" id="automationSubmitBtn">Create</button>
        <span class="automation-form-status" id="automationFormStatus"></span>
      </div>
    </div>`;
  body.innerHTML = html;

  // Wire row-level delete buttons
  body.querySelectorAll<HTMLElement>(".automation-row-delete").forEach((btn) => {
    btn.onclick = async () => {
      const aid = btn.dataset.aid!;
      const name = btn.dataset.name || aid;
      if (!confirm(`Delete "${name}"?`)) return;
      try {
        await api.deleteAutomation(aid);
        await loadAutomationsIntoModal();
      } catch (e) {
        alert(`Delete failed: ${(e as Error).message}`);
      }
    };
  });

  // Wire the form submit
  (body.querySelector("#automationSubmitBtn") as HTMLElement).onclick = async () => {
    const name = (body.querySelector("#automationNameInput") as HTMLInputElement).value.trim();
    const prompt = (body.querySelector("#automationPromptInput") as HTMLTextAreaElement).value.trim();
    const interval = parseInt((body.querySelector("#automationIntervalInput") as HTMLSelectElement).value, 10);
    const statusEl = body.querySelector("#automationFormStatus") as HTMLElement;
    if (!prompt) {
      statusEl.textContent = "Prompt is required";
      statusEl.className = "automation-form-status error";
      return;
    }
    statusEl.textContent = "Creating...";
    statusEl.className = "automation-form-status";
    try {
      await api.createAutomation(name || "Untitled task", prompt, interval);
      statusEl.textContent = "Created";
      statusEl.className = "automation-form-status success";
      // Brief success flash, then re-render the list
      setTimeout(() => loadAutomationsIntoModal(), 400);
    } catch (e) {
      statusEl.textContent = (e as Error).message || "Failed to create";
      statusEl.className = "automation-form-status error";
    }
  };
}

function renderAutomationRow(a: api.Automation): string {
  const intervalLabel = formatInterval(a.interval_seconds);
  const lastRunLabel = a.last_run ? formatRelativeTime(Math.floor(a.last_run)) || "just now" : "never";
  const lastResult = a.last_result
    ? `<div class="automation-row-result" title="${esc(a.last_result)}">${esc(a.last_result.slice(0, 140))}${a.last_result.length > 140 ? "…" : ""}</div>`
    : '<div class="automation-row-result muted">No runs yet</div>';
  return `
    <div class="automation-row">
      <div class="automation-row-main">
        <div class="automation-row-name">${esc(a.name)}</div>
        <div class="automation-row-meta">
          <span class="automation-row-interval">⏰ ${esc(intervalLabel)}</span>
          <span class="automation-row-lastrun">Last run: ${esc(lastRunLabel)}</span>
          <span class="automation-row-status ${a.enabled ? "on" : "off"}">${a.enabled ? "● running" : "○ stopped"}</span>
        </div>
        ${lastResult}
      </div>
      <button class="automation-row-delete" data-aid="${esc(a.id)}" data-name="${esc(a.name)}" title="Delete">🗑</button>
    </div>`;
}

function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

// ---- Diff ----
function toggleDiff() {
  const panel = $("rightPanel");
  panel.classList.toggle("show");
  store.set({ diffPanelOpen: panel.classList.contains("show") });
  if (panel.classList.contains("show")) refreshDiff();
}

async function refreshDiff() {
  const { activeSid } = store.get();
  if (!activeSid) return;
  const diff = await api.getDiff(activeSid);
  const body = $("diffBody");
  const stats = $("diffStats");
  if (!diff) {
    body.innerHTML = '<div class="diff-empty">No changes</div>';
    stats.textContent = "";
    return;
  }

  interface DiffFile { path: string; adds: number; dels: number; lines: string[]; }
  const fileGroups: DiffFile[] = [];
  let current: DiffFile | null = null;
  let totalAdds = 0, totalDels = 0;

  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) {
      const match = line.match(/b\/(.+)/);
      const path = match ? match[1] : "unknown";
      current = { path, adds: 0, dels: 0, lines: [] };
      fileGroups.push(current);
    } else if (current) {
      if (line.startsWith("+") && !line.startsWith("+++")) { current.adds++; totalAdds++; }
      else if (line.startsWith("-") && !line.startsWith("---")) { current.dels++; totalDels++; }
      current.lines.push(line);
    }
  }

  stats.textContent = `${fileGroups.length} files, +${totalAdds} -${totalDels}`;

  let html = "";
  for (const fg of fileGroups) {
    html += `<div class="diff-file">`;
    html += `<div class="diff-file-header" data-path="${esc(fg.path)}">`;
    html += `<span>${esc(fg.path)}</span>`;
    html += `<span class="file-stats">+${fg.adds} -${fg.dels}</span>`;
    html += `<button class="revert-btn" data-path="${esc(fg.path)}">Revert</button>`;
    html += `</div>`;
    html += `<div class="diff-file-content" style="display:none">`;
    for (const line of fg.lines) {
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) {
        html += `<div class="diff-line hdr">${esc(line)}</div>`;
      } else if (line.startsWith("+")) {
        html += `<div class="diff-line add">${esc(line)}</div>`;
      } else if (line.startsWith("-")) {
        html += `<div class="diff-line del">${esc(line)}</div>`;
      } else {
        html += `<div class="diff-line ctx">${esc(line)}</div>`;
      }
    }
    html += `</div></div>`;
  }

  body.innerHTML = html;

  body.querySelectorAll(".diff-file-header").forEach((hdr) => {
    const el = hdr as HTMLElement;
    const content = el.nextElementSibling as HTMLElement;
    el.onclick = (e) => {
      if ((e.target as HTMLElement).classList.contains("revert-btn")) return;
      content.style.display = content.style.display === "none" ? "block" : "none";
    };
  });

  body.querySelectorAll(".revert-btn").forEach((btn) => {
    (btn as HTMLElement).onclick = async (e) => {
      e.stopPropagation();
      const path = (btn as HTMLElement).dataset.path!;
      if (!confirm(`Revert ${path}?`)) return;
      try {
        await api.revertFiles(activeSid!, [path]);
        refreshDiff();
      } catch { /* ignore */ }
    };
  });
}

// ---- MCP Status ----
async function refreshMCPStatus() {
  try {
    const status = await api.getMCPStatus();
    if (status.servers.length > 0) {
      ($("mcpStatus") as HTMLElement).style.display = "flex";
      $("mcpDetail").textContent = `${status.servers.length} server${status.servers.length > 1 ? "s" : ""}, ${status.tools.length} tools`;
    } else {
      ($("mcpStatus") as HTMLElement).style.display = "none";
    }
  } catch {
    ($("mcpStatus") as HTMLElement).style.display = "none";
  }
}

// ---- Status ----
async function refreshStatus() {
  try {
    const s = await api.getStatus();
    store.set({ config: { ...store.get().config, model: s.model, workspace: s.workspace, tools: s.tools, approval: s.approval_policy } });
    ($("approvalSelect") as HTMLSelectElement).value = s.approval_policy;
  } catch { /* server not running */ }
}

// ---- Slash Commands ----
const SLASH_COMMANDS = [
  { name: "/compact", description: "Compact context window (model summary)" },
  { name: "/prune", description: "Strip old tool outputs (no model call)" },
];

let slashMenuIndex = -1;

function showSlashMenu(text: string) {
  const menu = $("slashMenu");
  const query = text.slice(1).toLowerCase();
  const filtered = SLASH_COMMANDS.filter(
    (c) => c.name.toLowerCase().includes(query) || c.description.toLowerCase().includes(query)
  );
  if (filtered.length === 0) {
    hideSlashMenu();
    return;
  }
  menu.innerHTML = filtered
    .map(
      (c, i) =>
        `<div class="slash-item ${i === 0 ? "active" : ""}" data-cmd="${esc(c.name)}" data-index="${i}">
           <span class="slash-name">${esc(c.name)}</span>
           <span class="slash-desc">${esc(c.description)}</span>
         </div>`
    )
    .join("");
  menu.style.display = "block";
  slashMenuIndex = 0;
  menu.querySelectorAll(".slash-item").forEach((el) => {
    el.addEventListener("click", () => {
      const cmd = (el as HTMLElement).dataset.cmd || "";
      insertSlashCommand(cmd);
    });
  });
}

function hideSlashMenu() {
  $("slashMenu").style.display = "none";
  slashMenuIndex = -1;
}

function isSlashMenuVisible() {
  return $("slashMenu").style.display === "block";
}

function moveSlashSelection(dir: number) {
  const items = $("slashMenu").querySelectorAll(".slash-item");
  if (items.length === 0) return;
  items[slashMenuIndex]?.classList.remove("active");
  slashMenuIndex = (slashMenuIndex + dir + items.length) % items.length;
  items[slashMenuIndex]?.classList.add("active");
}

function selectSlashCommand() {
  const items = $("slashMenu").querySelectorAll(".slash-item");
  if (items[slashMenuIndex]) {
    const cmd = (items[slashMenuIndex] as HTMLElement).dataset.cmd || "";
    insertSlashCommand(cmd);
  }
}

function insertSlashCommand(cmd: string) {
  const promptEl = $("prompt") as HTMLTextAreaElement;
  promptEl.value = cmd + " ";
  promptEl.focus();
  hideSlashMenu();
  // Auto-send no-argument commands like /compact and /prune
  if (cmd === "/compact" || cmd === "/prune") {
    sendMessage();
  }
}

// ---- Settings modal ----
async function openSettingsModal() {
  closeAllFullpageOverlays();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "settingsModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">Settings</div>
        <div class="fullpage-topbar-spacer"></div>
        <button class="settings-save-btn" id="settingsSaveBtn">Save</button>
      </div>
      <div class="fullpage-body settings-body">
        <div class="settings-loading">Loading config...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const body = backdrop.querySelector(".fullpage-body") as HTMLElement;

  try {
    const cfg = await api.getConfigJson();
    const m = cfg.model || {};
    const ap = cfg.approval || {};
    const mem = cfg.memory || {};
    const tool = cfg.tool || {};
    const mcp = cfg.mcp || {};
    const mcpServers = mcp.servers || {};
    const sandbox = cfg.sandbox || {};
    const hooks = cfg.hooks || {};
    const prompt = cfg.prompt || {};

    // SVG icons for tabs (16x16)
    const icons = {
      model: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"/><path d="M2 12h20"/><path d="M12 2a15 15 0 0 1 4 10 15 15 0 0 1-4 10 15 15 0 0 1-4-10A15 15 0 0 1 12 2z"/></svg>`,
      approval: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
      mcp: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`,
      tool: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`,
      hooks: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
      memory: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="2" x2="9" y2="4"/><line x1="15" y1="2" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="22"/><line x1="15" y1="20" x2="15" y2="22"/><line x1="20" y1="9" x2="22" y2="9"/><line x1="20" y1="14" x2="22" y2="14"/><line x1="2" y1="9" x2="4" y2="9"/><line x1="2" y1="14" x2="4" y2="14"/></svg>`,
      sandbox: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
      prompt: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
    };

    // Build MCP servers HTML
    let mcpServersHtml = "";
    const mcpServerNames = Object.keys(mcpServers);
    for (const sname of mcpServerNames) {
      const srv = mcpServers[sname] as any;
      const cmd = Array.isArray(srv.command) ? srv.command.join(" ") : (srv.command || "");
      mcpServersHtml += `
        <div class="settings-mcp-card" data-mcp-server="${esc(sname)}">
          <div class="settings-mcp-card-header">
            <span class="settings-mcp-name">${esc(sname)}</span>
            <div>
              <select class="settings-select" style="width:auto;padding:4px 8px;font-size:12px" data-mcp-enabled="${esc(sname)}">
                <option value="true" ${srv.enabled !== false ? "selected" : ""}>Enabled</option>
                <option value="false" ${srv.enabled === false ? "selected" : ""}>Disabled</option>
              </select>
              <button class="settings-hook-remove" data-mcp-remove="${esc(sname)}" title="Remove">×</button>
            </div>
          </div>
          <div class="settings-row"><label class="settings-label">Command</label><input class="settings-input" data-mcp-command="${esc(sname)}" value="${esc(cmd)}" /></div>
          <div class="settings-row"><label class="settings-label">Type</label>
            <select class="settings-select" data-mcp-type="${esc(sname)}">
              <option value="local" ${srv.type !== "remote" ? "selected" : ""}>local</option>
              <option value="remote" ${srv.type === "remote" ? "selected" : ""}>remote</option>
            </select>
          </div>
        </div>`;
    }

    // Build hooks HTML per hook type
    const hookTypes = ["before_turn", "after_turn", "before_tool", "after_tool"];
    let hooksHtml = "";
    for (const ht of hookTypes) {
      const items: string[] = hooks[ht] || [];
      let rows = "";
      for (let i = 0; i < items.length; i++) {
        rows += `<div class="settings-hook-row"><input class="settings-input" data-hook="${ht}" data-hook-idx="${i}" value="${esc(items[i])}" /><button class="settings-hook-remove" data-hook-remove="${ht}:${i}" title="Remove">×</button></div>`;
      }
      hooksHtml += `
        <div class="settings-section">
          <div class="settings-section-title">${ht.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</div>
          <div class="settings-desc">Shell commands to run ${ht.replace(/_/g, " ")}.</div>
          <div data-hook-list="${ht}">${rows}</div>
          <button class="settings-add-btn" data-hook-add="${ht}">+ Add command</button>
        </div>`;
    }

    body.innerHTML = `
      <div class="settings-layout">
        <div class="settings-tabs">
          <button class="settings-tab active" data-tab="model">${icons.model}<span>Model</span></button>
          <button class="settings-tab" data-tab="approval">${icons.approval}<span>Approval</span></button>
          <button class="settings-tab" data-tab="mcp">${icons.mcp}<span>MCP Servers</span></button>
          <button class="settings-tab" data-tab="tool">${icons.tool}<span>Tool</span></button>
          <button class="settings-tab" data-tab="hooks">${icons.hooks}<span>Hooks</span></button>
          <button class="settings-tab" data-tab="memory">${icons.memory}<span>Memory</span></button>
          <button class="settings-tab" data-tab="sandbox">${icons.sandbox}<span>Sandbox</span></button>
          <button class="settings-tab" data-tab="prompt">${icons.prompt}<span>Prompt</span></button>
        </div>
        <div class="settings-content">
          <!-- Model -->
          <div class="settings-panel active" data-panel="model">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Provider</div>
                <div class="settings-row"><label class="settings-label">Type</label>
                  <select class="settings-select" id="s_model_provider">
                    <option value="openai_agents" ${m.provider === "openai_agents" ? "selected" : ""}>OpenAI Agents</option>
                    <option value="anthropic" ${m.provider === "anthropic" ? "selected" : ""}>Anthropic</option>
                    <option value="openai" ${m.provider === "openai" ? "selected" : ""}>OpenAI</option>
                    <option value="" ${!m.provider ? "selected" : ""}>Custom</option>
                  </select>
                </div>
                <div class="settings-row"><label class="settings-label">API Key</label><input class="settings-input" type="password" id="s_model_api_key" value="${esc(m.api_key || "")}" /></div>
                <div class="settings-row"><label class="settings-label">Base URL</label><input class="settings-input" id="s_model_base_url" value="${esc(m.base_url || "")}" /></div>
              </div>
              <div class="settings-section">
                <div class="settings-section-title">Models</div>
                <div class="settings-desc">Add models and set one as default. Mark "Image" if the model supports vision input.</div>
                <div id="sModelsList"></div>
                <button class="settings-add-btn" id="addModelBtn">+ Add model</button>
              </div>
            </div>
          </div>
          <!-- Approval -->
          <div class="settings-panel" data-panel="approval">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Approval Policy</div>
                <div class="settings-desc">Controls how tools request permission before execution.</div>
                <div class="settings-row"><label class="settings-label">Policy</label>
                  <select class="settings-select" id="s_approval_policy">
                    <option value="suggest" ${ap.policy === "suggest" ? "selected" : ""}>suggest (ask every time)</option>
                    <option value="auto-edit" ${ap.policy === "auto-edit" ? "selected" : ""}>auto-edit (auto file edits)</option>
                    <option value="full-auto" ${ap.policy === "full-auto" ? "selected" : ""}>full-auto (no prompts)</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
          <!-- MCP -->
          <div class="settings-panel" data-panel="mcp">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">MCP</div>
                <div class="settings-row"><label class="settings-label">MCP Enabled</label>
                  <select class="settings-select" id="s_mcp_enabled">
                    <option value="true" ${mcp.enabled ? "selected" : ""}>Yes</option>
                    <option value="false" ${!mcp.enabled ? "selected" : ""}>No</option>
                  </select>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-title">Servers</div>
                <div id="mcpServersList">${mcpServersHtml}</div>
                <button class="settings-add-btn" id="addMcpServer">+ Add MCP server</button>
              </div>
            </div>
          </div>
          <!-- Tool -->
          <div class="settings-panel" data-panel="tool">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Tool Settings</div>
                <div class="settings-row"><label class="settings-label">Max Rounds</label><input class="settings-input" type="number" id="s_tool_max_rounds" value="${tool.max_rounds || 0}" /><span style="font-size:12px;color:var(--muted);margin-left:4px">0 = unlimited</span></div>
              </div>
            </div>
          </div>
          <!-- Hooks -->
          <div class="settings-panel" data-panel="hooks">
            <div class="settings-panel-inner">${hooksHtml}</div>
          </div>
          <!-- Memory -->
          <div class="settings-panel" data-panel="memory">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Memory</div>
                <div class="settings-row"><label class="settings-label">Backend</label>
                  <select class="settings-select" id="s_memory_backend">
                    <option value="inmemory" ${mem.backend === "inmemory" || !mem.backend ? "selected" : ""}>In-memory</option>
                  </select>
                </div>
                <div class="settings-row"><label class="settings-label">Context Window</label><input class="settings-input" type="number" id="s_memory_tokens" value="${mem.context_window_tokens || 200000}" /><span style="font-size:12px;color:var(--muted);margin-left:4px">tokens</span></div>
              </div>
            </div>
          </div>
          <!-- Sandbox -->
          <div class="settings-panel" data-panel="sandbox">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Sandbox</div>
                <div class="settings-row"><label class="settings-label">Mode</label>
                  <select class="settings-select" id="s_sandbox_mode">
                    <option value="off" ${sandbox.mode !== "docker" && sandbox.mode !== "restrictive" ? "selected" : ""}>Off</option>
                    <option value="docker" ${sandbox.mode === "docker" ? "selected" : ""}>Docker</option>
                    <option value="restrictive" ${sandbox.mode === "restrictive" ? "selected" : ""}>Restrictive</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
          <!-- Prompt -->
          <div class="settings-panel" data-panel="prompt">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Prompt Profile</div>
                <div class="settings-row"><label class="settings-label">Profile</label>
                  <select class="settings-select" id="s_prompt_profile">
                    <option value="default" ${prompt.profile === "default" || !prompt.profile ? "selected" : ""}>default</option>
                    <option value="concise" ${prompt.profile === "concise" ? "selected" : ""}>concise</option>
                    <option value="detailed" ${prompt.profile === "detailed" ? "selected" : ""}>detailed</option>
                    <option value="" ${!["default","concise","detailed"].includes(prompt.profile) && prompt.profile ? "selected" : ""}>custom</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>`;

    // Tab switching
    const tabs = body.querySelectorAll<HTMLButtonElement>(".settings-tab");
    const panels = body.querySelectorAll<HTMLDivElement>(".settings-panel");
    tabs.forEach(tab => {
      tab.onclick = () => {
        tabs.forEach(t => t.classList.remove("active"));
        panels.forEach(p => p.classList.remove("active"));
        tab.classList.add("active");
        body.querySelector(`.settings-panel[data-panel="${tab.dataset.tab}"]`)?.classList.add("active");
      };
    });

    // Hook add/remove
    body.querySelectorAll<HTMLButtonElement>("[data-hook-add]").forEach(btn => {
      btn.onclick = () => {
        const ht = btn.dataset.hookAdd!;
        const list = body.querySelector(`[data-hook-list="${ht}"]`)!;
        const idx = list.children.length;
        const row = document.createElement("div");
        row.className = "settings-hook-row";
        row.innerHTML = `<input class="settings-input" data-hook="${ht}" data-hook-idx="${idx}" value="" placeholder="e.g. npm run lint" /><button class="settings-hook-remove" data-hook-remove="${ht}:${idx}" title="Remove">×</button>`;
        row.querySelector(".settings-hook-remove")!.onclick = () => row.remove();
        list.appendChild(row);
        row.querySelector("input")?.focus();
      };
    });
    body.querySelectorAll<HTMLButtonElement>("[data-hook-remove]").forEach(btn => {
      btn.onclick = () => (btn.closest(".settings-hook-row") as HTMLElement)?.remove();
    });

    // MCP server remove
    body.querySelectorAll<HTMLButtonElement>("[data-mcp-remove]").forEach(btn => {
      btn.onclick = () => (btn.closest(".settings-mcp-card") as HTMLElement)?.remove();
    });

    // Models list management
    const modelsList = (m.models || []) as Array<{ name: string; supports_image?: boolean }>;
    const defaultModelName = m.name || "";
    const modelsListEl = body.querySelector("#sModelsList") as HTMLElement;

    function renderModelsList() {
      const rows = modelsListEl.querySelectorAll(".settings-model-row");
      const current: Array<{ name: string; supports_image: boolean; is_default: boolean }> = [];
      rows.forEach((row) => {
        const nameInput = row.querySelector(".s-model-name") as HTMLInputElement;
        const imgCheck = row.querySelector(".s-model-image") as HTMLInputElement;
        const defaultRadio = row.querySelector(".s-model-default") as HTMLInputElement;
        if (nameInput) {
          current.push({
            name: nameInput.value.trim(),
            supports_image: imgCheck?.checked ?? false,
            is_default: defaultRadio?.checked ?? false,
          });
        }
      });
      modelsListEl.innerHTML = current.map((m, i) => `
        <div class="settings-model-row">
          <input class="settings-input s-model-name" value="${esc(m.name)}" placeholder="Model name" style="flex:1" />
          <label class="settings-model-check" title="Supports image input"><input type="checkbox" class="s-model-image" ${m.supports_image ? "checked" : ""} /> Image</label>
          <label class="settings-model-check" title="Set as default model"><input type="radio" name="modelDefault" class="s-model-default" ${m.is_default ? "checked" : ""} /> Default</label>
          <button class="settings-hook-remove s-model-remove" title="Remove">×</button>
        </div>
      `).join("");
      modelsListEl.querySelectorAll(".s-model-remove").forEach((btn) => {
        (btn as HTMLElement).onclick = () => (btn as HTMLElement).closest(".settings-model-row")!.remove();
      });
      modelsListEl.querySelectorAll(".s-model-default").forEach((radio) => {
        (radio as HTMLInputElement).onchange = () => {
          modelsListEl.querySelectorAll(".s-model-default").forEach((r) => {
            if (r !== radio) (r as HTMLInputElement).checked = false;
          });
        };
      });
    }

    // Populate initial models
    if (modelsList.length > 0) {
      modelsListEl.innerHTML = modelsList.map((model: any) => `
        <div class="settings-model-row">
          <input class="settings-input s-model-name" value="${esc(model.name || "")}" placeholder="Model name" style="flex:1" />
          <label class="settings-model-check" title="Supports image input"><input type="checkbox" class="s-model-image" ${model.supports_image ? "checked" : ""} /> Image</label>
          <label class="settings-model-check" title="Set as default model"><input type="radio" name="modelDefault" class="s-model-default" ${model.name === defaultModelName ? "checked" : ""} /> Default</label>
          <button class="settings-hook-remove s-model-remove" title="Remove">×</button>
        </div>
      `).join("");
    } else {
      // Single model from config (backward compat)
      modelsListEl.innerHTML = `
        <div class="settings-model-row">
          <input class="settings-input s-model-name" value="${esc(m.name || "")}" placeholder="Model name" style="flex:1" />
          <label class="settings-model-check" title="Supports image input"><input type="checkbox" class="s-model-image" /> Image</label>
          <label class="settings-model-check" title="Set as default model"><input type="radio" name="modelDefault" class="s-model-default" checked /> Default</label>
          <button class="settings-hook-remove s-model-remove" title="Remove">×</button>
        </div>`;
    }
    modelsListEl.querySelectorAll(".s-model-remove").forEach((btn) => {
      (btn as HTMLElement).onclick = () => (btn as HTMLElement).closest(".settings-model-row")!.remove();
    });
    modelsListEl.querySelectorAll(".s-model-default").forEach((radio) => {
      (radio as HTMLInputElement).onchange = () => {
        modelsListEl.querySelectorAll(".s-model-default").forEach((r) => {
          if (r !== radio) (r as HTMLInputElement).checked = false;
        });
      };
    });

    const addModelBtn = body.querySelector("#addModelBtn") as HTMLElement;
    if (addModelBtn) {
      addModelBtn.onclick = () => {
        const row = document.createElement("div");
        row.className = "settings-model-row";
        row.innerHTML = `
          <input class="settings-input s-model-name" value="" placeholder="Model name" style="flex:1" />
          <label class="settings-model-check" title="Supports image input"><input type="checkbox" class="s-model-image" /> Image</label>
          <label class="settings-model-check" title="Set as default model"><input type="radio" name="modelDefault" class="s-model-default" /> Default</label>
          <button class="settings-hook-remove s-model-remove" title="Remove">×</button>`;
        row.querySelector(".s-model-remove")!.onclick = () => row.remove();
        row.querySelector(".s-model-default")!.onchange = () => {
          modelsListEl.querySelectorAll(".s-model-default").forEach((r) => {
            if (r !== row.querySelector(".s-model-default")) (r as HTMLInputElement).checked = false;
          });
        };
        modelsListEl.appendChild(row);
        row.querySelector("input")?.focus();
      };
    }

    // MCP add server
    const addBtn = body.querySelector("#addMcpServer") as HTMLElement;
    if (addBtn) {
      addBtn.onclick = () => {
        const name = prompt("MCP server name:");
        if (!name) return;
        const list = body.querySelector("#mcpServersList")!;
        const card = document.createElement("div");
        card.className = "settings-mcp-card";
        card.dataset.mcpServer = name;
        card.innerHTML = `
          <div class="settings-mcp-card-header">
            <span class="settings-mcp-name">${esc(name)}</span>
            <div>
              <select class="settings-select" style="width:auto;padding:4px 8px;font-size:12px" data-mcp-enabled="${esc(name)}">
                <option value="true" selected>Enabled</option>
                <option value="false">Disabled</option>
              </select>
              <button class="settings-hook-remove" data-mcp-remove="${esc(name)}" title="Remove">×</button>
            </div>
          </div>
          <div class="settings-row"><label class="settings-label">Command</label><input class="settings-input" data-mcp-command="${esc(name)}" value="" placeholder="e.g. npx @anthropic/mcp-server" /></div>
          <div class="settings-row"><label class="settings-label">Type</label>
            <select class="settings-select" data-mcp-type="${esc(name)}">
              <option value="local" selected>local</option>
              <option value="remote">remote</option>
            </select>
          </div>`;
        card.querySelector(".settings-hook-remove")!.onclick = () => card.remove();
        list.appendChild(card);
        card.querySelector("input")?.focus();
      };
    }

    // Save
    (backdrop.querySelector("#settingsSaveBtn") as HTMLElement).onclick = async () => {
      const btn = backdrop.querySelector("#settingsSaveBtn") as HTMLElement;
      btn.textContent = "Saving...";
      btn.setAttribute("disabled", "true");
      try {
        const updated = { ...cfg };

        // Model
        const providerSel = backdrop.querySelector("#s_model_provider") as HTMLSelectElement;
        const modelsRows = backdrop.querySelectorAll(".settings-model-row");
        const models: Array<{ name: string; supports_image: boolean }> = [];
        let defaultName = "";
        modelsRows.forEach((row) => {
          const nameInput = row.querySelector(".s-model-name") as HTMLInputElement;
          const imgCheck = row.querySelector(".s-model-image") as HTMLInputElement;
          const defaultRadio = row.querySelector(".s-model-default") as HTMLInputElement;
          const name = nameInput?.value.trim() || "";
          if (!name) return;
          const supports_image = imgCheck?.checked ?? false;
          models.push({ name, supports_image });
          if (defaultRadio?.checked) defaultName = name;
        });
        if (!defaultName && models.length > 0) defaultName = models[0].name;
        const provider = providerSel.value || (defaultName.includes("claude") ? "anthropic" : "openai");
        updated.model = {
          ...updated.model,
          provider,
          name: defaultName || updated.model?.name,
          api_key: (backdrop.querySelector("#s_model_api_key") as HTMLInputElement).value,
          base_url: (backdrop.querySelector("#s_model_base_url") as HTMLInputElement).value,
          models,
        };

        // Approval
        updated.approval = { ...updated.approval, policy: (backdrop.querySelector("#s_approval_policy") as HTMLSelectElement).value };

        // Memory
        updated.memory = { ...updated.memory, context_window_tokens: parseInt((backdrop.querySelector("#s_memory_tokens") as HTMLInputElement).value) || 200000 };

        // Tool
        updated.tool = { ...updated.tool, max_rounds: parseInt((backdrop.querySelector("#s_tool_max_rounds") as HTMLInputElement).value) || 0 };

        // MCP
        const mcpEnabled = (backdrop.querySelector("#s_mcp_enabled") as HTMLSelectElement).value === "true";
        const newMcpServers: Record<string, any> = {};
        backdrop.querySelectorAll<HTMLElement>(".settings-mcp-card").forEach(card => {
          const sname = card.dataset.mcpServer!;
          const cmdStr = (card.querySelector(`[data-mcp-command="${sname}"]`) as HTMLInputElement)?.value || "";
          const srvEnabled = (card.querySelector(`[data-mcp-enabled="${sname}"]`) as HTMLSelectElement)?.value !== "false";
          const srvType = (card.querySelector(`[data-mcp-type="${sname}"]`) as HTMLSelectElement)?.value || "local";
          const existing = mcpServers[sname] || {};
          newMcpServers[sname] = {
            ...existing,
            type: srvType,
            command: cmdStr.split(" ").filter(Boolean),
            enabled: srvEnabled,
          };
        });
        updated.mcp = { ...updated.mcp, enabled: mcpEnabled, servers: newMcpServers };

        // Sandbox
        updated.sandbox = { ...updated.sandbox, mode: (backdrop.querySelector("#s_sandbox_mode") as HTMLSelectElement).value };

        // Prompt
        updated.prompt = { ...updated.prompt, profile: (backdrop.querySelector("#s_prompt_profile") as HTMLSelectElement).value };

        // Hooks — rebuild from DOM
        const newHooks: Record<string, string[]> = {};
        for (const ht of hookTypes) {
          newHooks[ht] = Array.from(backdrop.querySelectorAll<HTMLInputElement>(`[data-hook="${ht}"]`)).map(i => i.value).filter(Boolean);
        }
        updated.hooks = newHooks;

        // Remove skill_index metadata before saving
        delete (updated as any)._skill_index;

        await api.saveConfigJson(updated);
        btn.textContent = "Saved";
        setTimeout(() => { btn.textContent = "Save"; btn.removeAttribute("disabled"); }, 1500);
      } catch (e) {
        btn.textContent = "Error";
        alert((e as Error).message);
        setTimeout(() => { btn.textContent = "Save"; btn.removeAttribute("disabled"); }, 1500);
      }
    };
  } catch (e) {
    body.innerHTML = `<div class="skills-modal-error">Failed to load config: ${esc((e as Error).message)}</div>`;
  }
}

// ---- Bootstrap ----
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
