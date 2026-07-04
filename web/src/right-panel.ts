/** Right-panel tab system + renderers — extracted verbatim from main.ts.
 *  Tab strip management (open/close/activate/render), the plan/review/terminal/files
 *  tab renderers, the file-tree viewer, and the git-diff refresh logic. Self-contained
 *  (no main.ts deps) — imports store/api/dom/@xterm/prism directly. */

import * as api from "./api";
import { $, esc, bindResizer } from "./dom";
import { store } from "./state";
import type { RightPanelTab } from "./state";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import Prism from "prismjs";

const panelTypes = [
  { type: "review", label: "Code Review", icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>' },
  { type: "plan", label: "Plan", icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>' },
  { type: "terminal", label: "Terminal", icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>' },
  { type: "files", label: "Files", icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>' },
] as const;

let _tabIdCounter = 0;
function nextTabId(): string { return "tab_" + (++_tabIdCounter); }

function openRightPanel(type: RightPanelTab["type"], title?: string, initialUrl?: string) {
  const { rightPanelTabs } = store.get();
  const pt = panelTypes.find(p => p.type === type);
  const tab: RightPanelTab = { id: nextTabId(), type, title: title || (pt ? pt.label : type) };
  if (initialUrl) tab.initialUrl = initialUrl;
  const tabs = [...rightPanelTabs, tab];
  store.set({ rightPanelTabs: tabs, activeRightTabId: tab.id, rightPanelOpen: true });
  $("rightPanel").classList.add("show");
  $("btnOpenRightPanel")?.classList.add("panel-open");
  renderTabBar();
  activateTab(tab.id);
  // Scroll tab bar to the end so the new tab is visible
  const scroll = document.querySelector(".rp-tabs-scroll");
  if (scroll) scroll.scrollLeft = scroll.scrollWidth;
}

function closeRightPanelTab(tabId: string) {
  const { rightPanelTabs, activeRightTabId } = store.get();
  const idx = rightPanelTabs.findIndex(t => t.id === tabId);
  if (idx < 0) return;
  const tabs = rightPanelTabs.filter(t => t.id !== tabId);
  let newActive = activeRightTabId;
  if (activeRightTabId === tabId) {
    if (tabs.length === 0) {
      newActive = null;
    } else {
      newActive = tabs[Math.min(idx, tabs.length - 1)].id;
    }
  }
  store.set({ rightPanelTabs: tabs, activeRightTabId: newActive });
  renderTabBar();
  if (newActive) {
    activateTab(newActive);
  } else if (tabs.length === 0) {
    renderWelcomeState();
  }
}

function activateTab(tabId: string) {
  const { rightPanelTabs } = store.get();
  store.set({ activeRightTabId: tabId });
  const tab = rightPanelTabs.find(t => t.id === tabId);
  if (!tab) return;
  const rp = $("rightPanel");
  const body = rp.querySelector(".right-panel-body") as HTMLElement;
  if (!body) return;
  body.querySelectorAll(".panel-content").forEach(el => ((el as HTMLElement).style.display = "none"));
  const welcome = body.querySelector(".welcome-state") as HTMLElement;
  if (welcome) welcome.style.display = "none";
  let content = body.querySelector(`[data-tab-id="${tabId}"]`) as HTMLElement;
  if (!content) {
    content = document.createElement("div");
    content.className = "panel-content";
    content.dataset.tabId = tabId;
    body.appendChild(content);
    renderTabContent(tab, content);
  }
  content.style.display = "flex";
  renderTabBar();
}

function renderTabBar() {
  const { rightPanelTabs, activeRightTabId } = store.get();
  const rp = $("rightPanel");
  let bar = rp.querySelector(".right-panel-tabs") as HTMLElement;
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "right-panel-tabs";
    rp.prepend(bar);
  }
  let html = '<div class="rp-tabs-scroll">';
  for (const tab of rightPanelTabs) {
    const pt = panelTypes.find(p => p.type === tab.type);
    const icon = pt ? pt.icon : "";
    const active = tab.id === activeRightTabId ? " active" : "";
    html += `<div class="rp-tab${active}" data-tab-id="${tab.id}">
      ${icon}<span class="rp-tab-title">${esc(tab.title)}</span>
      <button class="rp-tab-close" data-close-tab="${tab.id}">&times;</button>
    </div>`;
  }
  html += `</div>`;
  html += `<button class="rp-tab-add" id="btnAddTab" title="New tab">+</button>`;
  html += `<button class="rp-tab-toggle rp-tab-fullscreen" id="btnFullscreenPanel" title="Fullscreen">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
  </button>`;
  // Toggle/close button — same icon as toolbar-right-toggle (mirrored sidebar icon)
  html += `<button class="rp-tab-toggle" id="btnToggleRight" title="Close panel">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
  </button>`;
  bar.innerHTML = html;
  bar.querySelectorAll(".rp-tab").forEach(el => {
    (el as HTMLElement).onclick = (e) => {
      if ((e.target as HTMLElement).classList.contains("rp-tab-close")) return;
      activateTab((el as HTMLElement).dataset.tabId!);
    };
  });
  bar.querySelectorAll(".rp-tab-close").forEach(btn => {
    (btn as HTMLElement).onclick = (e) => {
      e.stopPropagation();
      closeRightPanelTab(((btn as HTMLElement).dataset.closeTab)!);
    };
  });
  const addBtn = bar.querySelector("#btnAddTab") as HTMLElement;
  if (addBtn) addBtn.onclick = showAddTabMenu;
  const fsBtn = bar.querySelector("#btnFullscreenPanel");
  if (fsBtn) fsBtn.addEventListener("click", toggleFullscreenPanel);
  const toggleBtn = bar.querySelector("#btnToggleRight");
  if (toggleBtn) toggleBtn.addEventListener("click", toggleRightPanel);
}

function renderWelcomeState() {
  const rp = $("rightPanel");
  const body = rp.querySelector(".right-panel-body") as HTMLElement;
  if (!body) return;
  body.querySelectorAll(".panel-content").forEach(el => ((el as HTMLElement).style.display = "none"));
  let welcome = body.querySelector(".welcome-state") as HTMLElement;
  if (!welcome) {
    welcome = document.createElement("div");
    welcome.className = "welcome-state";
    welcome.innerHTML = `
      <div class="welcome-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
      </div>
      <div class="welcome-title">工具面板</div>
      <div class="welcome-desc">点击 <strong>+</strong> 按钮打开面板</div>
      <div class="welcome-actions">
        ${panelTypes.map(pt => `<button class="welcome-action-btn" data-panel-type="${pt.type}">${pt.icon}<span>${pt.label}</span></button>`).join("")}
      </div>`;
    body.appendChild(welcome);
    welcome.querySelectorAll(".welcome-action-btn").forEach(btn => {
      (btn as HTMLElement).onclick = () => {
        const type = (btn as HTMLElement).dataset.panelType;
        if (type) openRightPanel(type as RightPanelTab["type"]);
      };
    });
  }
  welcome.style.display = "flex";
}

let _addTabMenu: HTMLElement | null = null;
let _addTabOutsideHandler: ((e: MouseEvent) => void) | null = null;
function showAddTabMenu() {
  if (_addTabMenu) { closeAddTabMenu(); return; }
  const menu = document.createElement("div");
  menu.className = "panel-dropdown add-tab-menu";
  for (const pt of panelTypes) {
    const el = document.createElement("div");
    el.className = "panel-dropdown-item";
    el.innerHTML = pt.icon + `<span>${pt.label}</span>`;
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeAddTabMenu();
      openRightPanel(pt.type);
    });
    menu.appendChild(el);
  }
  document.body.appendChild(menu);
  _addTabMenu = menu;
  // Position below the + button, clamped to viewport
  const btn = $("btnAddTab");
  if (btn) {
    const rect = btn.getBoundingClientRect();
    let top = rect.bottom + 4;
    let left = rect.left;
    const menuH = menu.offsetHeight || 180;
    const menuW = 180;
    if (left + menuW > window.innerWidth) left = window.innerWidth - menuW - 4;
    if (top + menuH > window.innerHeight) top = rect.top - menuH - 4;
    menu.style.position = "fixed";
    menu.style.top = Math.max(4, top) + "px";
    menu.style.left = Math.max(4, left) + "px";
  }
  // Close on any outside click
  _addTabOutsideHandler = (e: MouseEvent) => {
    if (_addTabMenu && !_addTabMenu.contains(e.target as Node) && (e.target as HTMLElement)?.id !== "btnAddTab") {
      closeAddTabMenu();
    }
  };
  setTimeout(() => {
    document.addEventListener("click", _addTabOutsideHandler!, true);
  }, 10);
}

function closeAddTabMenu() {
  if (_addTabMenu) { _addTabMenu.remove(); _addTabMenu = null; }
  if (_addTabOutsideHandler) {
    document.removeEventListener("click", _addTabOutsideHandler, true);
    _addTabOutsideHandler = null;
  }
}

export function toggleRightPanel() {
  const rp = $("rightPanel");
  const isOpen = rp.classList.toggle("show");
  store.set({ rightPanelOpen: isOpen });
  if (isOpen) {
    $("btnOpenRightPanel")?.classList.add("panel-open");
    const { rightPanelTabs, activeRightTabId } = store.get();
    if (rightPanelTabs.length === 0) {
      renderTabBar();
      renderWelcomeState();
    } else if (activeRightTabId) {
      activateTab(activeRightTabId);
    }
  } else {
    $("btnOpenRightPanel")?.classList.remove("panel-open");
    const layout = document.querySelector(".ziva-layout");
    layout?.classList.remove("panel-fullscreen");
  }
}

function closeRightPanel() {
  $("rightPanel").classList.remove("show");
  $("btnOpenRightPanel")?.classList.remove("panel-open");
  store.set({ rightPanelOpen: false });
  const layout = document.querySelector(".ziva-layout");
  layout?.classList.remove("panel-fullscreen");
}

function toggleFullscreenPanel() {
  const layout = document.querySelector(".ziva-layout");
  layout?.classList.toggle("panel-fullscreen");
}

// ---- Resizable Right Panel ----
export function initResizablePanel() {
  const rp = $("rightPanel");
  const handle = document.createElement("div");
  handle.className = "rp-resize-handle";
  rp.prepend(handle);
  let startX = 0, startW = 0;
  handle.addEventListener("mousedown", (e: MouseEvent) => {
    e.preventDefault();
    startX = e.clientX;
    startW = rp.offsetWidth;
    const onMouseMove = (e2: MouseEvent) => {
      const dx = startX - e2.clientX;
      const newW = Math.max(280, Math.min(window.innerWidth * 0.8, startW + dx));
      rp.style.flex = `0 0 ${newW}px`;
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  });
}

// ---- Panel Content Renderers ----

function renderTabContent(tab: RightPanelTab, container: HTMLElement) {
  switch (tab.type) {
    case "review": renderReviewTab(container); break;
    case "plan": renderPlanTab(container); break;
    case "terminal": renderTerminalTab(container); break;
    case "files": renderFilesTab(container); break;
  }
}

let _currentPlanSteps: { id?: string; description?: string; status?: string }[] = [];

function renderPlanTab(container: HTMLElement) {
  container.innerHTML = `<div class="panel-content-body plan-panel"><div class="plan-empty">Loading...</div></div>`;
  // Load latest plan from server
  const sid = store.get().activeSid;
  if (sid) {
    api.getPlan(sid).then(steps => {
      if (steps && steps.length > 0) {
        _currentPlanSteps = steps as { id?: string; description?: string; status?: string }[];
        updatePlanTabContent(_currentPlanSteps);
      } else if (_currentPlanSteps.length > 0) {
        updatePlanTabContent(_currentPlanSteps);
      } else {
        const panel = container.querySelector(".plan-panel") as HTMLElement;
        if (panel) panel.innerHTML = `<div class="plan-empty">No active plan</div>`;
      }
    }).catch(() => {
      const panel = container.querySelector(".plan-panel") as HTMLElement;
      if (panel) panel.innerHTML = `<div class="plan-empty">No active plan</div>`;
    });
  } else {
    const panel = container.querySelector(".plan-panel") as HTMLElement;
    if (panel) panel.innerHTML = `<div class="plan-empty">No active plan</div>`;
  }
}

export function updatePlanTabContent(steps: { id?: string; description?: string; status?: string }[]) {
  _currentPlanSteps = steps;
  const { rightPanelTabs } = store.get();
  const planTab = rightPanelTabs.find(t => t.type === "plan");
  if (!planTab) return;

  // Always find the panel-content via the right panel body to avoid wrong container
  const rp = $("rightPanel");
  const body = rp.querySelector(".right-panel-body") as HTMLElement;
  if (!body) return;
  let content = body.querySelector(`[data-tab-id="${planTab.id}"]`) as HTMLElement;
  if (!content) {
    activateTab(planTab.id);
    content = body.querySelector(`[data-tab-id="${planTab.id}"]`) as HTMLElement;
  }
  if (!content) return;
  let panel = content.querySelector(".plan-panel") as HTMLElement;
  if (!panel) {
    // Create the plan-panel directly instead of calling renderPlanTab,
    // which would asynchronously fetch from the server and overwrite
    // the steps we already have from the live tool_end event.
    panel = document.createElement("div");
    panel.className = "panel-content-body plan-panel";
    content.appendChild(panel);
  }
  if (!panel) return;

  const completed = steps.filter((s) => s.status === "completed").length;
  const inProgress = steps.filter((s) => s.status === "in_progress").length;
  const total = steps.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  let html = `<div class="plan-summary">${completed}/${total} done (${pct}%)</div>`;
  html += `<div class="plan-progress-bar"><div class="plan-progress-fill" style="width:${pct}%"></div></div>`;
  html += `<div class="plan-steps">`;
  for (const step of steps) {
    const icon = step.status === "completed" ? "✓" : step.status === "in_progress" ? "●" : "○";
    const cls = step.status || "pending";
    html += `<div class="plan-step plan-step-${esc(cls)}"><span class="plan-step-icon">${icon}</span><span class="plan-step-text">${esc(step.description || step.id || "")}</span></div>`;
  }
  html += `</div>`;
  panel.innerHTML = html;
}

function renderReviewTab(container: HTMLElement) {
  container.innerHTML = `
    <div class="review-header">
      <div class="review-header-info">
        <span class="review-branch" data-review-branch></span>
        <span class="review-stats" data-review-stats></span>
      </div>
      <div class="review-header-actions">
        <button class="review-action-btn" data-action="expand-all" title="Expand all">Expand All</button>
        <button class="review-action-btn" data-action="collapse-all" title="Collapse all">Collapse All</button>
      </div>
    </div>
    <div class="panel-content-body">
      <div class="review-layout">
        <div class="review-diff-area" data-review-body>
          <div class="diff-empty">No changes yet</div>
        </div>
        <div class="resizer" data-review-resizer title="Drag to resize"></div>
        <div class="review-file-sidebar">
          <div class="review-file-search">
            <input type="text" placeholder="Filter files..." data-review-filter />
          </div>
          <div class="review-file-list" data-review-files></div>
        </div>
      </div>
    </div>`;
  bindResizer(container.querySelector("[data-review-resizer]") as HTMLElement, container.querySelector(".review-file-sidebar") as HTMLElement, "right", 140);
  const body = container.querySelector("[data-review-body]") as HTMLElement;
  (container.querySelector('[data-action="expand-all"]') as HTMLElement).onclick = () => body.querySelectorAll(".diff-file-content").forEach(el => ((el as HTMLElement).style.display = "block"));
  (container.querySelector('[data-action="collapse-all"]') as HTMLElement).onclick = () => body.querySelectorAll(".diff-file-content").forEach(el => ((el as HTMLElement).style.display = "none"));
  // Filter handler
  const filterInput = container.querySelector("[data-review-filter]") as HTMLInputElement;
  filterInput.addEventListener("input", () => {
    const q = filterInput.value.toLowerCase();
    container.querySelectorAll("[data-review-file-item]").forEach(el => {
      const path = (el as HTMLElement).dataset.path || "";
      (el as HTMLElement).style.display = path.toLowerCase().includes(q) ? "" : "none";
    });
  });
  refreshDiffForContainer(container);
}

function renderTerminalTab(container: HTMLElement) {
  const tabId = container.dataset.tabId!;
  container.innerHTML = `
    <div class="panel-content-body" style="height:100%">
      <div id="terminalContainer_${tabId}" class="terminal-container"></div>
    </div>`;
  initTerminal(tabId);
}

function renderFilesTab(container: HTMLElement) {
  const ws = store.get().config.workspace || "";
  const wsName = ws.split("/").pop() || ws || "Files";
  container.innerHTML = `
    <div class="panel-content-header">
      <span class="panel-content-title" title="${esc(ws)}">${esc(wsName)}</span>
      <span class="panel-content-path">${esc(ws)}</span>
    </div>
    <div class="panel-content-body">
      <div class="files-layout">
        <div class="files-tree" data-files-tree></div>
        <div class="resizer" data-files-resizer title="Drag to resize"></div>
        <div class="files-viewer" data-files-viewer>
          <div class="files-viewer-empty">Select a file to view</div>
        </div>
      </div>
    </div>`;
  loadFileTreeForContainer(container);
  bindResizer(container.querySelector("[data-files-resizer]") as HTMLElement, container.querySelector("[data-files-tree]") as HTMLElement, "left", 120);
}

function initTerminal(tabId: string) {
  const container = document.getElementById("terminalContainer_" + tabId);
  if (!container) return;
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${wsProto}//${location.host}/ws/terminal`);
  const term = new Terminal({
    theme: { background: "#1a1a1a", foreground: "#e0e0e0", cursor: "#e0e0e0" },
    fontSize: 13,
    fontFamily: "var(--mono)",
    cursorBlink: true,
  });
  const fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  setTimeout(() => fitAddon.fit(), 0);
  term.onData((data: string) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data })); });
  ws.onmessage = (ev) => {
    try { const msg = JSON.parse(ev.data); if (msg.type === "output") term.write(msg.data); } catch {}
  };
  ws.onclose = () => term.write("\r\n\x1b[90m[Connection closed]\x1b[0m");
  ws.onerror = () => term.write("\r\n\x1b[31m[Connection error]\x1b[0m");
  const onResize = () => {
    fitAddon.fit();
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  window.addEventListener("resize", onResize);
  (container as any)._cleanup = () => { window.removeEventListener("resize", onResize); term.dispose(); };
}

