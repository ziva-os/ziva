import "./styles/base.css";
import "./styles/theme-dark.css";
import "./styles/theme-light.css";
import "./styles/components.css";
import * as api from "./api";
import { SSEConnection } from "./sse";
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
const store = new Store({
  sessions: [],
  activeSid: null,
  isRunning: false,
  config: { model: "unknown", models: [], approval: "suggest", workspace: "", tools: [] },
  connected: false,
  tokenUsage: null,
  latencyMs: null,
  sidebarOpen: true,
  diffPanelOpen: false,
  theme: (document.documentElement.getAttribute("data-theme") as "dark" | "light") || "dark",
  autoScroll: true,
});

const sse = new SSEConnection(handleEvent);
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

// ---- DOM Bootstrap — Antigravity Agent Manager layout ----
function init() {
  const app = $("app");
  app.innerHTML = `
    <div class="ziva-layout">
      <aside class="ziva-sidebar" id="sidebar">
        <div class="sidebar-header">
          <span class="sidebar-title">Agent Manager</span>
          <span class="sidebar-badge">Preview</span>
        </div>
        <div class="sidebar-top">
          <button id="btnNewSession" class="sidebar-btn">
            <span class="btn-icon">+</span>
            <span>New conversation</span>
          </button>
        </div>
        <div class="sidebar-nav">
          <button class="sidebar-nav-item" id="btnHistory">
            <span class="nav-icon">↺</span>
            <span>Conversation History</span>
          </button>
          <button class="sidebar-nav-item" id="btnScheduled">
            <span class="nav-icon">⏰</span>
            <span>Scheduled Tasks</span>
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
            <span class="nav-icon" id="themeIcon">◐</span>
            <span>Theme</span>
          </button>
          <button class="sidebar-nav-item" id="btnRightPanel">
            <span class="nav-icon">⊡</span>
            <span>Changes</span>
          </button>
          <button class="sidebar-nav-item" id="btnFeedback">
            <span class="nav-icon">?</span>
            <span>Feedback</span>
          </button>
        </div>
      </aside>
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
            <textarea id="prompt" placeholder="Ask anything, @ to mention, / for workflows" rows="1"></textarea>
            <div class="slash-menu" id="slashMenu" style="display:none"></div>
            <div class="composer-toolbar">
              <div class="toolbar-left">
                <button class="composer-action-btn" id="btnAttach" title="Attach">+</button>
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
      e.preventDefault(); sendMessage();
    }
    if (e.key === "Escape") {
      hideSlashMenu();
      if (store.get().isRunning) { cancelTurn(); }
    }
    if (e.key === "ArrowDown" && isSlashMenuVisible()) { e.preventDefault(); moveSlashSelection(1); }
    if (e.key === "ArrowUp" && isSlashMenuVisible()) { e.preventDefault(); moveSlashSelection(-1); }
  });

  $("btnSend").onclick = () => {
    if (store.get().isRunning) { cancelTurn(); } else { sendMessage(); }
  };
  $("btnNewSession").onclick = () => createSession();
  $("btnRightPanel").onclick = toggleDiff;
  $("btnCloseRight").onclick = toggleDiff;

  $("btnTheme").onclick = () => {
    const current = store.get().theme;
    const next = current === "dark" ? "light" : "dark";
    store.set({ theme: next });
    document.documentElement.setAttribute("data-theme", next);
    $("themeIcon").textContent = next === "dark" ? "◐" : "◑";
    localStorage.setItem("ziva-theme", next);
  };

  $("btnFeedback").onclick = () => {
    window.open("https://github.com/anthropics/claude-code/issues", "_blank");
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
        store.set({ activeSid: null, isRunning: false });
        $("messages").innerHTML = "";
        showEmptyState(true);
        sse.disconnect();
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

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "d") { e.preventDefault(); toggleDiff(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "b") { e.preventDefault(); $("sidebar").classList.toggle("show"); }
    if ((e.metaKey || e.ctrlKey) && e.key === "n") { e.preventDefault(); createSession(); }
  });

  $("messages").addEventListener("scroll", () => {
    const el = $("messages");
    store.set({ autoScroll: el.scrollTop + el.clientHeight >= el.scrollHeight - 50 });
  });

  const savedTheme = localStorage.getItem("ziva-theme") as "dark" | "light" | null;
  if (savedTheme) {
    store.set({ theme: savedTheme });
    document.documentElement.setAttribute("data-theme", savedTheme);
    $("themeIcon").textContent = savedTheme === "dark" ? "◐" : "◑";
  }
}

// ---- Config ----
async function refreshConfig() {
  try {
    const cfg = await api.getConfig();
    store.set({ config: { ...store.get().config, model: cfg.model.current, models: cfg.model.available, approval: cfg.approval.current } });
    const sel = $("modelSelect") as HTMLSelectElement;
    sel.innerHTML = cfg.model.available.map(m => `<option value="${esc(m)}" ${m === cfg.model.current ? "selected" : ""}>${esc(m)}</option>`).join("");
    ($("approvalSelect") as HTMLSelectElement).value = cfg.approval.current;
  } catch { /* server not running */ }
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

setInterval(async () => {
  const { sessions, activeSid } = store.get();
  const bgRunning = sessions.filter(s => s.status === "running" && s.id !== activeSid);
  if (bgRunning.length === 0) return;
  
  let changed = false;
  for (const s of bgRunning) {
    try {
      const turns = await api.getTurns(s.id);
      const stillRunning = turns.some(t => t.status === "running");
      if (!stillRunning) {
         s.status = turns.length > 0 ? "done" : "idle";
         changed = true;
      }
    } catch {}
  }
  if (changed) {
    store.set({ sessions: [...sessions] });
    renderSessions();
  }
}, 3000);

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
  store.set({ activeSid: sid, isRunning: false });
  renderSessions();
  $("messages").innerHTML = "";
  currentAssistantEl = null;
  currentTextParts = { thinking: "", main: "" };
  pendingTools.forEach(c => c.remove());
  pendingTools.clear();
  await loadHistory(sid);

  try {
    const turns = await api.getTurns(sid);
    const activeTurn = turns.find(t => t.status === "running");
    if (activeTurn) {
      store.set({ isRunning: true });
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

  updateSendStopButton();
  sse.connect(sid);
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
    sse.disconnect();
  }
  renderSessions();
}

async function loadHistory(sid: string) {
  const data = await api.getMessages(sid);
  const msgs = data.messages || [];
  const hasContent = msgs.length > 0;
  showEmptyState(!hasContent);

  // Clear existing messages before rebuilding
  $("messages").innerHTML = "";
  currentAssistantEl = null;
  currentTextParts = { thinking: "", main: "" };
  pendingTools.forEach(c => c.remove());
  pendingTools.clear();

  // Restore context ring from persisted usage
  if (data.last_usage?.prompt_tokens) {
    const pct = Math.min(data.last_usage.prompt_tokens / 200000, 1);
    updateContextProgress(pct, data.last_usage.prompt_tokens);
  } else {
    updateContextProgress(0, 0);
  }

  // Rebuild UI from persisted messages in original order
  // Track tool_calls from assistant messages to pair with tool results
  let pendingToolCalls: { id: string; name: string; arguments: Record<string, unknown> }[] = [];

  for (const m of msgs) {
    const isSub = (m as any)._subagent === true;

    if (m.role === "user") {
      appendUserMsg(m.content);
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
          $("messages").appendChild(thinkDiv);
        }
      } else {
        appendAssistantMsg(m.content);
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
      try { output = JSON.parse(m.content); } catch {}

      let subagentTools: string[] | undefined;
      if (toolName === "spawn_agent" && typeof output === "object" && output !== null) {
        subagentTools = (output as any).tools;
      }

      appendToolCard(toolName, args, "success", output, subagentTools);
    }
  }
  scrollBottom();
}

// ---- Chat Rendering ----
function appendUserMsg(text: string) {
  showEmptyState(false);
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> You</div><div class="md">${renderMarkdown(text)}</div></div>`;
  $("messages").appendChild(div);
  currentAssistantEl = null;
  highlightCode(div);
}