async function loadFileTreeForContainer(panelContainer: HTMLElement) {
  const tree = panelContainer.querySelector("[data-files-tree]") as HTMLElement;
  const viewer = panelContainer.querySelector("[data-files-viewer]") as HTMLElement;
  if (!tree) return;
  tree.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px">Loading...</div>';
  try {
    const resp = await fetch("/api/files/tree?depth=2");
    if (!resp.ok) throw new Error("Failed");
    const data = await resp.json();
    renderFileTreeIn(tree, data.entries || [], 0, viewer);
  } catch {
    tree.innerHTML = '<div style="padding:14px;color:var(--red);font-size:12px">Failed to load files</div>';
  }
}

async function expandDir(item: HTMLElement, entry: any, depth: number, viewer: HTMLElement) {
  // If the entry already has children (returned by an earlier fetch at or
  // above this depth), render them directly. Otherwise lazy-load: the
  // initial tree fetch is shallow (depth=2), so deeper directories arrive
  // with no children — fetch this directory's subtree on first expand so
  // the Files tab can reach any depth instead of stopping at depth 2.
  if (entry.children && entry.children.length > 0) {
    renderFileTreeAtIn(item, entry.children, depth + 1, viewer);
    return;
  }
  try {
    item.classList.add("loading");
    const resp = await fetch("/api/files/tree?path=" + encodeURIComponent(entry.path) + "&depth=2");
    if (resp.ok) {
      const data = await resp.json();
      entry.children = data.entries || [];
      if (entry.children.length > 0) renderFileTreeAtIn(item, entry.children, depth + 1, viewer);
    }
  } catch { /* ignore lazy-load errors */ } finally {
    item.classList.remove("loading");
  }
}

function renderFileTreeIn(container: HTMLElement, entries: any[], depth: number, viewer: HTMLElement) {
  container.innerHTML = "";
  const sorted = [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const entry of sorted) {
    const item = document.createElement("div");
    item.className = "files-tree-item";
    item.style.paddingLeft = (10 + depth * 16) + "px";
    const icon = entry.type === "dir"
      ? '<span class="file-icon">&#128193;</span>'
      : '<span class="file-icon">&#128196;</span>';
    item.innerHTML = `${icon}<span class="${entry.type === "dir" ? "files-tree-dir" : ""}">${esc(entry.name)}</span>`;
    item.onclick = () => {
      if (entry.type === "dir") {
        const expanded = item.dataset.expanded === "true";
        item.dataset.expanded = expanded ? "false" : "true";
        let next = item.nextElementSibling as HTMLElement;
        while (next && parseInt(next.style.paddingLeft || "10px") > parseInt(item.style.paddingLeft || "10px")) {
          const toRemove = next;
          next = next.nextElementSibling as HTMLElement;
          toRemove.remove();
        }
        if (!expanded) {
          expandDir(item, entry, depth, viewer);
        }
      } else {
        container.querySelectorAll(".files-tree-item.active").forEach(el => el.classList.remove("active"));
        item.classList.add("active");
        loadFileContentIn(viewer, entry.path);
      }
    };
    container.appendChild(item);
  }
}