function appendAssistantMsg(text: string) {
  const div = document.createElement("div");
  div.className = "msg assistant";
  const { thinking, main } = extractThinking(text);
  let content = "";
  if (thinking) {
    content += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
  }
  content += `<div class="md">${renderMarkdown(main)}</div>`;
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div>${content}</div>`;
  $("messages").appendChild(div);
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
): HTMLElement {
  const card = document.createElement("div");
  card.className = "tool-card" + (status === "running" ? " open" : "");
  const statusClass = status === "error" ? "error" : status === "running" ? "running" : "success";
  const statusText = status === "error" ? "error" : status === "running" ? "running..." : "done";
  const abbrevArg = getAbbreviatedArg(args);

  let body = "";
  if (Object.keys(args).length > 0) {
    body += `<div class="section-label">Input</div>`;
    body += `<div class="section-content"><code>${esc(JSON.stringify(args, null, 2))}</code></div>`;
  }
  if (output !== undefined) {
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

  $("messages").appendChild(card);
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

function appendQuestionCard(question: string, options: string[]) {
  showEmptyState(false);
  const card = document.createElement("div");
  card.className = "question-card";
  let html = `<div class="question-header">?</div><div class="question-text">${esc(question)}</div>`;
  if (options.length > 0) {
    html += `<div class="question-options">${options.map((o) => `<button class="question-option-btn">${esc(o)}</button>`).join("")}</div>`;
  } else {
    html += `<div class="question-input-row"><input type="text" class="question-input" placeholder="Your answer..." /><button class="question-submit">Send</button></div>`;
  }
  card.innerHTML = html;

  const submit = (answer: string) => {
    const activeSid = store.get().activeSid;
    if (!activeSid || !answer.trim()) return;
    api.createTurn(activeSid, answer.trim());
    card.querySelector(".question-input-row")?.remove();
    card.querySelector(".question-options")?.remove();
    const replyDiv = document.createElement("div");
    replyDiv.className = "question-reply";
    replyDiv.textContent = `You: ${answer}`;
    card.appendChild(replyDiv);
  };

  if (options.length > 0) {
    card.querySelectorAll(".question-option-btn").forEach((btn) => {
      btn.addEventListener("click", () => submit((btn as HTMLElement).textContent || ""));
    });
  } else {
    const input = card.querySelector(".question-input") as HTMLInputElement;
    const btn = card.querySelector(".question-submit") as HTMLElement;
    btn.onclick = () => submit(input.value);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(input.value); });
  }

  $("messages").appendChild(card);
  currentAssistantEl = null;
}

function appendError(msg: string) {
  const div = document.createElement("div");
  div.className = "error-card";
  div.textContent = "Error: " + msg;
  $("messages").appendChild(div);
  currentAssistantEl = null;
}

function scrollBottom() {
  if (store.get().autoScroll) $("messages").scrollTop = $("messages").scrollHeight;
}

// ---- SSE Event Handling ----
function handleEvent(ev: api.Event, updateScroll: boolean = true) {
  // Skip all sub-agent events — they are shown in a collapsed card, not individually
  if ((ev as any)._subagent) return;

  const t = ev.type as string;

  if (t === "turn_start") {
    store.set({ isRunning: true });
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
      const output = (ev.output || {}) as Record<string, unknown>;
      appendQuestionCard(String(output.question || ""), (output.options || []) as string[]);
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
    removeTyping();
    store.set({ isRunning: false });
    currentAssistantEl = null;
    updateSendStopButton();
    pendingTools.forEach(c => c.remove());
    pendingTools.clear();
    refreshPlan();
    refreshSessions();
    if ($("rightPanel").classList.contains("show")) refreshDiff();
    if (store.get().activeSid) {
      $("messages").innerHTML = "";
      currentAssistantEl = null;
      currentTextParts = { thinking: "", main: "" };
      loadHistory(store.get().activeSid!);
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
    store.set({ isRunning: false });
    updateSendStopButton();
    appendError(ev.error as string || "Unknown error");
  }

  updateConnStatus(sse.connected);
}

function updateConnStatus(connected: boolean) {
  store.set({ connected });
}

function updateSendStopButton() {
  const btn = $("btnSend");
  const { isRunning } = store.get();
  if (isRunning) {
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

// ---- Cancel ----
async function cancelTurn() {
  const { activeSid } = store.get();
  if (!activeSid) return;
  try { await api.cancelTurn(activeSid); } catch { /* ignore */ }
  store.set({ isRunning: false });
  removeTyping();
  updateSendStopButton();
}

// ---- Send ----
async function sendMessage() {
  const text = ($("prompt") as HTMLTextAreaElement).value.trim();
  if (!text) return;
  if (!store.get().activeSid) await createSession();
  const sid = store.get().activeSid!;

  // Intercept /compact command
  if (text.trim() === "/compact") {
    ($("prompt") as HTMLTextAreaElement).value = "";
    ($("prompt") as HTMLTextAreaElement).style.height = "auto";
    $("charCount").textContent = "";

    showEmptyState(false);
    const compactEl = document.createElement("div");
    compactEl.className = "msg compacting";
    compactEl.id = "compactingIndicator";
    compactEl.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> System</div><div class="compact-body">Compacting context...</div></div>`;
    $("messages").appendChild(compactEl);
    scrollBottom();

    try {
      const startTime = Date.now();
      await api.compactSession(sid);
      // Ensure indicator is visible for at least 600ms
      const elapsed = Date.now() - startTime;
      if (elapsed < 600) await new Promise(r => setTimeout(r, 600 - elapsed));
      compactEl.remove();
      await loadHistory(sid);
      // Show success toast with dismiss button (aligned with aicoder's showInfo pattern)
      const toast = document.createElement("div");
      toast.className = "msg compacting";
      toast.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> System</div><div class="compact-body">Context compacted successfully <button class="toast-dismiss" style="margin-left:8px;padding:2px 8px;font-size:12px;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:4px;cursor:pointer;">Dismiss</button></div></div>`;
      $("messages").appendChild(toast);
      const dismissBtn = toast.querySelector(".toast-dismiss") as HTMLButtonElement;
      if (dismissBtn) {
        dismissBtn.onclick = () => toast.remove();
      }
      setTimeout(() => toast.remove(), 5000);
    } catch (e: any) {
      compactEl.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> System</div><div class="error-card">Compaction failed: ${esc(e.message || "unknown error")}</div></div>`;
    }
    return;
  }

  ($("prompt") as HTMLTextAreaElement).value = "";
  ($("prompt") as HTMLTextAreaElement).style.height = "auto";
  $("charCount").textContent = "";
  appendUserMsg(text);
  appendTyping();
  scrollBottom();
  await api.createTurn(sid, text);
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
async function refreshAutomations() {
  try { await api.listAutomations(); } catch { /* automations UI removed */ }
}

async function createAutomation() {
  /* automations UI removed from sidebar */
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
  { name: "/compact", description: "Compact context window" },
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
  // Auto-send no-argument commands like /compact
  if (cmd === "/compact") {
    sendMessage();
  }
}

// ---- Bootstrap ----
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