function renderFileTreeAtIn(afterEl: HTMLElement, entries: any[], depth: number, viewer: HTMLElement) {
  const parent = afterEl.parentElement;
  if (!parent) return;
  const sorted = [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  let refNode = afterEl.nextElementSibling;
  for (const entry of sorted) {
    const item = document.createElement("div");
    item.className = "files-tree-item";
    item.style.paddingLeft = (10 + depth * 16) + "px";
    const icon = entry.type === "dir" ? '<span class="file-icon">&#128193;</span>' : '<span class="file-icon">&#128196;</span>';
    item.innerHTML = `${icon}<span class="${entry.type === "dir" ? "files-tree-dir" : ""}">${esc(entry.name)}</span>`;
    item.onclick = () => {
      if (entry.type === "dir") {
        const expanded = item.dataset.expanded === "true";
        item.dataset.expanded = expanded ? "false" : "true";
        let next = item.nextElementSibling as HTMLElement;
        while (next && parseInt(next.style.paddingLeft || "10px") > parseInt(item.style.paddingLeft || "10px")) {
          const toRemove = next;
          next = next.nextElementSibling as HTMLElement;
          toRemove.remove();
        }
        if (!expanded) expandDir(item, entry, depth, viewer);
      } else {
        parent!.querySelectorAll(".files-tree-item.active").forEach(el => el.classList.remove("active"));
        item.classList.add("active");
        loadFileContentIn(viewer, entry.path);
      }
    };
    parent.insertBefore(item, refNode);
  }
}

async function loadFileContentIn(viewer: HTMLElement, path: string) {
  viewer.innerHTML = '<div class="file-empty">Loading...</div>';
  const ext = (path.split(".").pop() || "").toLowerCase();
  const imgExts = ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"];
  if (imgExts.includes(ext)) {
    const imgSrc = "/api/files/read?path=" + encodeURIComponent(path) + "&binary=1";
    viewer.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;padding:14px;background:var(--surface)">
      <img src="${imgSrc}" style="max-width:100%;max-height:100%;object-fit:contain;border-radius:6px" alt="${esc(path)}" />
    </div>`;
    return;
  }
  try {
    const resp = await fetch("/api/files/read?path=" + encodeURIComponent(path));
    if (!resp.ok) throw new Error("Failed");
    const data = await resp.json();
    const lang: Record<string, string> = { py: "python", ts: "typescript", js: "javascript", css: "css", html: "html", json: "json", md: "markdown", yaml: "yaml", yml: "yaml", sh: "bash", rs: "rust", go: "go", toml: "toml" };
    const pre = document.createElement("pre");
    pre.className = "language-" + (lang[ext] || "text");
    pre.textContent = data.content || "";
    viewer.innerHTML = "";
    viewer.appendChild(pre);
    if (typeof Prism !== "undefined") Prism.highlightElement(pre);
  } catch {
    viewer.innerHTML = '<div class="file-empty">Failed to load file</div>';
  }
}

const FILE_MUTATING_TOOLS = new Set<string>([
  "write_file",
  "edit_file",
  "shell",        // can mutate via redirection, rm, mv, sed, etc.
  "spawn_agent",  // sub-agents may also mutate files
]);

let _diffRefreshTimer: number | null = null;
export function scheduleDiffRefresh() {
  if (!$("rightPanel").classList.contains("show")) return;
  if (_diffRefreshTimer !== null) clearTimeout(_diffRefreshTimer);
  _diffRefreshTimer = window.setTimeout(() => {
    _diffRefreshTimer = null;
    refreshActiveReviewTabs();
  }, 250);
}

export function refreshActiveReviewTabs() {
  const { rightPanelTabs, activeRightTabId } = store.get();
  const activeTab = rightPanelTabs.find(t => t.id === activeRightTabId && t.type === "review");
  if (!activeTab) return;
  const rp = $("rightPanel");
  const container = rp.querySelector(`[data-tab-id="${activeTab.id}"]`) as HTMLElement;
  if (container) refreshDiffForContainer(container);
}

async function refreshDiffForContainer(container: HTMLElement) {
  const { activeSid } = store.get();
  if (!activeSid) return;
  const diff = await api.getDiff(activeSid);
  const body = container.querySelector("[data-review-body]") as HTMLElement;
  const fileList = container.querySelector("[data-review-files]") as HTMLElement;
  const statsEl = container.querySelector("[data-review-stats]") as HTMLElement;
  const branchEl = container.querySelector("[data-review-branch]") as HTMLElement;
  if (!body || !fileList) return;

  // Show branch
  if (branchEl) {
    const branchName = $("gitBranchName")?.textContent || "main";
    branchEl.textContent = `${branchName} → origin/${branchName}`;
  }

  if (!diff) {
    body.innerHTML = '<div class="diff-empty">No changes</div>';
    fileList.innerHTML = "";
    if (statsEl) statsEl.textContent = "";
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

  // Stats header
  if (statsEl) statsEl.innerHTML = `<span class="review-stat-add">+${totalAdds}</span> <span class="review-stat-del">-${totalDels}</span>`;

  // File sidebar
  let fileHtml = "";
  for (let i = 0; i < fileGroups.length; i++) {
    const fg = fileGroups[i];
    const iconColor = getFileIconColor(fg.path);
    fileHtml += `<div class="review-file-item" data-review-file-item data-path="${esc(fg.path)}" data-index="${i}">
      <span class="review-file-type" style="color:${iconColor}">${getFileIconText(fg.path)}</span>
      <span class="review-file-name">${esc(fg.path)}</span>
      <span class="review-file-stat">+${fg.adds} -${fg.dels}</span>
    </div>`;
  }
  fileList.innerHTML = fileHtml;

  // Diff viewer — only show selected file initially (first file selected)
  let selectedIdx = 0;
  const renderFileDiff = (idx: number) => {
    const fg = fileGroups[idx];
    if (!fg) { body.innerHTML = '<div class="diff-empty">Select a file</div>'; return; }
    let lineNum = 0;
    let html = `<div class="diff-file-active">`;
    html += `<div class="diff-active-header">`;
    html += `<span class="diff-active-indicator"></span>`;
    html += `<span class="diff-active-path">${esc(fg.path)}</span>`;
    html += `<span class="diff-active-stats">+${fg.adds} -${fg.dels}</span>`;
    html += `<button class="revert-btn" data-path="${esc(fg.path)}" title="Revert this file">Revert</button>`;
    html += `</div>`;
    html += `<div class="diff-file-content" style="display:block">`;
    for (const line of fg.lines) {
      lineNum++;
      if (line.startsWith("+++") || line.startsWith("---")) {
        continue; // skip file path headers
      } else if (line.startsWith("@@")) {
        const hunkMatch = line.match(/@@ -(\d+)/);
        lineNum = hunkMatch ? parseInt(hunkMatch[1]) - 1 : lineNum;
        html += `<div class="diff-line hdr"><span class="diff-linenum"></span><span class="diff-line-content">${esc(line)}</span></div>`;
      } else if (line.startsWith("+")) {
        lineNum++;
        html += `<div class="diff-line add"><span class="diff-linenum">${lineNum}</span><span class="diff-line-content">${esc(line.substring(1))}</span></div>`;
      } else if (line.startsWith("-")) {
        html += `<div class="diff-line del"><span class="diff-linenum"></span><span class="diff-line-content">${esc(line.substring(1))}</span></div>`;
      } else {
        lineNum++;
        html += `<div class="diff-line ctx"><span class="diff-linenum">${lineNum}</span><span class="diff-line-content">${esc(line)}</span></div>`;
      }
    }
    html += `</div></div>`;
    body.innerHTML = html;

    body.querySelectorAll(".revert-btn").forEach((btn) => {
      (btn as HTMLElement).onclick = async (e) => {
        e.stopPropagation();
        const path = (btn as HTMLElement).dataset.path!;
        if (!confirm(`Revert ${path}?`)) return;
        try {
          await api.revertFiles(activeSid, [path]);
          refreshDiffForContainer(container);
        } catch {}
      };
    });
  };
  renderFileDiff(0);

  // File sidebar click
  fileList.querySelectorAll("[data-review-file-item]").forEach((el) => {
    (el as HTMLElement).onclick = () => {
      fileList.querySelectorAll(".review-file-item.active").forEach(e => e.classList.remove("active"));
      (el as HTMLElement).classList.add("active");
      renderFileDiff(parseInt((el as HTMLElement).dataset.index || "0"));
    };
  });
  // Highlight first file
  const firstItem = fileList.querySelector("[data-review-file-item]") as HTMLElement;
  if (firstItem) firstItem.classList.add("active");
}

function getFileIconText(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const map: Record<string, string> = {
    py: "PY", ts: "TS", tsx: "TX", js: "JS", jsx: "JX", rs: "RS", go: "GO",
    css: "CS", html: "HT", json: "{ }", md: "MD", yaml: "YM", yml: "YM",
    toml: "TM", sql: "SQ", sh: "SH", txt: "TX", svg: "SV", png: "PN",
    jpg: "JP", jpeg: "JP", gif: "GI", webp: "WP", ico: "IC", bmp: "BM",
    lock: "LK", gitignore: "GI", cfg: "CF", ini: "IN",
  };
  return map[ext] || ext.substring(0, 2).toUpperCase() || "FI";
}

function getFileIconColor(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const colors: Record<string, string> = {
    py: "#3572A5", ts: "#3178C6", tsx: "#3178C6", js: "#F7DF1E", jsx: "#F7DF1E",
    rs: "#DEA584", go: "#00ADD8", css: "#563D7C", html: "#E34C26",
    json: "#FBC02D", md: "#81C995", yaml: "#CB171E", yml: "#CB171E",
    sh: "#89E051", sql: "#E38C00", svg: "#FF9900", png: "#F28B82",
    jpg: "#F28B82", jpeg: "#F28B82", gif: "#F28B82", webp: "#F28B82",
  };
  return colors[ext] || "var(--muted)";
}
