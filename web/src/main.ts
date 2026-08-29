import "./styles/base.css";
import "./styles/theme-dark.css";
import "./styles/theme-light.css";
import "./styles/components.css";
import "./styles/browser-shell.css";
import "@xterm/xterm/css/xterm.css";
import * as api from "./api";
import { SSEPool } from "./sse";

import { FILE_MUTATING_TOOLS } from "./right-panel";
import { renderMarkdown, addCopyButtons, highlightCode, extractThinking, setAttachmentDataMap } from "./markdown";
import { Store, store } from "./state";
import type { AppState, PendingAttachment, RightPanelTab } from "./state";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import Prism from "prismjs";
import { $, esc, bindResizer } from "./dom";
import { openLinkInBrowser, initMessageLinkInterceptor } from "./links";
import { initBrowserShell, openInBrowserTab } from "./browser-shell";
import { closeAllFullpageOverlays } from "./modals";
import { copyText } from "./electron-bridge";
import { openSkillsBrowser, closeSkillViewer, invalidateSkillsCache, refreshSkillsModalInPlace } from "./modals/skills";
import { openSettingsModal, setSettingsDeps } from "./modals/settings";
import { openAutomationsModal, closeAutomationsModal, loadAutomationsIntoModal, setAutomationsDeps, refreshAutomationDetailIfOpen } from "./modals/automations";
import { openIMBridgeModal } from "./modals/im-bridge";
import { channelIconHtml } from "./icons";
import * as i18n from "./i18n";
import { formatRelativeTime, formatRelativeSeconds } from "./format";
import { refreshStatus, refreshMCPStatus, updateConnStatus, setStatusDeps } from "./status";
import {   toggleRightPanel, initResizablePanel, updatePlanTabContent, scheduleDiffRefresh, refreshActiveReviewTabs, refreshActivePlanTab, ensurePlanSubscriber } from "./right-panel";
import {
  isActiveRunning, getActivePending, setActivePending, setActiveRunning,
  setSessionRunning, isSessionRunning, getSessionPending, setSessionPending,
  generatePendingId, enqueuePending, getPendingQueue, updatePendingItem,
  removePendingItem, clearAllPending,
  streamCtx, clearStreamCtx, invalidateLiveStreamEl, invalidateStreamCtx,
  getLiveStreamSid, setLiveStreamSid, getLiveStreamTarget, setLiveStreamTarget,
  draftImages, setDraftImages, draftText, setDraftText,
  queuedImages,
  markSidCancelled, clearSidCancelled, isSidCancelling,
  setRuntimeStateDeps,
} from "./runtime-state";

// Helper to strip <thinking> block from text
export function stripThinking(text: string): string {
  if (!text) return text;
  return text
    .replace(/<think>[\s\S]*?<\/think>/g, "")
    .replace(/<think>[\s\S]*$/g, "")
    .trim();
}

// Lightbox for clicking on images to zoom
function initLightbox() {
  const overlay = document.createElement("div");
  overlay.id = "lightbox";
  overlay.style.cssText = "display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.85);cursor:pointer;align-items:center;justify-content:center";
  overlay.innerHTML = '<img style="max-width:90vw;max-height:90vh;object-fit:contain;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,0.5)" />';
  overlay.onclick = () => { overlay.style.display = "none"; };
  document.body.appendChild(overlay);

  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    if (target.tagName === "IMG" && target.closest(".msg-inner, .tool-output-image, .compact-dropped, .image-preview-item, .pending-bar-thumb")) {
      const img = overlay.querySelector("img") as HTMLImageElement;
      // Prefer data-full-src if available (full resolution), otherwise use src
      img.src = (target as HTMLImageElement).getAttribute("data-full-src") || (target as HTMLImageElement).src;
      overlay.style.display = "flex";
      e.preventDefault();
      e.stopPropagation();
    }
  });
}


function previewText(content: unknown): string {
  if (typeof content === "string") return content.slice(0, 60);
  if (Array.isArray(content)) {
    for (const p of content) {
      if (typeof p === "object" && p !== null && (p as any).type === "text" && (p as any).text) {
        return (p as any).text.slice(0, 60);
      }
    }
    return "(image)";
  }
  return String(content).slice(0, 60);
}

// Coerce any SSE/persisted content payload into display text. Deltas and
// assistant messages are usually strings, but block arrays
// ([{type:"text",text:"..."}]) and stray objects do reach these paths —
// `"" + obj` would paint "[object Object]" into the bubble.
function contentToText(content: unknown): string {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((c) => {
      if (typeof c === "string") return c;
      const o = c as any;
      if (o?.type === "text" && typeof o.text === "string") return o.text;
      if (o?.type === "image_url" || o?.image_url) return " [image] ";
      try { return JSON.stringify(c); } catch { return String(c); }
    }).join("");
  }
  if (typeof content === "object") {
    const o = content as any;
    if (typeof o.text === "string") return o.text;
    try { return JSON.stringify(content); } catch { return String(content); }
  }
  return String(content);
}

// ---- State ----

// ---- Per-session state helpers ----
// Reading the running flag for the active session. Other sessions'
// values are kept in the map but only matter for background turns
// (e.g. when a question card is answered in a non-active session —
// handled in the SSE event path).

const sse = new SSEPool();


// Global voice-input state. Bound to `#btnMic` in the (single, global)
// composer. The MediaRecorder is a single resource — only one
// recording at a time.
let mediaRecorder: MediaRecorder | null = null;
let audioChunks: Blob[] = [];
let isRecording = false;


// Maximum number of times a queued (Codex-style) message will be
// re-tried after a failed createTurn before we give up and surface
// a permanent error to the user.
const MAX_QUEUE_RETRIES = 3;

// ---- Empty State ----
function showEmptyState(show: boolean) {
  const center = document.querySelector(".ziva-center");
  if (center) center.classList.toggle("has-messages", !show);
  // Clear any inline display overrides first — revealMessagesTarget sets
  // them to surface the /model picker without flipping has-messages — so the
  // has-messages CSS + the rules below drive visibility cleanly.
  const msgs = $("messages");
  const empty = $("emptyState");
  if (msgs) msgs.style.display = "";
  if (empty) empty.style.display = "";
  // In split mode we keep #messages visible and show a per-pane placeholder.
  const inSplit = !!center?.classList.contains("multi");
  if (!inSplit && msgs) {
    msgs.style.display = show ? "none" : "block";
  }
  // (#emptyState visibility is owned by the has-messages / multi CSS rules,
  //  so once its inline override is cleared above it follows has-messages.)
}

// Make a messages target visible before appending a card (e.g. the /model or
// /effort picker). An empty single-pane session sets #messages to
// display:none and overlays the welcome screen, so without this the card
// would be appended but invisible; split panes just hold a placeholder div.
// NOTE: do NOT use showEmptyState(false) here — toggling has-messages also
// hides the status bar (workspace/branch selector), so the user would lose
// workspace selection after picking a model. Override the empty-state CSS
// directly via inline styles instead; showEmptyState clears them on the next
// empty-state re-evaluation.
function revealMessagesTarget(target: HTMLElement) {
  clearPaneEmptyPlaceholder(target);
  if (target.id === "messages") {
    target.style.display = "block";
    const empty = $("emptyState");
    if (empty) empty.style.display = "none";
  }
}

function setPaneEmptyPlaceholder(target: HTMLElement) {
  target.innerHTML = `<div class="pane-empty-state"><svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><div>${i18n.t("msg.empty")}</div></div>`;
}

function clearPaneEmptyPlaceholder(target: HTMLElement) {
  target.querySelectorAll(".pane-empty-state").forEach((el) => el.remove());
}

// ---- Right Panel Tab System ----


// ---- Relative time formatting ----

// ---- DOM Bootstrap — Ziva layout ----
function init() {
  // Sync UI language to backend so server-side features (compaction, etc.)
  // use the correct language. Fire-and-forget — the first compaction won't
  // happen until many turns in, so latency here is irrelevant.
  fetch("/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ui: { lang: i18n.getLang() } }),
  }).catch(() => {});
  // Inject main.ts dependencies into the extracted modal modules. Done here
  // (not at module top) because `store` is a `const` defined below — calling
  // these at module-load would hit the temporal dead zone and crash init.
  setSettingsDeps({ refreshConfig });
  setAutomationsDeps({ store, composerTextarea, formatRelativeSeconds });
  setStatusDeps({ composerApprovalSelect, openProjectPicker, openGitBranchPicker });
  setRuntimeStateDeps({ updateSendStopButton });
  initLightbox();
  initMessageLinkInterceptor();
  if ((window as any).electronAPI && navigator.platform.toLowerCase().includes("mac")) {
    document.body.classList.add("electron-darwin");
  }
  const app = $("app");
  app.innerHTML = `
    <div class="ziva-layout">
      <aside class="ziva-sidebar" id="sidebar">
        <div class="sidebar-header">
          <button class="sidebar-toggle-btn" id="btnToggleSidebar" title="${i18n.t("sidebar.toggle")}" aria-label="${i18n.t("sidebar.toggle")}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
        </div>
        <div class="sidebar-top">
          <button id="btnNewSession" class="sidebar-btn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            <span>${i18n.t("sidebar.newSession")}</span>
          </button>
        </div>
        <div class="sidebar-nav">
          <button class="sidebar-nav-item" id="btnSkills">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
            <span>${i18n.t("sidebar.skills")}</span>
          </button>
          <button class="sidebar-nav-item" id="btnScheduled">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
            <span>${i18n.t("sidebar.automations")}</span>
          </button>
          <button class="sidebar-nav-item" id="btnConnectIM" title="${i18n.t("sidebar.connectIM")}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"></rect><line x1="12" y1="18" x2="12.01" y2="18"></line></svg>
            <span>${i18n.t("sidebar.connectIM")}</span>
          </button>
        </div>
        <div class="sidebar-section-header">
          <span>${i18n.t("sidebar.projects")}</span>
          <div class="section-actions">
            <button class="section-action-btn" id="btnFilterSessions" title="${i18n.t("sidebar.filter")}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></button>
            <button class="section-action-btn" id="btnSelectMode" title="${i18n.t("sidebar.selectAll")}">☐</button>
            <button class="section-action-btn delete-selected-btn" id="batchDeleteBtn" title="${i18n.t("sidebar.deleteSelected")}" style="display:none"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg></button>
          </div>
        </div>
        <div class="sidebar-search" id="sessionSearch" style="display:none">
          <input type="text" id="sessionSearchInput" placeholder="${i18n.t("sidebar.searchPlaceholder")}" />
        </div>
        <div class="sessions-list" id="sessionList"></div>
        <div class="sidebar-bottom">
          <div class="mcp-status" id="mcpStatus" style="display:none">
            <span class="mcp-indicator">⚡</span>
            <span class="mcp-label">MCP:</span>
            <span class="mcp-detail" id="mcpDetail"></span>
          </div>
          <button class="sidebar-nav-item" id="btnLang" title="${i18n.t("sidebar.language")}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
            <span>中 / EN</span>
          </button>
          <button class="sidebar-nav-item" id="btnTheme">
            <span class="theme-icon">
              <svg id="themeIconMoon" class="ico-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
              <svg id="themeIconSun" class="ico-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
            </span>
            <span>${i18n.t("sidebar.theme")}</span>
          </button>
          <button class="sidebar-nav-item" id="btnSettings">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            <span>${i18n.t("sidebar.settings")}</span>
          </button>
        </div>
      </aside>
      <main class="ziva-center">
        <div class="ziva-toolbar" id="zivaToolbar">
          <button class="toolbar-sidebar-open" id="btnOpenSidebar" title="${i18n.t("sidebar.openSidebar")}" aria-label="${i18n.t("sidebar.openSidebar")}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
          <div class="toolbar-title" id="toolbarTitle"></div>
          <div class="toolbar-actions">
            <button class="toolbar-right-toggle" id="btnOpenRightPanel" title="${i18n.t("sidebar.togglePanel")}" aria-label="${i18n.t("sidebar.togglePanel")}">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
            </button>
          </div>
        </div>
        <div class="empty-state" id="emptyState"></div>
        <div class="split-container" id="splitContainer">
          <div class="split-pane split-pane-active" id="activePaneContainer">
            <div class="split-pane-header" id="activePaneHeader" style="display:none">
              <span class="split-pane-title" id="activePaneTitle"></span>
              <span class="split-pane-actions">
                <button class="split-pane-enter" id="activePaneEnter" title="Fullscreen" type="button">
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
                </button>
                <button class="split-pane-close" id="activePaneClose" title="Close pane" type="button">×</button>
              </span>
            </div>
            <div class="pane-messages" id="messages"></div>
            <div class="pane-composer" id="activePaneComposer" data-sid="" style="display:none">
              <div id="activePaneComposerInner"></div>
            </div>
          </div>
        </div>
        <div class="ziva-composer-wrapper" id="composerWrapper">
          <div class="pane-composer" id="composerHost"></div>
          <div class="ziva-status-bar" id="statusBar">
            <div class="status-item" id="contextWorkspace" title="${i18n.t("status.workspace")}">
              <span class="status-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-1.22-1.8A2 2 0 0 0 7.53 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg></span>
              <span id="workspaceName">ziva</span>
              <span class="status-chevron"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
            </div>
            <div class="status-item" id="gitBranchContext" title="${i18n.t("status.gitBranch")}">
              <span class="status-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg></span>
              <span id="gitBranchName">main</span>
              <span class="status-chevron"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
            </div>
          </div>
        </div>
      </main>
      <aside class="ziva-right-panel" id="rightPanel">
        <div class="right-panel-body"></div>
      </aside>
    </div>`;

  // The browser shell (tab strip + omnibox + embedded Chromium) only makes
  // sense in the Electron desktop app — in a regular browser, the "web tabs"
  // are just iframes (can't render complex sites). So in web mode, skip the
  // shell entirely and show the ziva chat UI directly (original layout).
  if ((window as any).electronAPI) {
    initBrowserShell();
    // Web-tab "send selection to Ziva": drop {text, url, screenshot} into
    // the active composer.
    (window as any).electronAPI.onBrowserSelection?.((payload: { text: string; url: string; screenshotDataUrl: string }) => {
      void insertBrowserSelection(payload);
    });
  }
  bindEvents();
  // Rewind button delegation: each user message's "↩ 回退" button carries
  // the message's global index in its .msg[data-idx]; route the click to
  // rewindUserMsg (Claude Code-style rewind to that user message).
  document.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest(".rewind-btn, .rewind-btn-tool");
    if (!btn) return;
    const msg = btn.closest(".msg, .tool-card");
    const idx = msg?.getAttribute("data-idx");
    if (idx == null) return;
    const paneSid = msg!.closest("[data-sid]")?.getAttribute("data-sid");
    const sid = paneSid || store.get().activeSid || "";
    if (!sid) return;
    e.stopPropagation();
    const kind = msg!.classList.contains("tool-card") ? "tool" : "user";
    void rewindUserMsg(sid, Number(idx), kind);
  });
  refreshStatus();
  refreshMCPStatus();
  refreshConfig();
  refreshSessions().then(() => {
    const s = store.get();
    const restored = s.activeSid;
    // If the last active session still exists, restore it so reopening Ziva
    // brings the user back to their previous conversation.
    if (restored && s.sessions.some(session => session.id === restored)) {
      switchSession(restored);
    } else if (!s.activeSid) {
      // Only auto-select a session on initial load if the user hasn't already
      // picked one (e.g. by clicking "New Session" while refreshSessions was
      // still in flight). Otherwise we clobber the user's explicit choice and
      // the composer swaps to a different session unexpectedly.
      if (s.sessions.length > 0) {
        switchSession(s.sessions[0].id);
      } else {
        createSession();
      }
    }
  });
}

// ---- Slash Commands ----
const SLASH_COMMANDS = [
  { name: "/new", description: i18n.t("slash.new.desc") },
  { name: "/model", description: i18n.t("slash.model.desc") },
  { name: "/effort", description: "Show or set reasoning effort" },
  { name: "/compact", description: i18n.t("slash.compact.desc") },
  { name: "/prune", description: i18n.t("slash.prune.desc") },
  { name: "/automation", description: i18n.t("slash.automation.desc") },
  { name: "/restart", description: i18n.t("slash.restart.desc") },
];

let slashMenuIndex = -1;
// The sid of the composer whose slash menu is currently open (only one at
// a time — the focused composer). Used by the unified sid-aware slash fns.
let slashMenuSid = "";

// ---- Event Bindings ----
// Per-composer voice input. Reuses the single global MediaRecorder (only
// one recording at a time) and inserts the transcription into THIS
// composer's textarea.
async function startComposerMic(sid: string, btn: HTMLButtonElement) {
  if (!sid) return;
  if (isRecording) { mediaRecorder?.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      isRecording = false;
      btn.classList.remove("recording");
      btn.title = i18n.t("composer.voiceInput");
      const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || "audio/webm" });
      try {
        btn.title = i18n.t("composer.transcribing");
        const formData = new FormData();
        formData.append("audio", blob, "recording.webm");
        const res = await fetch("/api/stt", { method: "POST", body: formData });
        // Surface backend errors as visible cards. Previously these were
        // swallowed by `catch (err)` and the only signal was a console
        // log, so a failed /api/stt looked indistinguishable from a
        // successful but empty transcription — both showed nothing.
        if (!res.ok) {
          let detail = "";
          try { detail = (await res.text()).slice(0, 200); } catch {}
          appendError(i18n.t("toast.sttFailedStatus", { status: res.status, detail: detail || res.statusText }));
          return;
        }
        const data = await res.json();
        const text = typeof data.text === "string" ? data.text.trim() : "";
        if (text) {
          const ta = composerTextarea(sid);
          if (ta) {
            ta.value = ta.value ? ta.value + "\n" + text : text;
            ta.dispatchEvent(new Event("input"));
            ta.focus();
          }
        } else {
          // Whisper returned an empty string — recording was too short,
          // silent, or no speech detected. Surface a hint so the user
          // doesn't think the button is broken.
          const ta = composerTextarea(sid);
          const hint = i18n.t("toast.noSpeech");
          if (ta) {
            ta.placeholder = hint;
            setTimeout(() => {
              if (ta.placeholder === hint) ta.placeholder = "";
            }, 5000);
          } else {
            appendError(hint);
          }
          console.warn("STT returned empty text — recording likely had no detectable speech.");
        }
      } catch (err: any) {
        appendError(i18n.t("toast.sttFailed", { err: err?.message || err }));
        console.error("STT failed:", err);
      } finally {
        btn.title = i18n.t("composer.voiceInput");
      }
    };
    mediaRecorder.start();
    isRecording = true;
    btn.classList.add("recording");
    btn.title = i18n.t("composer.stopRecording");
  } catch (err: any) {
    console.error("Mic failed:", err);
  }
}

// ONE delegated listener on document routes every composer's events by
// `data-sid`. This single handler serves the full-screen composer and
// every split pane — no per-element handlers, no #splitContainer delegation.
function bindComposerEvents() {
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const sendBtn = target.closest(".pane-send") as HTMLButtonElement | null;
    if (sendBtn) {
      const sid = sendBtn.dataset.sid || "";
      if (sendBtn.classList.contains("stop-btn")) cancelComposerTurn(sid);
      else sendComposerMessage(sid);
      return;
    }
    if (target.closest(".pane-btn-attach")) {
      const sid = (target.closest("[data-sid]") as HTMLElement | null)?.dataset.sid || "";
      const input = composerFileInput(sid);
      if (input) input.click();
      return;
    }
    const micBtn = target.closest(".pane-btn-mic") as HTMLButtonElement | null;
    if (micBtn) {
      const sid = (target.closest("[data-sid]") as HTMLElement | null)?.dataset.sid || "";
      startComposerMic(sid, micBtn);
      return;
    }
    const slashItem = target.closest(".slash-item") as HTMLElement | null;
    if (slashItem) {
      const sid = (slashItem.closest("[data-sid]") as HTMLElement | null)?.dataset.sid || slashMenuSid;
      if (sid) selectSlashCommandFor(sid);
      return;
    }
    if (target.closest(".pending-bar-clear")) {
      const sid = (target.closest("[data-sid]") as HTMLElement | null)?.dataset.sid || "";
      clearComposerPending(sid);
      return;
    }
    if (target.closest(".pending-bar-text")) {
      const sid = (target.closest("[data-sid]") as HTMLElement | null)?.dataset.sid || "";
      editComposerPending(sid);
      return;
    }
  });

  document.addEventListener("keydown", (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("pane-prompt")) return;
    const sid = (target as HTMLTextAreaElement).dataset.sid || "";
    const menu = composerSlashEl(sid);
    const menuOpen = !!menu && menu.style.display === "block";
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (menuOpen) { selectSlashCommandFor(sid); return; }
      if (isSessionRunning(sid)) queueComposerMessage(sid); else sendComposerMessage(sid);
      return;
    }
    if (e.key === "Escape") {
      if (menuOpen) { hideSlashMenuFor(sid); return; }
      if (isSessionRunning(sid)) cancelComposerTurn(sid);
      return;
    }
    if (menuOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      moveSlashSelectionFor(sid, e.key === "ArrowDown" ? 1 : -1);
    }
  });

  document.addEventListener("input", (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("pane-prompt")) return;
    const textarea = target as HTMLTextAreaElement;
    const sid = textarea.dataset.sid || "";
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
    if (sid) setDraftText(sid, textarea.value);
    const cc = composerCharCount(sid);
    if (cc) cc.textContent = textarea.value.length > 0 ? String(textarea.value.length) : "";
    if (textarea.value.startsWith("/")) showSlashMenuFor(sid, textarea.value);
    else hideSlashMenuFor(sid);
  });

  document.addEventListener("change", async (e) => {
    const target = e.target as HTMLElement;
    if (target.classList.contains("pane-model")) {
      const sel = target as HTMLSelectElement;
      const sid = sel.dataset.sid || "";
      const raw = sel.value;  // "provider|model" (grouped) or "model" (flat)
      if (sid) {
        const sep = raw.indexOf("|");
        const provider_name = sep > 0 ? raw.slice(0, sep) : undefined;
        const model = sep > 0 ? raw.slice(sep + 1) : raw;
        // Capture previous so we can revert on PATCH failure — silently
        // swallowing left UI and backend out of sync.
        const { sessions } = store.get();
        const s = sessions.find(x => x.id === sid);
        const prevModel = s ? (s as any).model_name : undefined;
        const prevProvider = s ? (s as any).provider_name : undefined;
        try {
          await api.updateSession(sid, { model_name: model, ...(provider_name ? { provider_name } : {}) });
          if (s) { (s as any).model_name = model; (s as any).provider_name = provider_name ?? null; }
          // Rebuild the effort dropdown for the new model (its effort_levels
          // + downgrade the current level if unsupported).
          hydrateComposer(sid);
        } catch (err: any) {
          if (s && prevModel !== undefined) { (s as any).model_name = prevModel; (s as any).provider_name = prevProvider ?? null; }
          sel.value = prevProvider ? `${prevProvider}|${prevModel}` : (prevModel ?? sel.value);
          appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
          console.error("updateSession(model_name) failed:", err);
        }
      }
      return;
    }
    if (target.classList.contains("pane-effort")) {
      const sel = target as HTMLSelectElement;
      const sid = sel.dataset.sid || "";
      const thinking_mode = sel.value;
      if (sid) {
        const { sessions } = store.get();
        const s = sessions.find(x => x.id === sid);
        try {
          await api.updateSession(sid, { thinking_mode });
          if (s) (s as any).thinking_mode = thinking_mode;
        } catch (err: any) {
          appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
          console.error("updateSession(thinking_mode) failed:", err);
        }
      }
      // The selected label changed (e.g. low → medium); refit so a longer
      // label isn't ellipsised to "m..." by the stale, narrower width.
      fitSelectWidth(sel);
      return;
    }
    if (target.classList.contains("pane-approval")) {
      const sel = target as HTMLSelectElement;
      const sid = sel.dataset.sid || "";
      const policy = sel.value;
      fitSelectWidth(sel);
      if (sid) {
        const { sessions } = store.get();
        const s = sessions.find(x => x.id === sid);
        const prevPolicy = s ? (s as any).approval_policy : undefined;
        try {
          await api.updateSession(sid, { approval_policy: policy });
          if (s) (s as any).approval_policy = policy;
        } catch (err: any) {
          if (s && prevPolicy !== undefined) (s as any).approval_policy = prevPolicy;
          sel.value = prevPolicy ?? sel.value;
          fitSelectWidth(sel);
          appendError(i18n.t("toast.approvalChangeFailed", { err: err?.message || err }));
          console.error("updateSession(approval_policy) failed:", err);
        }
      }
      return;
    }
    if (target.classList.contains("pane-image-input")) {
      const files = (target as HTMLInputElement).files;
      const sid = (target as HTMLInputElement).dataset.sid || "";
      if (files) for (const f of Array.from(files)) await addImageFile(f, sid);
      (target as HTMLInputElement).value = "";
    }
  });

  document.addEventListener("paste", (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("pane-prompt")) return;
    const sid = (target as HTMLTextAreaElement).dataset.sid || "";
    const files = (e as ClipboardEvent).clipboardData?.files;
    if (!files || files.length === 0) return;
    e.preventDefault();
    for (const f of Array.from(files)) addImageFile(f, sid);
  });

  document.addEventListener("dragover", (e) => {
    const target = e.target as HTMLElement;
    if (target.classList.contains("pane-prompt")) e.preventDefault();
  });

  document.addEventListener("drop", (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("pane-prompt")) return;
    e.preventDefault();
    const sid = (target as HTMLTextAreaElement).dataset.sid || "";
    const files = (e as DragEvent).dataTransfer?.files;
    if (files) for (const f of Array.from(files)) addImageFile(f, sid);
  });
}

function bindEvents() {
  // ---- Composer ----
  // One delegated listener (bound once) routes every composer's events by
  // data-sid — full-screen and every split pane share it. No per-element
  // handlers, no element IDs inside the composer.
  bindComposerEvents();

  // ---- Sidebar / modals / theme / keyboard ----
  $("btnNewSession").onclick = () => createSession();
  $("btnOpenRightPanel").onclick = toggleRightPanel;
  initResizablePanel();
  // Reactively re-paint the Plan panel whenever store.currentPlanSteps
  // or activeSid changes — replaces the previous imperative path that
  // raced with itself across SSE handlers and loadHistory calls.
  ensurePlanSubscriber();

  $("btnSkills").onclick = () => openSkillsBrowser();
  $("btnScheduled").onclick = () => openAutomationsModal();
  $("btnConnectIM").onclick = () => openIMBridgeModal();
  $("btnSettings").onclick = () => openSettingsModal();

  function updateThemeIcon(_theme: string) {
    // Icon visibility is now driven purely by CSS via [data-theme] on <html>
    // (see .theme-icon rules in base.css) — sun/moon crossfade on switch.
  }

  $("btnLang").onclick = () => i18n.setLang(i18n.getLang() === "zh" ? "en" : "zh");

  $("btnTheme").onclick = () => {
    const current = store.get().theme;
    const next = current === "dark" ? "light" : "dark";
    store.set({ theme: next });
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("ziva-theme", next);
    updateThemeIcon(next);
    (window as any).electronAPI?.setTheme?.(next);
  };
  // Sync icon with saved theme on startup
  updateThemeIcon(store.get().theme);

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
  };

  $("batchDeleteBtn").onclick = async () => {
    const checked = $("sessionList").querySelectorAll<HTMLInputElement>(".session-checkbox:checked");
    const items = Array.from(checked).map(cb => ({ sid: cb.dataset.sid!, workspace: cb.dataset.workspace }));
    if (items.length === 0) return;
    if (!confirm(i18n.t("confirm.deleteSessions", { n: items.length }))) return;
    const { activeSid } = store.get();
    for (const { sid, workspace } of items) {
      await api.deleteSession(sid, workspace ? { workspace } : undefined);
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
    if (items.some(it => it.sid === activeSid)) {
      const sessions = store.get().sessions;
      if (sessions.length > 0) {
        switchSession(sessions[0].id);
      }
    }
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
    if ((e.metaKey || e.ctrlKey) && e.key === "d") { e.preventDefault(); toggleRightPanel(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "b") { e.preventDefault(); toggleSidebar(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "n") { e.preventDefault(); createSession(); }
    if (e.key === "Escape") {
      if (document.getElementById("settingsModalBackdrop")) {
        e.preventDefault();
        closeSettingsModal();
      } else if (document.getElementById("skillsModalBackdrop")) {
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

  const savedTheme = localStorage.getItem("ziva-theme") as "dark" | "light" | null;
  if (savedTheme) {
    store.set({ theme: savedTheme });
    document.documentElement.setAttribute("data-theme", savedTheme);
    (window as any).electronAPI?.setTheme?.(savedTheme);
  }
  // Mic is handled per-composer by bindComposerEvents (startComposerMic).
}

// ---- Image Attachments ----
//
// We don't push base64 around the wire anymore. The user pastes or
// drops a file → we POST it to /sessions/{sid}/attachments → the
// server drops the bytes under
// ``~/.ziva/sessions/<pid>/attachments/<sid>/`` and hands back the
// absolute path. We keep that path; the next /turns request embeds
// it in an `image_url.url` block. The runtime then expands the
// path to a base64 data URL *only* in the per-turn copy sent to
// the provider — the persisted history keeps the path so reloads
// don't have to re-read multi-MB blobs from disk.
//
// The local `thumbUrl` is a blob URL just for the in-input preview;
// it's never sent to the server. Image attachments are stored
// per-session in `store.pendingSessionImages[sid]`; the `pendingImages`
// array is the *active* session's mirror copy, kept in sync on every
// read/write so the global composer can show previews while the
// store remains the source of truth.

// Track in-flight image uploads so they can be cancelled when the user
// switches sessions. Each entry ties a fetch to the session it was
// initiated for.
const inFlightUploads: Array<{
  sid: string;
  controller: AbortController;
  thumbUrl: string;
}> = [];

// Cancel all in-flight image uploads for sessions other than the newly
// active one. Called from switchSession to prevent stale uploads from
// completing and pushing results into the wrong session's pendingImages.
function cancelInFlightUploads(keepSid: string) {
  for (let i = inFlightUploads.length - 1; i >= 0; i--) {
    const entry = inFlightUploads[i];
    if (entry.sid !== keepSid) {
      entry.controller.abort();
      URL.revokeObjectURL(entry.thumbUrl);
      inFlightUploads.splice(i, 1);
    }
  }
}

// Abort the in-flight upload that produced a specific preview thumbnail.
// Called when the user clicks the X on a pending image so the completion
// callback doesn't re-add a revoked/broken thumbnail after cancellation.
function abortImageUpload(thumbUrl: string) {
  const idx = inFlightUploads.findIndex(u => u.thumbUrl === thumbUrl);
  if (idx === -1) return;
  const entry = inFlightUploads[idx];
  entry.controller.abort();
  URL.revokeObjectURL(entry.thumbUrl);
  inFlightUploads.splice(idx, 1);
}

// Free any blob URLs in `arr` whose thumbUrl is NOT still referenced by
// any session's draft or queued images. Called when dropping a set of
// previews (clearComposerPending, sendComposerFromQueue success, etc.)
// to avoid leaking blob URLs — but only the ones truly no longer
// reachable from the store. If another session still holds the thumb,
// leave it alone.
function disposePendingImageThumbs(arr: Array<{ thumbUrl?: string }>) {
  if (!arr || arr.length === 0) return;
  const { promptDrafts, pendingMessages } = store.get();
  const live = new Set<string>();
  for (const sid in promptDrafts) {
    for (const a of promptDrafts[sid]?.images || []) {
      if (a?.thumbUrl) live.add(a.thumbUrl);
    }
  }
  for (const sid in pendingMessages) {
    for (const item of pendingMessages[sid] || []) {
      for (const a of item?.images || []) {
        if (a?.thumbUrl) live.add(a.thumbUrl);
      }
    }
  }
  for (const a of arr) {
    if (a?.thumbUrl && !live.has(a.thumbUrl)) {
      URL.revokeObjectURL(a.thumbUrl);
    }
  }
}

async function addImageFile(file: File, sid?: string) {
  // Resolve the target session: the explicit `sid` (split-pane composer)
  // or the active session. Create one if none exists so the attachment
  // has somewhere to land — same lazy-create flow as the send path.
  let uploadSid = sid || store.get().activeSid;
  if (!uploadSid) {
    try {
      await createSession();
    } catch (e: any) {
      appendError(i18n.t("toast.attachNoSessionCreate", { err: e?.message || "unknown" }));
      return;
    }
    uploadSid = sid || store.get().activeSid;
    if (!uploadSid) {
      appendError(i18n.t("toast.attachNoSession"));
      return;
    }
  }

  // Generate a local blob URL for the in-input preview *first*
  // (synchronous, can't fail), then kick off the upload so the user
  // sees the thumbnail immediately even on slow links.
  const thumbUrl = URL.createObjectURL(file);

  const controller = new AbortController();
  const uploadEntry = { sid: uploadSid, controller, thumbUrl };
  inFlightUploads.push(uploadEntry);

  const finish = (result: { path: string; mime: string; size: number } | null, err?: string) => {
    // Remove from in-flight tracker regardless of outcome.
    const idx = inFlightUploads.indexOf(uploadEntry);
    if (idx !== -1) inFlightUploads.splice(idx, 1);

    if (!result) {
      URL.revokeObjectURL(thumbUrl);
      console.error("image upload failed", err);
      appendError(i18n.t("toast.attachFailed", { name: file.name, err: err || "upload failed" }));
      return;
    }

    // The image belongs to uploadSid's composer regardless of whether
    // the user has since switched sessions — it is a live draft
    // attachment for that session, restored from the draft on return.
    const image = { ...result, name: file.name, thumbUrl };
    setDraftImages(uploadSid, [...draftImages(uploadSid), image]);
    renderComposerPreviews(uploadSid);
  };

  // Resolve the absolute path of a file chosen via <input type="file"> so we
  // can hand the runtime the real path and skip copying it into the
  // attachments dir. Electron 35 removed File.path; webUtils.getPathForFile
  // (exposed by the preload bridge) is the replacement, with File.path as a
  // fallback for older Electron. Clipboard-paste blobs have neither → upload.
  const localPath = (window as any).electronAPI?.getPathForFile?.(file) || (file as any).path;
  if (localPath) {
    finish({ path: localPath, mime: file.type, size: file.size });
  } else {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fetch(`/sessions/${uploadSid}/attachments`, {
      method: "POST",
      body: fd,
      signal: controller.signal,
    })
      .then(async (r) => {
        if (!r.ok) {
          const detail = await r.text().catch(() => r.statusText);
          finish(null, detail || `HTTP ${r.status}`);
          return;
        }
        const j = await r.json();
        finish({ path: j.path, mime: j.mime, size: j.size });
      })
      .catch((e) => {
        if ((e as DOMException).name === "AbortError") {
          const idx = inFlightUploads.indexOf(uploadEntry);
          if (idx !== -1) inFlightUploads.splice(idx, 1);
          return;
        }
        finish(null, String(e));
      });
  }
}

// ---- Split-screen sessions ----
// Show multiple sessions side-by-side. The active session always renders
// live in #messages; secondary sessions get their own pane that reloads
// from history when background turns finish.
function isSplitActive(): boolean {
  return store.get().splitSessions.length > 0;
}

function addToSplit(sid: string) {
  const { splitSessions, activeSid } = store.get();
  if (sid === activeSid) return;
  if (splitSessions.includes(sid)) return;
  store.set({ splitSessions: [...splitSessions, sid] });
  renderSplitPanes().catch((e) => {
    console.error("addToSplit: renderSplitPanes REJECTED:", e);
    if (e instanceof Error) console.error("  stack:", e.stack);
  });
}

function removeFromSplit(sid: string) {
  const next = store.get().splitSessions.filter(s => s !== sid);
  store.set({ splitSessions: next });
  renderSplitPanes();
}

function clearSplit() {
  store.set({ splitSessions: [] });
  renderSplitPanes();
}

function refreshSplitPane(sid: string) {
  const pane = document.querySelector(`.split-pane-secondary[data-sid="${sid}"] .pane-messages`) as HTMLElement | null;
  if (!pane) return;
  loadHistoryInto(sid, pane);
}

// Per-pane composer inner HTML. Same structure as the global #composerWrapper
// (pending bar, image previews, hidden file input, textarea, toolbar with
// +/mode/model/context/mic/send) so each split pane can operate
// independently: own attachments, own model+approval, own draft.
// ---------------------------------------------------------------------------
// Unified composer. ONE template + ONE sid-parameterized selector layer,
// shared by the full-screen composer and every split-pane composer. A
// session is just a (messages container + composer) pair addressed by
// `sid`; there is no active/background distinction in the code — only in
// layout (full-screen = one session in the wide area; split = N sessions
// side by side). This replaces the former parallel "global IDs" and
// "pane-* classes" implementations.
// ---------------------------------------------------------------------------

// The single composer template. Every interactive element carries
// `data-sid` so one delegated listener can route events to the right
// session, and several composers can coexist in split mode. It reuses the
// established `.pane-*` classes so the same CSS styles full-screen and
// pane composers identically. Ring geometry is unified to r=11 / 69.12.
function composerTemplate(sid: string): string {
  return `
    <div class="pending-bar pane-pending" data-sid="${esc(sid)}" hidden>
      <span class="pending-bar-label">${i18n.t("composer.queued")}</span>
      <span class="pending-bar-text"></span>
      <button class="pending-bar-clear" title="${i18n.t("composer.cancelQueue")}" type="button">×</button>
    </div>
    <div class="image-previews pane-previews" data-sid="${esc(sid)}" style="display:none"></div>
    <input type="file" class="pane-image-input" data-sid="${esc(sid)}" multiple style="display:none" />
    <textarea class="pane-prompt" data-sid="${esc(sid)}" placeholder="${i18n.t("composer.placeholder")}" rows="1"></textarea>
    <div class="slash-menu pane-slash" data-sid="${esc(sid)}" style="display:none"></div>
    <div class="composer-toolbar">
      <div class="toolbar-left">
        <button class="composer-action-btn pane-btn-attach" data-sid="${esc(sid)}" title="${i18n.t("composer.attachTitle")}">+</button>
        <select class="pane-approval" data-sid="${esc(sid)}" title="${i18n.t("composer.modeTitle")}">
          <option value="suggest">${i18n.t("composer.modeSuggest")}</option>
          <option value="full-auto">${i18n.t("composer.modeFullAuto")}</option>
        </select>
        <select class="pane-model" data-sid="${esc(sid)}" title="${i18n.t("composer.modelTitle")}"></select>
        <select class="pane-effort" data-sid="${esc(sid)}" title="Reasoning effort">
          <option value="disabled">off</option>
          <option value="low">low</option>
          <option value="medium">med</option>
          <option value="high">high</option>
          <option value="xhigh">xhigh</option>
          <option value="max">max</option>
        </select>
      </div>
      <div class="toolbar-right">
        <span class="char-count pane-charcount" data-sid="${esc(sid)}"></span>
        <div class="context-ring" title="${i18n.t("composer.contextTitle")}">
          <svg viewBox="0 0 24 24" width="28" height="28">
            <circle cx="12" cy="12" r="11" fill="none" stroke="var(--line)" stroke-width="2.5" />
            <circle cx="12" cy="12" r="11" fill="none" stroke="var(--accent)" stroke-width="2.5"
              stroke-dasharray="69.12" stroke-dashoffset="69.12" stroke-linecap="round"
              transform="rotate(-90 12 12)" class="pane-context-arc" data-sid="${esc(sid)}" />
          </svg>
          <span class="context-pct pane-context-pct" data-sid="${esc(sid)}"></span>
        </div>
        <button class="composer-action-btn mic-btn pane-btn-mic" data-sid="${esc(sid)}" title="${i18n.t("composer.voiceInput")}">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
        </button>
        <button class="pane-send" data-sid="${esc(sid)}" title="${i18n.t("composer.send")}">→</button>
      </div>
    </div>
  `;
}

// Resolve a session's messages container with ONE uniform selector. The
// active session's container (#activePaneContainer, tagged with its sid in
// renderSplitPanes) holds #messages — itself a .pane-messages — so this
// matches the full-screen session and every split pane identically.
function sessionMessagesEl(sid: string): HTMLElement | null {
  if (!sid) return null;
  return document.querySelector(`.split-pane[data-sid="${sid}"] .pane-messages`) as HTMLElement | null;
}

// Sid-keyed composer selectors (all reuse the `.pane-*` classes emitted by
// composerTemplate). The unified behavior layer uses these exclusively so
// it never references element IDs and never assumes "active".
function composerTextarea(sid: string): HTMLTextAreaElement | null {
  return document.querySelector(`.pane-prompt[data-sid="${sid}"]`) as HTMLTextAreaElement | null;
}
function composerSendBtn(sid: string): HTMLButtonElement | null {
  return document.querySelector(`.pane-send[data-sid="${sid}"]`) as HTMLButtonElement | null;
}
function composerAttachBtn(sid: string): HTMLButtonElement | null {
  return document.querySelector(`.pane-btn-attach[data-sid="${sid}"]`) as HTMLButtonElement | null;
}
function composerFileInput(sid: string): HTMLInputElement | null {
  return document.querySelector(`.pane-image-input[data-sid="${sid}"]`) as HTMLInputElement | null;
}
function composerModelSelect(sid: string): HTMLSelectElement | null {
  return document.querySelector(`.pane-model[data-sid="${sid}"]`) as HTMLSelectElement | null;
}
function composerApprovalSelect(sid: string): HTMLSelectElement | null {
  return document.querySelector(`.pane-approval[data-sid="${sid}"]`) as HTMLSelectElement | null;
}

function composerPreviewsEl(sid: string): HTMLElement | null {
  return document.querySelector(`.pane-previews[data-sid="${sid}"]`) as HTMLElement | null;
}
function composerPendingEl(sid: string): HTMLElement | null {
  return document.querySelector(`.pane-pending[data-sid="${sid}"]`) as HTMLElement | null;
}
function composerSlashEl(sid: string): HTMLElement | null {
  return document.querySelector(`.pane-slash[data-sid="${sid}"]`) as HTMLElement | null;
}
function composerCharCount(sid: string): HTMLElement | null {
  return document.querySelector(`.pane-charcount[data-sid="${sid}"]`) as HTMLElement | null;
}
function composerContextArc(sid: string): SVGCircleElement | null {
  return document.querySelector(`.pane-context-arc[data-sid="${sid}"]`) as SVGCircleElement | null;
}
function composerContextPct(sid: string): HTMLElement | null {
  return document.querySelector(`.pane-context-pct[data-sid="${sid}"]`) as HTMLElement | null;
}
function composerMicBtn(sid: string): HTMLButtonElement | null {
  return document.querySelector(`.pane-btn-mic[data-sid="${sid}"]`) as HTMLButtonElement | null;
}

async function renderSplitPanes() {
  const container = $("splitContainer");
  const { splitSessions, sessions, activeSid } = store.get();
  const isMulti = splitSessions.length > 0;
  container.classList.toggle("multi", isMulti);
  container.classList.toggle("has-many", splitSessions.length >= 3);
  // The shared #composerWrapper + status bar are hidden in split mode
  // (see .ziva-center.multi CSS); each pane gets its own composer.
  document.querySelector(".ziva-center")?.classList.toggle("multi", isMulti);
  // If we just entered split mode, clear any stale inline display:none so
  // the CSS rules that show the active pane's per-pane placeholder win.
  if (isMulti) $("messages").style.display = "";

  // Tag the active session's container with its sid so the unified
  // sessionMessagesEl(sid) selector resolves #messages (the active
  // session's .pane-messages) the same way it resolves any secondary
  // pane — no active/background special-casing elsewhere.
  const activeContainer = $("activePaneContainer");
  if (activeContainer) activeContainer.dataset.sid = activeSid || "";

  // Tear down any stale secondary panes that are no longer in splitSessions.
  Array.from(container.querySelectorAll<HTMLElement>(".split-pane-secondary")).forEach((el) => {
    const sid = el.dataset.sid;
    if (sid && !splitSessions.includes(sid)) {
      el.remove();
    }
  });

  // ── Active pane: header visibility, title, fullscreen / close buttons ──
  // The active session is always rendered into #messages (the original
  // global messages container). The header above #messages shows only
  // when there are secondary panes (single-pane mode = no header).
  const activeHeader = $("activePaneHeader") as HTMLElement;
  if (activeHeader) activeHeader.style.display = isMulti ? "flex" : "none";
  const activeTitle = $("activePaneTitle") as HTMLElement;
  const activePane = $("activePane") as HTMLElement;
  const activeS = sessions.find((x) => x.id === activeSid);
  if (activeTitle) activeTitle.textContent = activeS?.preview || activeSid || "";
  if (activePane) activePane.dataset.sid = activeSid || "";
  // The reconciliation loop skips the active session, so a just-created
  // or freshly-compacted active session can still be showing the id stub.
  // Trigger a one-shot preview refresh if needed (it'll re-render itself).
  if (activeSid && activeS && (!activeS.preview || ID_STUB_RE.test(activeS.preview))) {
    refreshSessionPreview(activeSid);
  }

  // Active pane's per-pane composer: show in split mode, hide in single-pane
  // (single-pane uses the shared #composerWrapper at the bottom).
  const activePaneComposer = $("activePaneComposer") as HTMLElement;
  const activeComposerInner = $("activePaneComposerInner") as HTMLElement;
  if (isMulti && activeSid) {
    if (activePaneComposer) {
      activePaneComposer.style.display = "flex";
      activePaneComposer.dataset.sid = activeSid;
    }
    if (activeComposerInner) {
      mountComposer(activeSid, activeComposerInner);
    }
  } else {
    if (activePaneComposer) activePaneComposer.style.display = "none";
    // Fullscreen: the active pane composer is unused (#composerHost takes
    // over). Clear any stale composer so it can't shadow #composerHost.
    if (activeComposerInner && activeComposerInner.dataset.sid) {
      activeComposerInner.replaceChildren();
      activeComposerInner.dataset.sid = "";
    }
  }
  setComposerRunning(activeSid || "", !!store.get().runningSessions[activeSid || ""]);

  // In split mode, show a placeholder inside the active pane when it has no
  // messages. (Single-pane mode uses the global empty-state area instead.)
  if (isMulti && activeSid && $("messages").children.length === 0) {
    setPaneEmptyPlaceholder($("messages"));
  }

  // Wire up the active pane header buttons (↗ fullscreen, × close).
  // Always wired (not just on first render) so the handlers survive
  // any innerHTML rewrites of the messages container.
  const activeEnter = $("activePaneEnter") as HTMLButtonElement | null;
  const activeClose = $("activePaneClose") as HTMLButtonElement | null;
  if (activeEnter) {
    activeEnter.onclick = () => {
      // Save the active pane's draft before exiting split mode, so the
      // user's unsent text isn't lost when the per-pane composer is
      // replaced by the shared global one.
      if (activeSid) {
        // The per-keystroke input handler already keeps promptDrafts in
        // sync, so the draft is current; no explicit save needed here.
      }
      // Fullscreen: drop all secondary sessions.
      store.set({ splitSessions: [] });
      renderSplitPanes();
      // Mount/hydrate the full-screen composer for the active session so
      // the user sees their saved draft.
      if (activeSid) {
        renderComposers();
        showEmptyState($("messages").children.length === 0);
      }
    };
  }
  if (activeClose) {
    activeClose.onclick = async () => {
      // "Close" the active pane: if there are secondary sessions,
      // promote the first one to be the new active session and drop
      // it from the split list. If there are no secondary sessions,
      // the close button is hidden anyway (single-pane fullscreen).
      const { splitSessions: ss } = store.get();
      if (ss.length > 0) {
        const promote = ss[0];
        const remaining = ss.slice(1);
        store.set({ splitSessions: remaining, activeSid: promote });
        renderSessions();
        renderSplitPanes();
        // Re-load history into the active pane for the new active session
        const newActive = store.get().activeSid;
        if (newActive) {
          await loadHistoryInto(newActive, $("messages"));
          showEmptyState($("messages").children.length === 0);
        }
      }
    };
  }

  // ── Render secondary panes ──
  // Secondary panes are READ-ONLY: a header (↗ + ×) + the session's
  // history, with NO composer. Live SSE events for the active session
  // flow into #messages; secondary sessions are refreshed from history
  // when their turns start/end (via refreshSplitPane).
  for (const sid of splitSessions) {
    try {
    if (container.querySelector(`.split-pane-secondary[data-sid="${sid}"]`)) continue; // already mounted
    const s = sessions.find((x) => x.id === sid);
    const pane = document.createElement("div");
    pane.className = "split-pane split-pane-secondary";
    pane.dataset.sid = sid;
    // If we don't have a real preview yet (id stub), kick off a refresh
    // in the background — it'll re-render once the user message arrives.
    if (s && (!s.preview || ID_STUB_RE.test(s.preview))) {
      refreshSessionPreview(sid);
    }
    pane.innerHTML = `
      <div class="split-pane-header">
        <span class="split-pane-title">${esc(s?.preview || sid)}</span>
        <span class="split-pane-actions">
          <button class="split-pane-enter" title="Fullscreen" type="button">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
          </button>
          <button class="split-pane-close" title="Close pane" type="button">×</button>
        </span>
      </div>
      <div class="pane-messages"></div>
      <div class="pane-composer" data-sid="${sid}"></div>
    `;
    // ↗ button: fullscreen — make this pane the only visible one.
    const enterBtn = pane.querySelector(".split-pane-enter") as HTMLButtonElement;
    enterBtn.onclick = () => {
      const { activeSid: curActive } = store.get();
      store.set({ activeSid: sid, splitSessions: [] });
      renderSessions();
      renderSplitPanes();
      // Reload history for the newly-active session and sync empty-state.
      loadHistoryInto(sid, $("messages")).then(() => {
        showEmptyState($("messages").children.length === 0);
      });
    };
    // × button: remove this pane from the split.
    const closeBtnEl = pane.querySelector(".split-pane-close") as HTMLButtonElement;
    closeBtnEl.onclick = async () => {
      const { splitSessions: cur, activeSid: curActive } = store.get();
      const remaining = cur.filter((s2) => s2 !== sid);
      // If we just removed the only secondary and active was it, fall
      // back to the most-recent remaining secondary (if any) as the new
      // active. Otherwise the user would lose the only open session.
      if (curActive === sid && remaining.length > 0) {
        store.set({ splitSessions: remaining.slice(1), activeSid: remaining[0] });
        renderSessions();
        renderSplitPanes();
        await loadHistoryInto(remaining[0], $("messages"));
        showEmptyState($("messages").children.length === 0);
      } else if (curActive === sid && remaining.length === 0) {
        // No more sessions visible — keep active as the removed one
        // (the sidebar still has it); just exit split mode.
        store.set({ splitSessions: [] });
        renderSplitPanes();
        showEmptyState($("messages").children.length === 0);
      } else {
        // Closing a non-active secondary pane. The active session is
        // unchanged and its messages are still in #messages, but
        // entering split mode can have flipped `.ziva-center.has-messages`
        // to true (loadHistoryInto on the secondary pane calls
        // showEmptyState(false) unconditionally on appendUserMsg), and
        // CSS then hides both the global empty-state and the per-pane
        // placeholder — leaving the active pane blank when it actually
        // is empty. Sync the empty-state class with the actual messages
        // count so the user sees the right thing post-close.
        store.set({ splitSessions: remaining });
        renderSplitPanes();
        // Entering split mode injects a per-pane placeholder into
        // #messages when the active session is empty. After exiting,
        // a single child = the placeholder (not a real message), so
        // strip it and let the global empty-state surface instead.
        const msgs = $("messages");
        if (msgs.children.length === 1 && msgs.querySelector(".pane-empty-state")) {
          clearPaneEmptyPlaceholder(msgs);
        }
        showEmptyState(msgs.children.length === 0);
      }
    };
    container.appendChild(pane);
    // Load the secondary session's history (read-only — live events are
    // routed via SSE only to the active session's pane).
    const paneMessages = pane.querySelector(".pane-messages") as HTMLElement;
    try {
      await loadHistoryInto(sid, paneMessages);
    } catch (e: any) {
      console.error("renderSplitPanes: loadHistoryInto failed for", sid, e?.message || e, e?.stack);
    }
    // Empty-session placeholder inside the pane so split panes don't look broken.
    if (paneMessages && paneMessages.children.length === 0) {
      setPaneEmptyPlaceholder(paneMessages);
    }
    // Mount the unified composer for this pane (hydrates model/approval/
    // draft/running state). Same template + behavior as the full-screen one.
    mountComposer(sid, pane.querySelector(".pane-composer") as HTMLElement);
    // If this session is already running (e.g. an IM-driven turn started
    // before the pane was opened), the composer button needs to start as
    // a stop button. Query turns and sync local running state.
    (async () => {
      try {
        const turns = await api.getTurns(sid);
        if (turns.some(t => t.status === "running")) {
          setSessionRunning(sid, true);
          setComposerRunning(sid, true);
        }
      } catch { /* ignore */ }
    })();
    } catch (e: any) {
      console.error("renderSplitPanes: secondary pane creation failed for", sid, e?.message || e, e?.stack);
    }
  }

  // Reconcile the full-screen composer host too. Every path that changes
  // activeSid / splitSessions goes through renderSplitPanes, so doing it
  // here guarantees #composerHost is cleared (no stale composer bound to a
  // previous session) when there's no active session, and mounted when
  // there is — covering workspace switch + session delete, not just switch.
  renderComposers();
}


// ---- Config ----
async function refreshConfig() {
  try {
    const cfg = await api.getConfig();
    const modelDetails = (cfg.model as any).models || (cfg.model.available || []).map((m: string) => ({ name: m, capabilities: { vision: true } }));
    store.set({ config: { ...store.get().config, model: cfg.model.current, providerName: (cfg.model as any).provider_name || "", models: cfg.model.available, modelDetails, approval: cfg.approval.current, contextWindow: cfg.context_window || 200000 } });
    // Mount/hydrate the full-screen composer with the freshly-loaded model
    // list + current selection. (Pane composers are hydrated by renderSplitPanes.)
    renderComposers();
    updateImageSupport();
  } catch { /* server not running */ }
}

function updateImageSupport() {
  // The attach button is always present in composerTemplate; model vision
  // capability is checked server-side. Nothing to toggle here anymore.
}

// ---- Sessions ----
async function refreshSessions() {
  const raw = await api.listSessions();
  const currentSessions = store.get().sessions;
  const existingMap = new Map(currentSessions.map(s => [s.id, s]));
  const activeWs = store.get().config.workspace || "";

  const sessions = raw.map(s => {
    const existing = existingMap.get(s.id);
    // Prefer the user-renamed `name` from disk, then the in-memory
    // preview, then a stub. Cross-project sessions without a name show
    // up as a short id stub until the user opens the project.
    const preview = existing?.preview || (s as any).name || s.id.slice(0, 8) + "...";
    return {
      ...s,
      preview,
      turnCount: existing?.turnCount || 0,
      status: (existing?.status as api.Session["status"]) || "idle",
      time: s.time || undefined,
    };
  });

  // Always sort by creation time so sessions never jump when a turn completes
  sessions.sort((a, b) => (b.time?.created || 0) - (a.time?.created || 0));

  // The server returns the workspace list alongside sessions; keep it so
  // the sidebar can render empty projects too.
  let recentWorkspaces: string[] = [];
  try {
    const wsRes = await api.getRecentWorkspaces();
    recentWorkspaces = wsRes.workspaces || [];
  } catch { /* ignore */ }

  store.set({ sessions, recentWorkspaces });
  renderSessions();
  // Enrichment (preview / turnCount / status). Preview reads the first
  // user message so historical sessions (which have no on-disk `name`)
  // still get a meaningful title — new sessions are stamped with a name
  // server-side, but old ones aren't, and (s as any).name would fall
  // through to the id stub for them.
  const toEnrich = sessions
    .filter(s => (s.workspace || activeWs) === activeWs)
    .slice(0, 10);
  for (const s of toEnrich) {
    try {
      const msgData = await api.getMessages(s.id);
      const msgs = msgData.messages || [];
      let userMsg = msgs.find(m => m.role === "user");
      // If compacted, filtered view may have no user messages — check full history
      if (!userMsg) {
        const fullData = await api.getMessages(s.id, { includeDropped: true });
        userMsg = (fullData.messages || []).find(m => m.role === "user");
      }
      s.preview = userMsg ? previewText(userMsg.content) : i18n.t("session.empty");
      const turns = await api.getTurns(s.id);
      s.turnCount = turns.length;
      const hasRunning = turns.some(t => t.status === "running");
      s.status = hasRunning ? "running" : turns.length > 0 ? "done" : "idle";
    } catch {
      s.preview = i18n.t("session.fallback");
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
  const { sessions, activeSid, config } = store.get();
  const activeWs = config.workspace || "";
  // /sessions/{sid}/messages is scoped to the active project, so skip
  // cross-project sessions here — they only get a meaningful preview
  // after the user switches to that project.
  const needTitle = sessions.filter(s =>
    s.id !== activeSid &&
    (s.workspace || activeWs) === activeWs &&
    (!s.preview || ID_STUB_RE.test(s.preview))
  );
  for (const s of needTitle) {
    await refreshSessionPreview(s.id);
  }
}, 5000);

interface SessionGroup { label: string; workspace: string; sessions: api.Session[]; totalCount: number; trimmed: boolean }

function projectDisplayName(workspace: string): string {
  if (!workspace) return "Project";
  return workspace.split("/").filter(Boolean).pop() || workspace;
}

let _renderSessionsTimer: ReturnType<typeof setTimeout> | null = null;
export function renderSessions() {
  if (_renderSessionsTimer) clearTimeout(_renderSessionsTimer);
  _renderSessionsTimer = setTimeout(() => {
    _renderSessionsTimer = null;
    _doRenderSessions();
  }, 30);
}
function _doRenderSessions() {
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

  // Group sessions by workspace so the sidebar can show every project at
  // once. Sessions missing a `workspace` field are bucketed under the
  // active workspace to preserve the old behavior.
  const activeWs = config.workspace || "";
  const groupMap = new Map<string, api.Session[]>();
  for (const s of filtered) {
    const ws = s.workspace || activeWs || "unknown";
    if (!groupMap.has(ws)) groupMap.set(ws, []);
    groupMap.get(ws)!.push(s);
  }
  // Also show recent workspaces that have no sessions yet, so a user who
  // switches to a brand-new project sees it in the sidebar immediately.
  for (const ws of store.get().recentWorkspaces) {
    if (ws && !groupMap.has(ws)) {
      groupMap.set(ws, []);
    }
  }
  if (activeWs && !groupMap.has(activeWs)) {
    groupMap.set(activeWs, []);
  }
  // Stable display order: active workspace first, then the rest sorted by
  // most-recently-updated session in each group.
  const groups: SessionGroup[] = Array.from(groupMap.entries()).map(([ws, list_]) => ({
    workspace: ws,
    label: projectDisplayName(ws),
    sessions: list_,
    totalCount: list_.length,
    trimmed: false,
  }));
  groups.sort((a, b) => {
    if (a.workspace === activeWs) return -1;
    if (b.workspace === activeWs) return 1;
    const aMax = a.sessions.reduce((m, s) => Math.max(m, s.time?.updated || s.time?.created || 0), 0);
    const bMax = b.sessions.reduce((m, s) => Math.max(m, s.time?.updated || s.time?.created || 0), 0);
    return bMax - aMax;
  });

  const MAX_COLLAPSED = 15;
  // The "Show all" toggle only applies to the active project — non-active
  // projects are rendered in full (they're usually small).
  const activeGroup = groups.find(g => g.workspace === activeWs);
  if (activeGroup && !showAll && !filter && activeGroup.sessions.length > MAX_COLLAPSED) {
    activeGroup.sessions = activeGroup.sessions.slice(0, MAX_COLLAPSED);
    activeGroup.trimmed = true;
  }

  if (groups.length === 0) {
    const empty = document.createElement("div");
    empty.className = "sessions-empty";
    empty.textContent = i18n.t("sidebar.noConversations");
    list.appendChild(empty);
    return;
  }

  for (const group of groups) {
    const isActive = group.workspace === activeWs;
    const hasRunning = group.sessions.some(s => store.get().runningSessions[s.id] || s.status === "running");
    const projectDiv = document.createElement("div");
    projectDiv.className = "session-project-group" + (isActive ? " active-project" : "");
    const trimmedBadge = group.trimmed
      ? `<span class="project-trimmed">${i18n.t("sidebar.trimmed", { shown: group.sessions.length, total: group.totalCount })}</span>`
      : "";
    projectDiv.innerHTML = `
      <details ${isActive ? "open" : ""}>
        <summary class="project-summary" title="${esc(group.workspace)}">
          <span class="project-chevron">▸</span>
          <span class="project-name">${esc(group.label)}</span>
          ${isActive ? '<span class="project-active-dot" title="Current project"></span>' : ""}
          <span class="project-count">${group.totalCount}</span>
          ${trimmedBadge}
        </summary>
        <div class="project-sessions"></div>
      </details>`;

    const sessionsContainer = projectDiv.querySelector(".project-sessions")!;

    if (group.sessions.length === 0) {
      const empty = document.createElement("div");
      empty.className = "project-sessions-empty";
      empty.textContent = isActive ? i18n.t("sidebar.noConvActive") : i18n.t("sidebar.noConvOther");
      sessionsContainer.appendChild(empty);
    } else {
      for (const s of group.sessions) {
        const div = document.createElement("div");
        div.className = "session-item" + (s.id === activeSid ? " active" : "");
        const timeStr = formatRelativeTime(s.time?.updated || s.time?.created);
        const inActiveWs = (s.workspace || activeWs) === activeWs;
        const isRunning = !!(store.get().runningSessions[s.id] || s.status === "running");
        div.innerHTML = `
          ${selectMode && inActiveWs ? `<input type="checkbox" class="session-checkbox" data-sid="${s.id}" data-workspace="${esc(s.workspace || activeWs)}" checked />` : ""}
          <span class="session-chevron">›</span>
          ${isRunning ? '<span class="session-running-dot" title="Running"></span>' : ""}
          ${s.channel ? `<span class="session-source">${channelIconHtml(s.channel)}</span>` : ""}
          <span class="session-name">${esc(s.preview || s.id)}</span>
          <span class="session-time">${timeStr}</span>
          ${!selectMode ? `<span class="split-btn" data-sid="${s.id}" title="Split view">⧉</span>` : ""}
          ${!selectMode ? `<span class="del-btn" data-sid="${s.id}">&times;</span>` : ""}`;
        div.onclick = (e) => {
          if ((e.target as HTMLElement).classList.contains("del-btn")) return;
          if ((e.target as HTMLElement).classList.contains("session-checkbox")) return;
          if ((e.target as HTMLElement).classList.contains("split-btn")) return;
          // Cross-project click: switch the active workspace first so
          // subsequent /sessions/{sid}/... calls hit the right project.
          if (s.workspace && s.workspace !== activeWs) {
            openProjectInSidebar(s.workspace, { thenSwitchTo: s.id });
          } else {
            switchSession(s.id);
          }
        };
        const splitBtn = div.querySelector(".split-btn");
        if (splitBtn) {
          (splitBtn as HTMLElement).onclick = (e) => {
            e.stopPropagation();
            const sid_ = (e.currentTarget as HTMLElement).dataset.sid!;
            if (store.get().splitSessions.includes(sid_)) {
              removeFromSplit(sid_);
            } else {
              addToSplit(sid_);
            }
          };
        }
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
            try {
              await api.updateSession(s.id, { name: newName, workspace: s.workspace });
            } catch { /* ignore */ }
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
            if (confirm(i18n.t("confirm.deleteSession"))) deleteSession(s.id, s.workspace);
          };
        }
        sessionsContainer.appendChild(div);
      }
    }

    // Restore checked states after rebuild
    if (selectMode && checkedSids.size > 0) {
      sessionsContainer.querySelectorAll<HTMLInputElement>(".session-checkbox").forEach(cb => {
        if (checkedSids.has(cb.dataset.sid!)) cb.checked = true;
      });
    }

    list.appendChild(projectDiv);
  }

  // "Show all" toggle — only meaningful for the active project.
  if (activeGroup) {
    if (activeGroup.trimmed) {
      const btn = document.createElement("button");
      btn.className = "show-all-btn";
      btn.textContent = i18n.t("sidebar.showAll", { n: activeGroup.totalCount, name: activeGroup.label });
      btn.onclick = () => { list.dataset.showAll = "true"; renderSessions(); };
      list.appendChild(btn);
    } else if (showAll && !filter) {
      const btn = document.createElement("button");
      btn.className = "show-all-btn";
      btn.textContent = i18n.t("sidebar.showRecent");
      btn.onclick = () => { list.dataset.showAll = "false"; renderSessions(); };
      list.appendChild(btn);
    }
  }
}

async function createSession() {
  closeAllFullpageOverlays();
  // Pin the new session to a concrete model up front. Without this the
  // session is created with model_name=None, and when the first message is
  // sent the backend falls back to the global config.model.name — but the
  // composer dropdown shows the DOM-default first option (or config.model),
  // so the two can disagree: the dropdown shows MiniMax-M3 while the
  // backend actually runs the config default (e.g. a leftover kimi model),
  // producing "shows A, calls B" errors. Resolve to the configured default,
  // or the first available model when no default is set.
  const { config } = store.get();
  const models = (config as any).modelDetails || [];
  const modelName = (config as any).model || (models[0] && models[0].name);
  // Resolve provider_name so the backend pins the right provider when the
  // same model name exists under multiple providers (e.g. MiniMax-M3 under
  // both "MiniMax" and "minimax-anthropic"). Prefer the config-level
  // providerName set by refreshConfig from get_config; fall back to the
  // matching modelDetails entry's "provider" field.
  let providerName = (config as any).providerName || "";
  if (!providerName && modelName) {
    const detail = models.find((m: any) => m.name === modelName);
    if (detail && detail.provider) providerName = detail.provider;
  }
  const id = await api.createSession(modelName, providerName || undefined);
  // New sessions always belong to the active workspace, so tag them here
  // so they show up in the right project group without waiting for the
  // next /sessions refresh.
  const activeWs = store.get().config.workspace || "";
  const sessions = [...store.get().sessions];
  sessions.unshift({ id, turnCount: 0, status: "idle", preview: i18n.t("session.empty"), workspace: activeWs, model_name: modelName } as any);
  store.set({ sessions });
  renderSessions();
  await switchSession(id);
}

// ---- Per-session prompt draft ----
// The textarea + attached images are bound to the active session so
// typing in A and switching to B doesn't leak A's text into B. We
// stash the current prompt into promptDrafts[oldSid] on switch and
// hydrate from promptDrafts[newSid] when the new session takes over.
// Background sessions don't get a textarea, so this only matters for
// the active sid; for any other sid the draft is just dormant state
// that gets re-saved verbatim the next time the user visits it.

async function switchSession(sid: string, opts: { skipGitRefresh?: boolean } = {}) {
  closeAllFullpageOverlays();
  // Per-session state (running / pending) lives in maps keyed by
  // sid, so switching sessions doesn't lose background work and
  // can't leak the previous session's flags into the new one. Only
  // activeSid + questionPending (which is question-card specific)
  // get reset.
  const oldSid = store.get().activeSid;
  if (oldSid && oldSid !== sid) {
    // Persist whatever model the leaving session had selected in its composer.
    // Don't swallow errors here: if the PATCH fails (e.g. session belongs
    // to a different workspace and the server returns 404), surface it so
    // the user knows the model they picked won't apply to the next turn.
    const sel = composerModelSelect(oldSid);
    if (sel && sel.value) {
      try {
        // sel.value is the composite "provider|model" (grouped dropdown) or a
        // bare model name (flat). Parse it back so we persist a clean
        // model_name + provider_name — writing the raw composite as model_name
        // corrupts the session (switch-back then matches no model and the
        // dropdown falls through to the first option).
        const raw = sel.value;
        const sep = raw.indexOf("|");
        const payload: Record<string, string> = sep > 0
          ? { provider_name: raw.slice(0, sep), model_name: raw.slice(sep + 1) }
          : { model_name: raw };
        await api.updateSession(oldSid, payload);
      } catch (err: any) {
        console.error("updateSession(model_name) failed on session switch:", err);
      }
    }
    // Stash the current prompt (text + attached images) under the
    // session we're leaving so switching back restores it verbatim.
    if (oldSid) {
      const ta = composerTextarea(oldSid);
      const text = ta ? ta.value : draftText(oldSid);
      const images = draftImages(oldSid);
      if (text || images.length > 0 || store.get().promptDrafts[oldSid]) {
        store.set({ promptDrafts: { ...store.get().promptDrafts, [oldSid]: { text, images } } });
      }
    }
  }
  store.set({ activeSid: sid, questionPending: false });
  // If the newly active session was shown as a secondary split pane,
  // remove it from the split list — it's now in the main #messages area.
  const splitSessions = store.get().splitSessions.filter(s => s !== sid);
  if (splitSessions.length !== store.get().splitSessions.length) {
    store.set({ splitSessions });
  }
  // Cancel any in-flight image uploads that belong to the old session
  // so they don't complete after activeSid has already changed and push
  // their results into the wrong session's pendingImages.
  cancelInFlightUploads(sid);
  renderSessions();
  $("messages").innerHTML = "";
  resetStreamingState(sid);
  renderPendingBar();
  await loadHistory(sid);
  renderSplitPanes();
  // The Plan tab shared its body with the previous session because the
  // in-memory cache was a module-level singleton (see right-panel.ts
  // updatePlanTabContent — it now keys by sid). Re-render against the
  // newly-active session so the panel shows its plan, not the previous
  // one's. No-op if the Plan tab isn't open / doesn't exist.
  refreshActivePlanTab();
  // Mount/hydrate the full-screen composer for the newly active session
  // (no-op in split mode; pane composers are hydrated in renderSplitPanes).
  renderComposers();
  // Skip when the caller already refreshed the branch for the new
  // workspace (e.g. openProjectInSidebar switching both workspace
  // and session in one go).
  if (!opts.skipGitRefresh) {
    await refreshGitBranch();
  }
  try {
    const turns = await api.getTurns(sid);
    const activeTurn = turns.find(t => t.status === "running");
    if (activeTurn) {
      setActiveRunning(true);
      // We missed the live turn_start event (e.g. the turn was started by
      // the IM bridge before the user switched to this session). Show a
      // typing indicator so the user knows a turn is in progress.
      appendTyping();
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
              (args.options as unknown[]) || [],
              !!args.multi_select,
              tc.id,
              $("messages"),
              sid,
            );
          }
        }
      }
      // The turn is still running — show the typing indicator so the
      // user sees the session as active, not idle.
      appendTyping();
      scrollBottom();
    } else {
      // No active turn — clear stale running state from missed turn_end
      const { runningSessions, sessions: curSessions } = store.get();
      if (runningSessions[sid]) {
        const next = { ...runningSessions };
        delete next[sid];
        const s = curSessions.find(x => x.id === sid);
        if (s) s.status = "done";
        store.set({ runningSessions: next, sessions: [...curSessions] });
        setActiveRunning(false);
        renderSessions();
      }
    }
  } catch (e) {
    console.error("Failed to fetch running turn events:", e);
  }

  // Hydrate the prompt textarea + attached images for the session
  // we're switching TO. Done last so all render/turn-detection above
  // has already settled, and the input bar reflects the new sid
  // when the user starts typing.
  if (sid) hydrateComposer(sid);
  updateSendStopButton();
  refreshPlan();
  if ($("rightPanel").classList.contains("show")) refreshActiveReviewTabs();

  // Show/hide compact toast based on session state
  const { compactingSessions } = store.get();
  if (compactingSessions[sid]) {
    setCompactToastState("loading", i18n.t("toast.compacting"), sid);
  } else {
    hideCompactToast();
  }
}

async function deleteSession(sid: string, workspace?: string) {
  try {
    await api.deleteSession(sid, workspace ? { workspace } : undefined);
  } catch (e: any) {
    alert(i18n.t("alert.deleteSessionFailed", { err: e?.message || "unknown" }));
    return;
  }
  const sessions = store.get().sessions.filter(s => s.id !== sid);
  const splitSessions = store.get().splitSessions.filter(s => s !== sid);
  store.set({ sessions, splitSessions });
  if (store.get().activeSid === sid) {
    store.set({ activeSid: null });
    $("messages").innerHTML = "";
    showEmptyState(true);
    updateContextProgress(0, 0);
  }
  // Drop the deleted session's draft + queued images so we don't keep
  // dead keys around (and release their blob URLs). Other sessions are
  // untouched.
  disposePendingImageThumbs([...draftImages(sid), ...queuedImages(sid)]);
  const { promptDrafts, pendingMessages } = store.get();
  const nextDrafts = { ...promptDrafts };
  delete nextDrafts[sid];
  const nextPending = { ...pendingMessages };
  delete nextPending[sid];
  store.set({ promptDrafts: nextDrafts, pendingMessages: nextPending });
  renderSessions();
  renderSplitPanes();
}

async function loadHistory(sid: string) {
  showEmptyState(true);
  $("messages").innerHTML = "";
  clearStreamCtx(sid);
  updateContextProgress(0, 0);

  const ok = await loadHistoryInto(sid, $("messages"));
  if (ok) scrollBottom();
}

async function loadHistoryInto(sid: string, target: HTMLElement): Promise<boolean> {
  // Single fetch with the full history. The filtered endpoint only
  // returns the latest summary + post-summary messages (via _llm_context
  // on the server), but the UI needs ALL summaries to be visible —
  // otherwise a 2nd compaction hides every previous summary from the
  // chat. We build the UI view client-side from fullMsgs below so
  // every summary is preserved.
  let fullData: any;
  try {
    fullData = await api.getMessages(sid, { includeDropped: true });
  } catch {
    target.innerHTML = "";
    return false;
  }
  const fullMsgs = fullData.messages || [];
  target.innerHTML = "";

  // Cache the session's last token usage on the session object so
  // hydrateComposer can restore the context ring when the composer mounts.
  // (loadHistoryInto can run before the composer is mounted — e.g. on
  // switchSession — so updating the ring here directly would target a
  // not-yet-existing element and the ring would stay empty.)
  if (fullData.last_usage) {
    const { sessions } = store.get();
    const si = sessions.findIndex(x => x.id === sid);
    if (si !== -1) {
      const next = [...sessions];
      (next[si] as any).lastUsage = fullData.last_usage;
      store.set({ sessions: next });
    }
  }

  // For the active session's main container, also sync chrome state
  // (empty-state, context ring, model dropdown) that doesn't exist per-pane.
  if (target === $("messages")) {
    showEmptyState(fullMsgs.length === 0);
    if (fullMsgs.length === 0 && document.querySelector(".ziva-center")?.classList.contains("multi")) {
      setPaneEmptyPlaceholder(target);
    }
    if (fullData.last_usage?.prompt_tokens !== undefined) {
      const contextWindow = store.get().config.contextWindow || 200000;
      const pct = Math.min(fullData.last_usage.prompt_tokens / contextWindow, 1);
      updateContextProgress(pct, fullData.last_usage.prompt_tokens, sid);
    }
    // Sync the active composer's model dropdown to the loaded session.
    const modelSel = composerModelSelect(sid);
    if (fullData.model_name) {
      // Heal a legacy "provider|model" composite that an older client
      // persisted as model_name, then mirror the clean values into the store.
      let modelName: string = fullData.model_name;
      let providerName: string | null = fullData.provider_name ?? null;
      const pipe = modelName.indexOf("|");
      if (pipe > 0) {
        providerName = providerName || modelName.slice(0, pipe);
        modelName = modelName.slice(pipe + 1);
      }
      // Mirror the server-side model_name (+ provider_name, thinking_mode)
      // into the store, not just the <select>'s .value. hydrateComposer
      // re-renders the dropdown from `session.model_name || config.model`; if
      // the store stays stale while the select's value is set, the next
      // render falls through to config.model or the DOM-default first option
      // — so the dropdown shows one model while the backend runs the
      // session's actual binding.
      const { sessions } = store.get();
      const si = sessions.findIndex(x => x.id === sid);
      if (si !== -1) {
        const cur = sessions[si] as any;
        if (cur.model_name !== modelName
            || (providerName != null && cur.provider_name !== providerName)
            || (fullData.thinking_mode != null && cur.thinking_mode !== fullData.thinking_mode)) {
          const next = [...sessions];
          (next[si] as any).model_name = modelName;
          if (providerName != null) (next[si] as any).provider_name = providerName;
          if (fullData.thinking_mode != null) (next[si] as any).thinking_mode = fullData.thinking_mode;
          store.set({ sessions: next });
        }
      }
      // Option values are the composite "provider|model"; set the matching
      // one so the dropdown lands on the right row for same-named models
      // across providers.
      const want = providerName ? `${providerName}|${modelName}` : modelName;
      if (modelSel && Array.from(modelSel.options).some((o) => o.value === want)) {
        if (modelSel.value !== want) {
          modelSel.value = want;
        }
      }
    }
  }

  const summaryIndices: number[] = [];
  for (let i = 0; i < fullMsgs.length; i++) {
    if ((fullMsgs[i] as any)._compaction_summary) summaryIndices.push(i);
  }
  // Render the COMPLETE history. The server's filtered endpoint hides
  // everything before the last summary, but the UI must show every
  // previous summary, every model's thinking/content/tool calls, and
  // every user message in between — not just the last summary + tail.
  // Summary messages render as regular assistant turns (their `content`
  // is the summary text), so `renderMessages(fullMsgs)` preserves
  // everything the user asked for.
  // Interleave compaction boundaries with the messages in a single pass.
  //
  // Each summary at index `s` folds the range [prev, s) — the messages
  // that were compacted INTO that summary. Walking every summary in order
  // and folding [prev, s) means earlier summaries end up folded inside
  // the box that fed the next summary (compaction is chained: summary N+1
  // was produced from summary N + the messages after it). Only the LAST
  // summary stays expanded, as the head of the remaining tail, because it
  // is the live context the model is working from.
  //
  // On-disk example after two compacts:
  //   [u1,a1,...,a3, S1, u4,a4,...,a7, S2, u8,...]
  // renders as:
  //   ▸ 之前 6 条消息已压缩为摘要   (covers [u1..a3]  → S1)
  //   ▸ 之前 9 条消息已压缩为摘要   (covers [S1,u4..a7] → S2)
  //     S2, u8, ...                  (expanded tail)
  //
  // Each boundary lazily renders its slice via renderMessages on expand,
  // so thinking + content + tool cards inside a fold look identical to
  // the live chat (just visually scaled down by the wrapper's CSS).
  let prev = 0;
  for (const si of summaryIndices) {
    // Each summary at index `s` folds the range [prev, s) — everything that
    // was compacted INTO that summary, including the previous summary itself
    // (it is the head of the context that produced the next summary).
    const foldStart = prev;
    const count = si - foldStart;
    if (count > 0) appendCompactBoundary(sid, count, foldStart, si, target);
    prev = si;
  }
  // Expanded tail: everything after the last folded summary (or the whole
  // history when there were no summaries).
  if (prev < fullMsgs.length) {
    renderMessages(target, fullMsgs.slice(prev), prev);
  }
  return true;
}

// Render a list of messages into a target container using the same DOM
// construction as the live chat (user / assistant / tool cards). Used
// both by `loadHistory` (target = #messages) and by the compact-history
// expand affordance (target = .compact-dropped inside the collapse bar),
// so the folded messages look identical to the live chat — just visually
// scaled down via the wrapper's CSS.
//
// `withRewind` controls whether rewind (回撤) buttons are attached. The
// main chat view passes true (default) so users can rewind to any live
// message. The compact-fold expander passes false: messages inside a
// fold are historical context only — rewinding into them would either
// truncate to a meaningless offset (idx is local to the slice) or
// resurrect a stale state that the summary has already superseded.
function renderMessages(target: HTMLElement, msgs: any[], offset: number = 0, withRewind: boolean = true): void {
  let pendingToolCalls: { id: string; name: string; arguments: Record<string, unknown> }[] = [];
  const toolCardByCallId = new Map<string, HTMLElement>();

  // Pre-scan hidden messages to build a filename→dataURL map so that
  // assistant markdown like ![alt](attachment://selfportrait.jpg) can be
  // resolved to the actual image. The hidden messages carry the real image
  // data (base64 data URL) and the original file path in their text part:
  //   "[Image from /tmp/vvg_star/starry_night.jpg | call_id=...]"
  const attachmentMap = new Map<string, string>();
  for (const m of msgs) {
    if (m._hidden && Array.isArray(m.content)) {
      let filePath = "";
      let dataUrl = "";
      for (const part of m.content) {
        if (part.type === "text" && typeof part.text === "string") {
          const match = part.text.match(/\[Image from\s+(\S+)/);
          if (match) filePath = match[1];
        }
        if (part.type === "image_url" && part.image_url?.url) {
          dataUrl = part.image_url.url;
        }
      }
      if (filePath && dataUrl) {
        const filename = filePath.split("/").pop() || "";
        if (filename) attachmentMap.set(filename, dataUrl);
      }
    }
  }
  // Make the map available to the markdown renderer so it can resolve
  // attachment:// URLs embedded in assistant messages (IM-bridge images).
  setAttachmentDataMap(attachmentMap.size > 0 ? attachmentMap : null);

  for (let mi = 0; mi < msgs.length; mi++) {
    const m = msgs[mi];
    const isSub = (m as any)._subagent === true;

    // Compaction summary has role="user" (for API alternation) but should
    // render as an assistant-style context block, not a user chat bubble.
    const isCompactionSummary = (m as any)._compaction_summary === true;

    if (m.role === "user" && !isCompactionSummary) {
      if ((m as any)._hidden) {
        // Attach hidden image messages to the matching tool card via tool_call_id.
        // This is robust to parallel tool calls where proximity would mis-associate.
        const toolCallId = (m as any).tool_call_id as string | undefined;
        const imgUrl = extractImageUrlFromHidden(m.content);
        if (imgUrl && toolCallId) {
          const card = toolCardByCallId.get(toolCallId);
          if (card) injectImageIntoToolCard(card, imgUrl);
        }
        continue;
      }
      appendUserMsg(m.content, target, withRewind ? offset + mi : undefined);
    } else if (m.role === "assistant" || isCompactionSummary) {
      if (isSub) {
        continue;
      }
      const toolCalls = (m as any).tool_calls as { id: string; name: string; arguments: Record<string, unknown> }[] | undefined;
      // Persisted by runtime.py into the assistant message's
      // `reasoning_content` field; surfaced here so reloading history
      // shows the same thinking card the user saw during streaming.
      const reasoning = ((m as any).reasoning_content as string | undefined) || "";
      if (toolCalls && toolCalls.length > 0) {
        pendingToolCalls = toolCalls;
        const thinking = mergeThinking(reasoning || undefined, m.content);
        if (thinking) {
          const thinkDiv = document.createElement("div");
          thinkDiv.className = "thinking-card-inline";
          thinkDiv.innerHTML = `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
          target.appendChild(thinkDiv);
        }
        // Intermediate assistant turns (the ones that issue tool_calls)
        // ALSO carry a short prose lead-in — "让我先看一下当前文件的实际状态。"
        // — that the live streaming path renders below the thinking card.
        // Without this, reloading history would silently drop that text,
        // so the chat reads as "Thinking → tool card" with no transition.
        // extractThinking already stripped 脑中...脑尾 for the streaming path;
        // apply the same here so reloads match what the user saw live.
        const { main: inlineMain } = extractThinking(typeof m.content === "string" ? m.content : "");
        if (inlineMain && inlineMain.trim()) {
          const proseDiv = document.createElement("div");
          proseDiv.className = "msg assistant assistant-inline-prose";
          proseDiv.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div><div class="md">${renderMarkdown(inlineMain)}</div></div>`;
          target.appendChild(proseDiv);
          highlightCode(proseDiv);
        } else if (!thinking) {
          // The model issued tool calls with no prose lead-in and no
          // thinking. Without this placeholder the chat reads "You →
          // tool card", which looks like the user called the tool.
          const placeholderDiv = document.createElement("div");
          placeholderDiv.className = "msg assistant assistant-tool-only";
          placeholderDiv.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div></div>`;
          target.appendChild(placeholderDiv);
        }
      } else {
        appendAssistantMsg(m.content, target, reasoning);
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

      let subagentTools: Record<string, number> | undefined;
      if (!isPruned && toolName === "spawn_agent" && typeof output === "object" && output !== null) {
        subagentTools = (output as any).tools_summary;
      }

      // ask_user is rendered as an answered question card, not a tool card.
      // The tool result content is currently persisted as the human-readable
      // text "User answered: <answer>" (see plugins/tools/ask_user/impl.py),
      // so `m.content` won't be parseable JSON on disk. We try the JSON
      // path first (in case future tool versions persist structured data),
      // then fall back to extracting the answer from the prefixed text.
      if (toolName === "ask_user") {
        let answer = "";
        if (typeof output === "object" && output !== null) {
          answer = String((output as any).answer || "");
        }
        if (!answer && typeof m.content === "string") {
          try {
            const parsed = JSON.parse(m.content);
            if (parsed && typeof parsed === "object") {
              answer = String((parsed as any).answer || "");
            }
          } catch {
            // not JSON — fall through to text-prefix extraction
            const match = m.content.match(/^User answered:\s*(.*)$/s);
            if (match) answer = match[1] ?? "";
          }
        }
        const q = String(args.question || "");
        const opts = (args.options as unknown[]) || [];
        const ms = !!args.multi_select;
        if (q) {
          const card = document.createElement("div");
          card.className = "question-card question-card-answered";
          card.innerHTML = `<div class="question-text">${esc(q)}</div><div class="question-reply">${i18n.t("question.you", { answer: esc(answer) })}</div>`;
          target.appendChild(card);
        }
        continue;
      }

      const card = appendToolCard(toolName, args, "success", output, subagentTools, isPruned, target, withRewind ? offset + mi : undefined);
      if (toolCallId) toolCardByCallId.set(toolCallId, card);
    }
  }
  // Reset the attachment map after the render pass so a later
  // renderMarkdown call outside this pass (e.g. an agent-card detail
  // preview) can't pick up this session's filename→dataURL map and
  // resolve an unrelated `attachment://` ref to the wrong image.
  setAttachmentDataMap(null);
}

// Render a collapse bar for a compaction layer. `start` and `end` define
// the range of folded messages [start, end) in the full history. On expand,
// fetches fullMsgs and renders that range inline.
function appendCompactBoundary(
  sid: string,
  droppedCount: number,
  start: number,
  end: number,
  target: HTMLElement = (getLiveStreamTarget() || $("messages")),
): void {
  const wrapper = document.createElement("div");
  wrapper.className = "compact-boundary";

  const bar = document.createElement("details");
  bar.className = "compact-collapse";
  const sum = document.createElement("summary");
  sum.textContent = i18n.t("msg.compactBoundary", { n: droppedCount });
  bar.appendChild(sum);

  const dropZone = document.createElement("div");
  dropZone.className = "compact-dropped";
  dropZone.textContent = i18n.t("msg.expandOriginal");
  bar.appendChild(dropZone);

  bar.addEventListener("toggle", async () => {
    if (!bar.open || dropZone.dataset.loaded === "1") return;
    try {
      const data = await api.getMessages(sid, { includeDropped: true });
      const fullMsgs = data.messages || [];
      dropZone.innerHTML = "";
      const originals = fullMsgs.slice(start, end);
      // Folded messages are historical context only — no rewind buttons
      // (rewinding into a compacted slice would resurrect stale state
      // the summary has already superseded, and the local idx would
      // collide with the main chat's data-idx scheme).
      renderMessages(dropZone, originals, 0, false);
      dropZone.dataset.loaded = "1";
    } catch (e) {
      dropZone.textContent = i18n.t("toast.loadFailed", { err: (e as Error).message });
    }
  });

  wrapper.appendChild(bar);
  target.appendChild(wrapper);
}

// Turn an `image_url.url` value into something the browser can load.
//
// We store attachment paths in the persisted message history (cheaper
// than base64), but the browser can't directly render an absolute
// filesystem path — so when the chat renderer encounters one, it
// proxies it through the server's /attachments endpoint. Optimistic
// renders use blob: URLs (live File objects in the renderer process),
// data: URLs come back from providers / older history entries, and
// http(s) URLs pass through unchanged.
function attachmentUrl(url: string): string {
  if (!url) return url;
  if (url.startsWith("data:") || url.startsWith("blob:") || url.startsWith("http://") || url.startsWith("https://") || url.startsWith("/attachments")) {
    return url;
  }
  if (url.startsWith("/")) {
    return `/attachments?path=${encodeURIComponent(url)}`;
  }
  return url;
}

// ---- Chat Rendering ----
// `target` defaults to `#messages` for the live streaming path. The
// compact-history expand affordance passes a different container so the
// folded messages reuse the same DOM (and styling) as the live chat,
// just visually scaled down via a wrapper class.
function appendUserMsg(text: string | unknown[], target: HTMLElement = (getLiveStreamTarget() || $("messages")), globalIndex?: number): HTMLElement {
  showEmptyState(false);
  // In split mode `setPaneEmptyPlaceholder` injects a `.pane-empty-state`
  // placeholder inside the per-pane messages container. `showEmptyState`
  // only toggles a CSS class on `.ziva-center`, which doesn't reach the
  // per-pane placeholder, so without this the user message lands BELOW
  // the still-visible placeholder (the placeholder has `flex: 1` and
  // hogs the vertical space). Tear the element out explicitly.
  if (target) clearPaneEmptyPlaceholder(target);
  const div = document.createElement("div");
  div.className = "msg user";
  // data-idx = the message's global index in the on-disk history (fullMsgs),
  // so the "rewind to here" button can tell the server exactly which message
  // to truncate at. Only set when rendering from history (renderMessages
  // passes offset+mi); the optimistic/streaming append omits it.
  if (globalIndex !== undefined) div.dataset.idx = String(globalIndex);
  let body = "";
  if (Array.isArray(text)) {
    for (const part of text) {
      if (typeof part === "object" && part !== null) {
        const p = part as Record<string, any>;
        if (p.type === "text" && p.text) body += renderMarkdown(p.text);
        else if (p.type === "image_url" && p.image_url?.url) {
          const src = attachmentUrl(p.image_url.url);
          body += `<div class="user-image"><img src="${esc(src)}" alt="attached image" loading="lazy" /></div>`;
        }
      }
    }
  } else {
    body = renderMarkdown(text);
  }
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> You</div><div class="md">${body}</div></div>`;
  if (globalIndex !== undefined) {
    const btn = document.createElement("button");
    btn.className = "rewind-btn";
    btn.title = i18n.t("msg.rewindUserEdit");
    btn.textContent = "↩";
    div.querySelector(".role-label")?.appendChild(btn);
  }
  target.appendChild(div);
  invalidateLiveStreamEl();
  highlightCode(div);
  // Return the element so the caller can remove it on failure
  // (the optimistic render is reverted when the server rejects the
  // create_turn — the queue bar is then the single source of truth
  // for "still pending", instead of chat+queue showing the same
  // message in two places).
  return div;
}

// After a turn finishes, the messages the streaming path appended (optimistic
// user msg + assistant/tool cards) have NO data-idx — so their rewind buttons
// are missing. Rather than reload (which clears #messages and flashes), fetch
// the message list once and stamp data-idx + the ↩ button onto the existing
// DOM elements in place, matching by document order.
async function patchRewindButtons(sid: string): Promise<void> {
  if (sid !== store.get().activeSid) return;
  let msgs: any[];
  try {
    msgs = (await api.getMessages(sid, { includeDropped: true })).messages || [];
  } catch { return; }
  const container = document.getElementById("messages");
  if (!container) return;

  // Only top-level message elements matter: messages rendered inside a
  // compact-fold dropZone are historical context and must never receive
  // rewind buttons. The `:scope >` selector excludes `.compact-dropped`
  // descendants while still matching live tail messages.
  const userEls = Array.from(container.querySelectorAll<HTMLElement>(":scope > .msg.user"));
  const toolEls = Array.from(container.querySelectorAll<HTMLElement>(":scope > .tool-card"));

  // The visible tail of the chat starts at the last compaction summary.
  // Anything before it is folded away, so we should not try to map those
  // historical messages onto the live DOM elements.
  let startIdx = 0;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if ((msgs[i] as any)._compaction_summary) {
      startIdx = i;
      break;
    }
  }

  // Walk the visible tail and the DOM in parallel: each visible user/tool
  // message maps to the next top-level element in document order.
  // ask_user is rendered as a .question-card, not a .tool-card, so it must
  // not consume a tool-card slot; otherwise every subsequent tool card gets
  // an idx shifted forward and rewinds to the wrong message.
  let ui = 0, ti = 0;
  for (let i = startIdx; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role === "user" && !m._hidden && !(m as any)._compaction_summary) {
      const el = userEls[ui++];
      if (el && el.dataset.idx == null) {
        el.dataset.idx = String(i);
        if (!el.querySelector(".rewind-btn")) {
          const btn = document.createElement("button");
          btn.className = "rewind-btn";
          btn.title = i18n.t("msg.rewindUserContent");
          btn.textContent = "↩";
          el.querySelector(".role-label")?.appendChild(btn);
        }
      }
    } else if (m.role === "tool" && !m._subagent && m.name !== "ask_user") {
      const el = toolEls[ti++];
      if (el && el.dataset.idx == null) {
        el.dataset.idx = String(i);
        if (!el.querySelector(".rewind-btn-tool")) {
          const btn = document.createElement("button");
          btn.className = "rewind-btn-tool";
          btn.title = i18n.t("msg.rewindTool");
          btn.textContent = "↩";
          const header = el.querySelector(".tool-card-header");
          const copyBtn = el.querySelector(".copy-tool-btn");
          if (header && copyBtn) header.insertBefore(btn, copyBtn);
        }
      }
    }
  }
}

// Rewind (Claude Code-style): delete the user message at idx and everything
// after it, then put that user's original text back into the composer for
// editing/resend. Stops there — does NOT auto-run the model.
async function rewindUserMsg(sid: string, idx: number, kind: "user" | "tool"): Promise<void> {
  const tip = kind === "tool"
    ? i18n.t("confirm.rewindTool")
    : i18n.t("confirm.rewindUser");
  if (!confirm(tip)) return;
  try {
    const res = await api.rewindSession(sid, idx);
    await loadHistory(sid);
    // Tool rewind just reloads history and stops — no composer restore.
    if (res.kind !== "user") return;
    const text = res.removed_user_content || "";
    setDraftText(sid, text);
    // Restore image attachments too: the attachment files are still on disk
    // (rewind only truncates messages, not the attachments dir), so the
    // stored path is still valid.
    const imgs = (res.removed_user_images || []).map((p: string) => ({
      path: p, mime: "image/*", size: 0,
      name: (p.split("/").pop() || "image"),
      thumbUrl: attachmentUrl(p),
    }));
    if (imgs.length) {
      setDraftImages(sid, imgs);
      renderComposerPreviews(sid);
    }
    const ta = composerTextarea(sid);
    if (ta) {
      ta.value = text;
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
      ta.focus();
    }
  } catch (e: any) {
    appendError(i18n.t("toast.rewindFailed", { err: e?.message || e }));
  }
}

// A web tab sent a selection (text + URL + screenshot). Drop it into the
// active composer as a quoted draft + image attachment, so the user can
// edit/send it like any other composed message.
async function insertBrowserSelection({ text, url, screenshotDataUrl }: { text: string; url: string; screenshotDataUrl: string }): Promise<void> {
  const sid = store.get().activeSid;
  if (!sid) {
    appendError(i18n.t("toast.openSessionFirst"));
    return;
  }
  const draft = `> ${text}\n\n${i18n.t("common.source")}${url}`;
  setDraftText(sid, draft);
  const ta = composerTextarea(sid);
  if (ta) {
    ta.value = draft;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
    ta.focus();
  }
  if (screenshotDataUrl) {
    try {
      const blob = await (await fetch(screenshotDataUrl)).blob();
      const file = new File([blob], "selection.png", { type: "image/png" });
      await addImageFile(file, sid);
    } catch (e: any) {
      appendError(i18n.t("toast.screenshotFailed", { err: e?.message || e }));
    }
  }
}

// Combine the provider's `reasoning_content` field (sent as
// `reasoning_delta` events during streaming, persisted on the assistant
// message as `reasoning_content`) with any <think>...</think> tags the
// provider embedded in the main content. Returns a single string to
// display inside the thinking card, or "" if neither source produced
// anything.
function mergeThinking(reasoning: string | undefined, content: string): string {
  const { thinking: inlineThink } = extractThinking(content);
  if (reasoning && inlineThink) return reasoning + "\n\n---\n\n" + inlineThink;
  if (reasoning) return reasoning.trim();
  if (inlineThink) return inlineThink;
  return "";
}

function appendAssistantMsg(text: string, target: HTMLElement = (getLiveStreamTarget() || $("messages")), reasoning: string = "") {
  text = contentToText(text);
  reasoning = contentToText(reasoning);
  const div = document.createElement("div");
  div.className = "msg assistant";
  const thinking = mergeThinking(reasoning || undefined, text);
  const { main } = extractThinking(text);
  let content = "";
  if (thinking) {
    content += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
  }
  content += `<div class="md">${renderMarkdown(main)}</div>`;
  div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div>${content}</div>`;
  target.appendChild(div);
  addCopyButtons(div);
  highlightCode(div);
  invalidateLiveStreamEl();
}

/**
 * Wipe everything mid-flight for a session's current turn: the streaming
 * assistant block, any in-flight tool cards, and the typing indicator —
 * all scoped to `sid` (defaults to the active session). Used on session
 * switch (abandon current turn) and on `stream_reset` (server retries the
 * same input; the partial text the deltas painted must be forgotten).
 */
function resetStreamingState(sid?: string) {
  const targetSid = sid || store.get().activeSid || "";
  if (targetSid) {
    clearStreamCtx(targetSid);
    const t = sessionMessagesEl(targetSid);
    if (t) removeTyping(t);
  }
}

function getOrCreateAssistantEl(sid: string = getLiveStreamSid() || "", target: HTMLElement = getLiveStreamTarget() || $("messages")): HTMLElement {
  const ctx = streamCtx(sid);
  if (!ctx.assistantEl) {
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = `<div class="msg-inner"><div class="role-label"><span class="dot"></span> Assistant</div><div class="md"></div></div>`;
    target.appendChild(div);
    const md = div.querySelector(".md") as HTMLElement;
    (md as any)._main = "";
    // Buffer for streaming `reasoning_delta` events (Anthropic / OpenAI
    // `reasoning_effort` providers send thinking in a separate field,
    // not embedded as <think> tags in the main content). Rendered into
    // the thinking card alongside any inline <think> blocks.
    (md as any)._reasoning = "";
    ctx.assistantEl = md;
  }
  return ctx.assistantEl!;
}

// Render the assistant message body (thinking card + markdown) from the
// streaming buffers stashed on the .md element. Called from both the
// throttled timer in the delta / reasoning_delta handlers and from
// model_response (which cancels the timer to land the final state
// deterministically).
function renderAssistantContent(el: HTMLElement) {
  const mainStr = (el as any)._main || "";
  const reasoningStr = (el as any)._reasoning || "";
  const thinking = mergeThinking(reasoningStr || undefined, mainStr);
  const { main } = extractThinking(mainStr);
  let html = "";
  if (thinking) {
    html += `<details class="thinking-card"><summary>Thinking</summary><div class="thinking-card-content">${esc(thinking)}</div></details>`;
  }
  html += renderMarkdown(main);
  el.innerHTML = html;
}

function appendTyping(target: HTMLElement = (getLiveStreamTarget() || $("messages"))) {
  if (target.querySelector(".typing-indicator")) return;
  const el = document.createElement("div");
  el.className = "typing-indicator";
  el.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div> Thinking...`;
  target.appendChild(el);
}

function removeTyping(target: HTMLElement = (getLiveStreamTarget() || $("messages"))) {
  const el = target.querySelector(".typing-indicator");
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

function extractImageUrlFromHidden(content: any): string | null {
  if (!Array.isArray(content)) return null;
  for (const part of content) {
    if (
      typeof part === "object" &&
      part !== null &&
      (part as any).type === "image_url" &&
      (part as any).image_url?.url
    ) {
      return (part as any).image_url.url as string;
    }
  }
  return null;
}

function injectImageIntoToolCard(card: HTMLElement, imgUrl: string): void {
  const body = card.querySelector(".tool-card-body");
  if (body && !body.querySelector("img")) {
    const imgDiv = document.createElement("div");
    imgDiv.className = "section-content tool-output-image";
    imgDiv.innerHTML = `<img src="${esc(imgUrl)}" alt="tool output" loading="lazy" />`;
    body.appendChild(imgDiv);
  }
}

function updateToolCardStatus(card: HTMLElement, status: "running" | "success" | "error" | "cancelled"): void {
  const statusClass = status === "error" ? "error" : status === "running" ? "running" : status === "cancelled" ? "cancelled" : "success";
  const statusText = status === "error" ? "error" : status === "running" ? "running..." : status === "cancelled" ? "cancelled" : "done";
  const statusEl = card.querySelector(".tool-status") as HTMLElement | null;
  if (statusEl) {
    statusEl.innerHTML = `<span class="status-dot ${statusClass}"></span>${statusText}`;
  }
  card.classList.toggle("open", status === "running");
}

function appendToolCard(
  toolName: string,
  args: Record<string, unknown>,
  status: string,
  output?: unknown,
  subagentTools?: Record<string, number>,
  isPruned: boolean = false,
  target: HTMLElement = (getLiveStreamTarget() || $("messages")),
  globalIndex?: number,
): HTMLElement {
  const card = document.createElement("div");
  // data-idx for rewind (history render only); the streaming path omits it.
  if (globalIndex !== undefined) card.dataset.idx = String(globalIndex);
  // spawn_agent cards should default to expanded (open) for both running and success states
  const isOpen = status === "running" || toolName === "spawn_agent";
  card.className = "tool-card" + (isOpen ? " open" : "") + (isPruned ? " pruned" : "");
  const statusClass = isPruned ? "pruned" : (status === "error" ? "error" : status === "running" ? "running" : "success");
  const statusText = isPruned ? "pruned" : (status === "error" ? "error" : status === "running" ? "running..." : "done");
  const abbrevArg = getAbbreviatedArg(args);

  let body = "";
  if (Object.keys(args).length > 0) {
    body += `<div class="section-label">${i18n.t("tool.input")}</div>`;
    body += `<div class="section-content"><code>${esc(JSON.stringify(args, null, 2))}</code></div>`;
  }
  if (isPruned) {
    body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
    body += `<div class="section-content pruned-output">${i18n.t("tool.prunedNote")}</div>`;
  } else if (output !== undefined) {
    if (toolName === "spawn_agent") {
      // Show the agent's completion text, which carries the grouped tools
      // summary ("grep ×3, read_file ×5") on its first line. Read from _text
      // (live) or the string output (replay — persisted content), so the
      // tools summary survives a restart instead of vanishing with the
      // non-persisted event metadata.
      const outText = (typeof output === "object" && output !== null && (output as any)._text)
        ? contentToText((output as any)._text)
        : (typeof output === "string" ? output : "");
      if (outText) {
        body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
        body += `<div class="section-content"><pre>${esc(outText)}</pre></div>`;
      }
    } else if (typeof output === "object" && output !== null && ((output as any).type === "image" || (output as any).image_url)) {
      // Tool returned an image — render it inline. Only explicit image
      // signals count (`type === "image"` set by the runtime when a tool
      // returns images, or an `image_url` field). We must NOT treat a
      // metadata `url` as an image: web_fetch legitimately carries
      // `{ url: <page>, format: "markdown", _text: <content> }`, and
      // rendering the page URL as <img> produced a broken-image icon
      // instead of the fetched markdown (which lives in `_text`).
      const imgUrl = (output as any).image_url || "";
      const outText = (output as any)._text ? contentToText((output as any)._text) : "";
      if (outText || imgUrl) {
        body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
      }
      if (outText) {
        body += `<div class="section-content"><pre>${esc(outText)}</pre></div>`;
      }
      if (imgUrl) {
        body += `<div class="section-content tool-output-image"><img src="${esc(imgUrl)}" alt="tool output" loading="lazy" /></div>`;
      }
    } else if (typeof output === "string" && /^data:image\//.test(output)) {
      body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
      body += `<div class="section-content tool-output-image"><img src="${esc(output)}" alt="tool output" loading="lazy" /></div>`;
    } else {
      // Prefer plain text from _text field (what the model sees), not the
      // full metadata dict that SSE carries for structured data.
      let outStr: string;
      if (typeof output === "object" && output !== null && (output as any)._text) {
        outStr = contentToText((output as any)._text);
        body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
        body += `<div class="section-content"><pre>${esc(outStr)}</pre></div>`;
      } else {
        outStr = typeof output === "string" ? output : JSON.stringify(output, null, 2);
        body += `<div class="section-label">${i18n.t("tool.output")}</div>`;
        body += `<div class="section-content"><code>${esc(outStr)}</code></div>`;
      }
    }
  }
  if (subagentTools && Object.keys(subagentTools).length > 0) {
    const summary = Object.entries(subagentTools)
      .map(([n, c]) => `${esc(n)} ×${c}`)
      .join(", ");
    body += `<div class="section-label">${i18n.t("tool.usedTools")}</div>`;
    body += `<div class="section-content subagent-tools"><span class="subagent-tool-summary">${summary}</span></div>`;
  }
  // send_file: render the delivered file from the tool's `path` arg, by kind:
  //   image → inline <img>; browser-previewable (pdf/video/audio/html/text)
  //   → link opening in the internal browser (new tab); anything the browser
  //   can't render (archives / office / binaries) → filename only, no link.
  // Driven by args so it shows on both streaming and history reload.
  if (toolName === "send_file" && typeof args.path === "string" && args.path) {
    const p = String(args.path);
    const src = attachmentUrl(p);
    const ext = (p.split(".").pop() || "").toLowerCase();
    const fname = p.split("/").pop() || p;
    const isImg = /^(png|jpe?g|gif|webp|bmp|svg|ico|tiff?)$/.test(ext);
    const isPreviewable = /^(mp4|webm|mov|m4v|ogv|mkv|avi|mp3|wav|ogg|m4a|aac|flac|opus|pdf|html?|txt|markdown|log|csv|json|xml|py|js|tsx?|jsx|css|scss|sh|bash|rs|go|java|kt|c|cc|cpp|h|hpp|rb|php|swift|sql|md)$/.test(ext);
    body += `<div class="section-label">${i18n.t("tool.file")}</div>`;
    if (isImg) {
      body += `<div class="section-content tool-output-image"><img src="${esc(src)}" alt="${esc(p)}" loading="lazy" /></div>`;
    } else if (isPreviewable) {
      body += `<div class="section-content"><a class="send-file-link" href="${esc(src)}" target="_blank" rel="noopener" title="Open ${esc(fname)}"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> ${esc(fname)} <span class="send-file-open">↗ ${i18n.t("tool.open")}</span></a></div>`;
    } else {
      body += `<div class="section-content send-file-name"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.55;vertical-align:-2px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> ${esc(fname)}</div>`;
    }
  }

  card.innerHTML = `
    <div class="tool-card-header">
      <span class="tool-icon">⚙</span>
      <span class="tool-name">${esc(toolName)}</span>
      <span class="tool-args">${esc(abbrevArg)}</span>
      <span class="tool-status"><span class="status-dot ${statusClass}"></span>${statusText}</span>
      <button class="copy-tool-btn" title="${i18n.t("tool.copy")}">⧉</button>
    </div>
    <div class="tool-card-body">${body}</div>`;

  (card.querySelector(".tool-card-header") as HTMLElement).onclick = () => card.classList.toggle("open");
  (card.querySelector(".copy-tool-btn") as HTMLElement).onclick = async (e) => {
    e.stopPropagation();
    const payload = `${toolName} - ${JSON.stringify(args)}${output !== undefined ? "\nOutput: " + (typeof output === "string" ? output : JSON.stringify(output)) : ""}`;
    const ok = await copyText(payload);
    if (!ok) {
      // eslint-disable-next-line no-console
      console.warn("[ziva] copy failed for tool card", toolName);
    }
  };

  // Rewind button on tool cards (history render only): snaps to the end of
  // this tool-call group (parallel tool results included) and deletes
  // everything after. Stop and wait for the user.
  if (globalIndex !== undefined) {
    const btn = document.createElement("button");
    btn.className = "rewind-btn-tool";
    btn.title = i18n.t("msg.rewindTool");
    btn.textContent = "↩";
    // Place inside the header next to the copy button (flex layout) instead
    // of an overlay, so it doesn't cover the copy button.
    const copyBtn = card.querySelector(".copy-tool-btn");
    const header = card.querySelector(".tool-card-header");
    if (header && copyBtn) header.insertBefore(btn, copyBtn);
  }
  target.appendChild(card);
  invalidateLiveStreamEl();
  return card;
}

function appendApprovalCard(requestId: string, toolName: string, args: Record<string, unknown>, target: HTMLElement = $("messages")) {
  const card = document.createElement("div");
  card.className = "approval-card";
  const argsStr = JSON.stringify(args, null, 2);
  card.innerHTML = `
    <div class="approval-header">${i18n.t("approval.title")}</div>
    <div class="approval-detail">${i18n.t("approval.tool")} <strong>${esc(toolName)}</strong><br/><pre>${esc(argsStr.slice(0, 500))}${argsStr.length > 500 ? "..." : ""}</pre></div>
    <div class="approval-actions">
      <button class="approve-once">${i18n.t("approval.allowOnce")}</button>
      <button class="approve-always">${i18n.t("approval.allowAlways")}</button>
      <button class="deny">${i18n.t("approval.deny")}</button>
    </div>`;
  (card.querySelector(".approve-once") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "once");
    card.remove();
  };
  (card.querySelector(".approve-always") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "always");
    card.remove();
  };
  (card.querySelector(".deny") as HTMLElement).onclick = async () => {
    await api.replyPermission(requestId, "reject");
    card.remove();
  };
  target.appendChild(card);
  invalidateLiveStreamEl();
}

interface OptionDisplay {
  // Pre-escaped HTML for the button / label inner content. For dict
  // options with both label and description, this is a two-line layout:
  // the label as the primary line and the description as a smaller
  // muted subtitle below. String options render as a single line.
  display: string;
  // The value sent back to the model when the user picks this option.
  // For dict options this is the option's `label`; for string options
  // it's the string itself. Matches the convention used by Claude
  // Code's AskUserQuestion and Codex's request_user_input.
  submitValue: string;
}

function normalizeOption(o: unknown): OptionDisplay {
  if (typeof o === "string") {
    // Legacy fallback: the old schema declared options as strings, and
    // some models may still pass strings. Render as a single line and
    // submit the string itself.
    return { display: esc(o), submitValue: o };
  }
  if (o && typeof o === "object" && !Array.isArray(o)) {
    const obj = o as Record<string, unknown>;
    const trimStr = (v: unknown): string =>
      typeof v === "string" && v.trim() ? v.trim() : "";
    const label = trimStr(obj.label);
    const description = trimStr(obj.description);

    // Build display: label as primary, description as muted subtitle.
    // If description is missing or duplicates label, just show the label.
    let display: string;
    if (label) {
      display = esc(label);
      if (description && description !== label) {
        display += `<br><span class="opt-description">${esc(description)}</span>`;
      }
    } else if (description) {
      // Malformed: only description, no label — treat description as the
      // label so the user still sees something meaningful.
      display = esc(description);
    } else {
      // Garbage dict (no label / description). Surface the JSON so the
      // user can see what was sent instead of silently dropping it.
      display = esc(JSON.stringify(o));
    }

    // Submit value: label is the canonical "what the model gets back".
    // Falls back to description if label is missing, then to the JSON
    // dump as last resort.
    const submitValue = label || description || JSON.stringify(o);

    return { display, submitValue };
  }
  if (Array.isArray(o)) {
    const text = o.map(String).filter(Boolean).join(", ") || "(empty list)";
    return { display: esc(text), submitValue: text };
  }
  const s = o == null ? "" : String(o);
  return { display: esc(s), submitValue: s };
}

function appendQuestionCard(
  question: string,
  rawOptions: unknown[],
  multiSelect: boolean = false,
  callId?: string,
  target: HTMLElement = $("messages"),
  sid: string = store.get().activeSid || "",
) {
  const options = rawOptions.map(normalizeOption);
  if (target === $("messages")) showEmptyState(false);
  const card = document.createElement("div");
  card.className = "question-card";
  let html = `<div class="question-text">${esc(question)}</div>`;
  if (options.length > 0) {
    if (multiSelect) {
      html += `<div class="question-options">${options.map((o, i) =>
        `<label class="question-checkbox-label"><input type="checkbox" class="question-checkbox" data-opt="${i}" value="${esc(o.submitValue)}" /><span>${o.display}</span></label>`
      ).join("")}</div>`;
      html += `<div class="question-input-row question-other-row">
        <input type="text" class="question-input" placeholder="${i18n.t("question.customAnswer")}" />
        <button class="question-submit" aria-label="${i18n.t("question.submit")}">↑</button>
      </div>`;
    } else {
      html += `<div class="question-options">${options.map((o, i) =>
        `<button class="question-option-btn" data-opt="${i}" data-submit="${esc(o.submitValue)}">${o.display}</button>`
      ).join("")}</div>`;
      html += `<div class="question-input-row question-other-row">
        <input type="text" class="question-input" placeholder="${i18n.t("question.customAnswer")}" />
        <button class="question-submit" aria-label="${i18n.t("question.send")}">↑</button>
      </div>`;
    }
  } else {
    html += `<div class="question-input-row">
      <input type="text" class="question-input" placeholder="${i18n.t("question.typeAnswer")}" />
      <button class="question-submit" aria-label="${i18n.t("question.send")}">↑</button>
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
      <span>${i18n.t("question.chatAbout")}</span>
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
    replyDiv.textContent = i18n.t("question.you", { answer });
    card.appendChild(replyDiv);
  };

  const submit = (answer: string) => {
    if (submitted) return;
    const trimmed = answer.trim();
    if (!trimmed) return;
    const questionSid = sid || store.get().activeSid;
    if (!questionSid) return;
    // Resolve the pending ask_user future on the backend instead of
    // starting a brand-new turn — the original model round is still
    // waiting for our answer.
    api.replyQuestion(questionSid, trimmed, callId).catch((e) => {
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
      btn.onclick = () => submit(btn.dataset.submit ?? btn.textContent ?? "");
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
    (card.querySelector(".question-input") as HTMLElement | null)?.focus();
  }

  const chatAboutBtn = card.querySelector(".question-chat-about") as HTMLElement;
  chatAboutBtn.onclick = () => {
    if (submitted) return;
    const questionSid = sid || store.get().activeSid;
    if (!questionSid) return;
    api.replyQuestion(questionSid, i18n.t("question.abandonReply"), callId).catch((e) => {
      console.error("replyQuestion failed:", e);
    });
    lockCard(i18n.t("question.chatAbout"));
    // Focus the chat composer so the user can immediately type
    // their new direction.
    setTimeout(() => {
      const prompt = document.getElementById("prompt") as HTMLTextAreaElement | null;
      prompt?.focus();
    }, 50);
  };

  target.appendChild(card);
  // Mark the turn as still running: the model round is suspended
  // waiting on the user, not idle. questionPending lets the Stop
  // button know to resolve the question as "user abandoned" rather
  // than cancelling the entire turn.
  store.set({ questionPending: true });
  if (target === $("messages")) setActiveRunning(true);
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
  removalObserver.observe(target, { childList: true });
  updateSendStopButton();
  scrollBottom(target);
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
function appendError(msg: string, target: HTMLElement = (getLiveStreamTarget() || $("messages"))) {
  const div = document.createElement("div");
  div.className = "error-card";
  div.textContent = "Error: " + msg;
  target.appendChild(div);
  invalidateLiveStreamEl();
}

// Lightweight system notice card (used by /new, /stop echoes, and the
// trailing "Switched model to X" acknowledgement). Renders as a centered
// pill so it reads as a system event rather than a chat message — no
// role label, no avatar, no Thinking block, no markdown body. Ephemeral:
// not persisted to history.
function appendSystem(iconSvg: string, label: string, detail?: string, target: HTMLElement = (getLiveStreamTarget() || $("messages"))) {
  const div = document.createElement("div");
  div.className = "system-card";
  const inner = document.createElement("div");
  inner.className = "system-inner";
  if (iconSvg) inner.insertAdjacentHTML("afterbegin", `<span class="system-icon">${iconSvg}</span>`);
  const labelEl = document.createElement("span");
  labelEl.className = "system-label";
  labelEl.textContent = label;
  inner.appendChild(labelEl);
  if (detail) {
    const detailEl = document.createElement("span");
    detailEl.className = "system-detail";
    detailEl.textContent = detail;
    inner.appendChild(detailEl);
  }
  div.appendChild(inner);
  target.appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  invalidateLiveStreamEl();
}

// Structured model picker for the no-arg `/model` command. Shows the
// current model as a labeled header, each available model as a clickable
// row (clicking fires the same PATCH the slash command would), grouped by
// provider so same-named models across providers are distinguishable, plus
// a footer hint. The card itself isn't persisted to history.
function appendModelPicker(
  currentModel: string,
  currentProvider: string,
  models: { name: string; provider?: string }[],
  sid: string,
  target: HTMLElement = (getLiveStreamTarget() || $("messages")),
) {
  const card = document.createElement("div");
  card.className = "model-picker";

  // Head: title + current-model pill
  const head = document.createElement("div");
  head.className = "model-picker-head";
  const title = document.createElement("h4");
  title.textContent = i18n.t("modelPicker.title");
  head.appendChild(title);
  const pill = document.createElement("span");
  pill.className = "current-pill";
  pill.textContent = i18n.t("modelPicker.current", { model: currentModel });
  head.appendChild(pill);
  card.appendChild(head);

  // Group by provider when any model carries a provider so duplicates
  // (e.g. glm-5.2 under glm + opencode) each get their own row.
  const hasProviders = models.some((m) => m.provider);
  const byProvider = new Map<string, { name: string; provider?: string }[]>();
  if (hasProviders) {
    models.forEach((m) => {
      const p = m.provider || "(default)";
      if (!byProvider.has(p)) byProvider.set(p, []);
      byProvider.get(p)!.push(m);
    });
  }

  const list = document.createElement("div");
  list.className = "model-picker-list";

  const renderRow = (m: { name: string; provider?: string }) => {
    const row = document.createElement("div");
    row.className = "model-picker-row";
    const isCurrent = m.name === currentModel &&
      (!hasProviders || !currentProvider || !m.provider || m.provider === currentProvider);
    if (isCurrent) row.classList.add("is-current");

    const check = document.createElement("span");
    check.className = "check";
    check.textContent = "✓";
    row.appendChild(check);

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = m.name;
    row.appendChild(name);

    if (isCurrent) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = i18n.t("modelPicker.inUse");
      row.appendChild(badge);
    }

    // Click anywhere on the row to switch — mirrors typing
    // `/model <provider:model>` and Enter. Rebuild the composer dropdown
    // (option values are "provider|model") via hydrateComposer; assigning a
    // bare name would leave the select empty.
    row.addEventListener("click", async () => {
      if (isCurrent) return; // already on it
      try {
        const payload: Record<string, string> = { model_name: m.name };
        if (m.provider) payload.provider_name = m.provider;
        await api.updateSession(sid, payload);
        const sessions = store.get().sessions;
        const s = sessions.find(x => x.id === sid);
        if (s) { (s as any).model_name = m.name; (s as any).provider_name = m.provider ?? null; }
        hydrateComposer(sid);
        // Swap the picker for a confirmation card (same code path as
        // `/model <name>` Enter).
        const detail = m.provider ? `${m.provider}:${m.name}` : m.name;
        const host = card.parentElement;
        card.remove();
        appendSystem(CHECK_ICON_SVG, i18n.t("system.switchedModel"), detail, host || undefined);
      } catch (err: any) {
        appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
        console.error("updateSession(model_name) failed:", err);
      }
    });

    list.appendChild(row);
  };

  if (hasProviders) {
    [...byProvider.keys()].sort().forEach((p) => {
      // Only show the provider subhead when there's >1 group — otherwise the
      // single provider is just noise.
      if (byProvider.size > 1) {
        const sub = document.createElement("div");
        sub.className = "model-picker-group";
        sub.textContent = p;
        list.appendChild(sub);
      }
      byProvider.get(p)!.forEach(renderRow);
    });
  } else {
    models.forEach(renderRow);
  }
  card.appendChild(list);

  // Foot: hint
  const foot = document.createElement("div");
  foot.className = "model-picker-foot";
  foot.innerHTML = i18n.t("modelPicker.hint");
  card.appendChild(foot);

  revealMessagesTarget(target);
  target.appendChild(card);
  card.scrollIntoView({ behavior: "smooth", block: "end" });
  invalidateLiveStreamEl();
}

// Structured effort picker for the no-arg `/effort` command. Mirrors the
// model picker: current level as a labeled header, each supported level as
// a clickable row, footer hint. Reuses `.model-picker` styles. Empty
// `levels` (non-thinking model) renders an explanatory note instead.
function appendEffortPicker(
  current: string,
  levels: string[],
  sid: string,
  target: HTMLElement = (getLiveStreamTarget() || $("messages")),
) {
  const card = document.createElement("div");
  card.className = "model-picker";

  const head = document.createElement("div");
  head.className = "model-picker-head";
  const title = document.createElement("h4");
  title.textContent = i18n.t("effortPicker.title");
  head.appendChild(title);
  const pill = document.createElement("span");
  pill.className = "current-pill";
  pill.textContent = i18n.t("effortPicker.current", { effort: current === "disabled" ? "off" : current });
  head.appendChild(pill);
  card.appendChild(head);

  if (!levels.length) {
    const empty = document.createElement("div");
    empty.className = "model-picker-foot";
    empty.textContent = i18n.t("effortPicker.none");
    card.appendChild(empty);
    revealMessagesTarget(target);
    target.appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "end" });
    invalidateLiveStreamEl();
    return;
  }

  const list = document.createElement("div");
  list.className = "model-picker-list";
  ["disabled", ...levels].forEach((lv) => {
    const row = document.createElement("div");
    row.className = "model-picker-row";
    const isCurrent = lv === current;
    if (isCurrent) row.classList.add("is-current");

    const check = document.createElement("span");
    check.className = "check";
    check.textContent = "✓";
    row.appendChild(check);

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = lv === "disabled" ? "off" : lv;
    row.appendChild(name);

    if (isCurrent) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = i18n.t("modelPicker.inUse");
      row.appendChild(badge);
    }

    row.addEventListener("click", async () => {
      if (isCurrent) return;
      try {
        await api.updateSession(sid, { thinking_mode: lv });
        const sessions = store.get().sessions;
        const s = sessions.find(x => x.id === sid);
        if (s) (s as any).thinking_mode = lv;
        hydrateComposer(sid);
        const host = card.parentElement;
        card.remove();
        appendSystem(CHECK_ICON_SVG, "Effort", lv === "disabled" ? "off" : lv, host || undefined);
      } catch (err: any) {
        appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
        console.error("updateSession(thinking_mode) failed:", err);
      }
    });

    list.appendChild(row);
  });
  card.appendChild(list);

  const foot = document.createElement("div");
  foot.className = "model-picker-foot";
  foot.innerHTML = i18n.t("effortPicker.hint");
  card.appendChild(foot);

  revealMessagesTarget(target);
  target.appendChild(card);
  card.scrollIntoView({ behavior: "smooth", block: "end" });
  invalidateLiveStreamEl();
}

const CHECK_ICON_SVG = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3.5 8.5 6.5 11.5 12.5 5"/></svg>`;
const SPARK_ICON_SVG = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l1.6 3.9 3.9 1.6-3.9 1.6L8 12.5 6.4 8.6 2.5 7l3.9-1.6z"/></svg>`;
const STOP_ICON_SVG = `<svg viewBox="0 0 16 16" fill="currentColor"><rect x="3.5" y="3.5" width="9" height="9" rx="1.5"/></svg>`;

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

function setCompactToastState(state: "loading" | "success" | "error", message: string, sid?: string): void {
  if (sid) {
    const { compactingSessions } = store.get();
    if (state === "loading") {
      store.set({ compactingSessions: { ...compactingSessions, [sid]: true } });
    } else {
      const next = { ...compactingSessions };
      delete next[sid];
      store.set({ compactingSessions: next });
    }
  }
  // Only show toast if it's for the active session
  const { activeSid } = store.get();
  if (sid && sid !== activeSid) return;
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

function scrollBottom(target: HTMLElement = (getLiveStreamTarget() || $("messages"))) {
  if (store.get().autoScroll) { target.scrollTop = target.scrollHeight; }
}

// Scroll a session's own messages container to the bottom.
function scrollSessionBottom(sid: string) {
  const el = sessionMessagesEl(sid);
  if (el) scrollBottom(el);
}

// ---- SSE Event Handling ----

// Single global SSE connection delivers events for every session. Each
// event carries a `session_id` field (set by the server's runtime._emit),
// which we use to route to the right handler. For the active session we
// render live; for background sessions we only sync the sidebar + per-
// session running flag — the chat DOM is rebuilt from history on switch.
function routeSSEEvent(ev: api.Event) {
  const t0 = ev.type as string;
  // Global (non-session) events — handle before any session-id routing,
  // since they don't carry a real session_id (the backend uses a
  // "_global" sentinel we deliberately ignore here).
  if (t0 === "skill_index_changed") {
    handleSkillIndexChanged();
    return;
  }
  const sid = (ev as any).session_id as string | undefined;
  if (!sid) return;
  // User pressed stop — drop in-flight tail events until the server
  // confirms with turn_cancelled/turn_failed. Without this, the next
  // 50-200ms of buffered stream chunks keep growing the assistant
  // bubble and re-pulsing the typing chip between stop click and
  // turn_cancelled arrival, which the user perceives as "slow".
  // We still let terminal events through so the cancellation
  // teardown (clear stream / pendingTools / runningSessions) fires.
  if (isSidCancelling(sid) && t0 !== "turn_cancelled" && t0 !== "turn_failed" && t0 !== "turn_end") {
    return;
  }
  const { activeSid, splitSessions } = store.get();
  if (sid === activeSid) {
    handleSessionEvent(sid, ev, true);
  } else {
    if (splitSessions.includes(sid)) {
      // Live-stream into the pane (delta / reasoning_delta / model_response /
      // tool_* / ask_user / permission). Previously only turn-boundary events
      // refreshed the pane from history, so a split session showed just the
      // user message with no streaming model output and no stop button.
      handleSessionEvent(sid, ev, false);
      const t = ev.type as string;
      // Update this pane's context ring from usage events. Without this
      // routing, a background pane's token ring never moved — usage_update
      // / round_complete were only handled for the active session.
      if (t === "usage_update" || t === "round_complete") {
        const usage = (ev as any).usage as { prompt_tokens?: number } | undefined;
        if (usage?.prompt_tokens !== undefined) {
          const contextWindow = store.get().config.contextWindow || 200000;
          const pct = Math.min(usage.prompt_tokens / contextWindow, 1);
          updateContextProgress(pct, usage.prompt_tokens, sid);
        }
      }
    }
    syncBackgroundSession(sid, ev);
  }
}

function syncBackgroundSession(sid: string, ev: api.Event) {
  const t = ev.type as string;
  const { sessions, runningSessions, compactingSessions } = store.get();
  const s = sessions.find(x => x.id === sid);
  if (!s) return;

  if (t === "turn_start") {
    s.status = "running";
    const next = { ...runningSessions, [sid]: true };
    store.set({ sessions: [...sessions], runningSessions: next });
    renderSessions();
    // Reflect in the per-pane send/stop button (matters when the session
    // is visible in a split pane, not just the active one).
    setComposerRunning(sid, true);
    // The server's `turn_start` event doesn't carry the user message
    // body, so we can't update the sidebar title from the event
    // payload. Fetch the session's first user message and use it as
    // the preview so the sidebar shows the actual question, not the
    // session id stub.
    refreshSessionPreview(sid);
  } else if (t === "status" && (ev as any).content === "compact") {
    // Background session's runtime hook entered auto-compact. Keep
    // compactingSessions in sync so when the user switches to this
    // session later, loadHistory shows the right toast (and not a
    // stale one from a prior compact that's already finished).
    store.set({ compactingSessions: { ...compactingSessions, [sid]: true } });
  } else if (t === "context_compacted") {
    // Background session's auto-compact finished. Clear the flag so
    // re-opening the session after the compact completes doesn't show
    // a ghost "Compacting context..." toast.
    if (compactingSessions[sid]) {
      const next = { ...compactingSessions };
      delete next[sid];
      store.set({ compactingSessions: next });
    }
  } else if (t === "turn_end" || t === "turn_cancelled" || t === "turn_failed") {
    // Mirrors handleSessionEvent's wasRunning guard: a flush from a
    // previous terminal event may have already started a new turn on
    // this sid (turn_start ran first and set runningSessions[sid]=true).
    // If we're seeing a late duplicate terminal event for the OLD turn,
    // clearing state here would clobber the new turn's running flag and
    // the sidebar would flicker. The new turn's own terminal event will
    // fire later and clean up properly.
    const wasRunning = !!runningSessions[sid];
    const shouldClobber = !(t === "turn_cancelled" && wasRunning);
    if (shouldClobber) {
      s.status = t === "turn_failed" ? "failed" : (t === "turn_cancelled" ? "idle" : "done");
      const next = { ...runningSessions };
      delete next[sid];
      store.set({ sessions: [...sessions], runningSessions: next });
      renderSessions();
      // Reflect the just-finished turn in the per-pane send/stop button
      // (matters when the session is visible in a split pane, not just active).
      setComposerRunning(sid, false);
    }
    // If the session is shown in a non-active split pane, the live SSE
    // stream only updates #messages, so the secondary pane's optimistic
    // copy plus the streamed assistant turn are stale. Re-fetch the
    // pane from the server to pick up the finalised assistant message.
    const { activeSid: curActive, splitSessions: curSplit } = store.get();
    if (sid !== curActive && curSplit.includes(sid)) {
      const paneMessages = sessionMessagesEl(sid);
      if (paneMessages) loadHistoryInto(sid, paneMessages);
    }
    // Flush the queue for terminal events. Mirrors handleSessionEvent's
    // behavior — turn_end uses a 200ms delay so the server has cleared
    // turn_task before the queued createTurn lands, while cancel/fail
    // flush immediately (cancel_turn clears turn_task synchronously).
    // Skip when shouldClobber is false: a new turn is already running,
    // its own terminal event will flush the queue when it ends.
    if (shouldClobber) {
      flushComposerQueue(sid, t === "turn_end" ? 200 : 0);
    }
  }
}

async function refreshSessionPreview(sid: string) {
  try {
    const data = await api.getMessages(sid);
    let userMsg = (data.messages || []).find(m => m.role === "user");
    // If compacted, filtered view may have no user messages — check full history
    if (!userMsg) {
      const fullData = await api.getMessages(sid, { includeDropped: true });
      userMsg = (fullData.messages || []).find(m => m.role === "user");
    }
    if (!userMsg) return;
    const preview = previewText(userMsg.content);
    const { sessions } = store.get();
    const s = sessions.find(x => x.id === sid);
    if (!s || s.preview === preview) return;
    s.preview = preview;
    store.set({ sessions: [...sessions] });
    renderSessions();
    // Sync any split-pane header that is showing this session.
    const paneTitle = document.querySelector(`.split-pane-secondary[data-sid="${sid}"] .split-pane-title`) as HTMLElement | null;
    if (paneTitle) paneTitle.textContent = preview;
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
          handleSessionEvent(sid, ev, false);
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

// Handle the global `skill_index_changed` event emitted by the backend
// whenever the on-disk SKILL.md tree changes (new/removed/edited skill).
// The frontend caches the skill list in module-level memory
// (see modals/skills.ts); without this listener the cache is populated
// once per page load and never refreshed, so newly installed skills
// stay invisible until a hard reload.
function handleSkillIndexChanged(): void {
  invalidateSkillsCache();
  // If the Skills modal is already mounted, re-fetch and re-render in
  // place. If not, the next open() will hit the network anyway because
  // invalidateSkillsCache() nulled the cache.
  void refreshSkillsModalInPlace();
}

// When SSE reconnects after a disconnect, reconcile running sessions
// in case we missed turn_end / turn_failed events.
sse.onReconnect(() => {
  reconcileRunningSessions();
});

async function reconcileRunningSessions() {
  const { runningSessions, sessions } = store.get();
  const runningSids = Object.keys(runningSessions);
  if (runningSids.length === 0) return;
  for (const sid of runningSids) {
    try {
      const turns = await api.getTurns(sid);
      const hasRunning = turns.some(t => t.status === "running");
      if (!hasRunning) {
        const next = { ...store.get().runningSessions };
        delete next[sid];
        const s = sessions.find(x => x.id === sid);
        if (s) {
          const lastTurn = turns[turns.length - 1];
          s.status = lastTurn
            ? (lastTurn.status === "failed" ? "failed" : "done")
            : "idle";
        }
        store.set({ sessions: [...sessions], runningSessions: next });
        // If this was the active session, update UI
        if (sid === store.get().activeSid) {
          removeTyping();
          setActiveRunning(false);
          updateSendStopButton();
          await loadHistory(sid);
        }
        renderSessions();
      }
    } catch { /* best-effort */ }
  }
}

function handleSessionEvent(sid: string, ev: api.Event, updateScroll: boolean = true) {
  if (!sid) return;
  setLiveStreamSid(sid);
  try {
  // Skip re-emitted internal sub-agent events (delta, tool_start, tool_end, etc.)
  // But let subagent_start / subagent_end through for background agent display.
  if ((ev as any)._subagent && ev.type !== "subagent_start" && ev.type !== "subagent_end") return;

  const t = ev.type as string;
  const targetRaw = sessionMessagesEl(sid);
  // No DOM container for this session — typically a hidden automation
  // backing session. The legacy `|| $("messages")` fallback leaked
  // automation streaming into the user's active session whenever the
  // runtime emitted chat events for a sid that wasn't open in the UI.
  // Only bail for events that actually paint into the messages pane;
  // routing-only events (`automation_run`, `turn_start`, usage updates,
  // round_complete, doom_loop_detected, status/context_compacted) still
  // need to fire so the Automations modal refreshes and the sidebar
  // tracks session status.
  const needsTarget =
    t === "subagent_start" || t === "subagent_end" ||
    t === "delta" || t === "reasoning_delta" || t === "model_response" ||
    t === "ask_user_question" ||
    t === "tool_start" || t === "tool_end" ||
    t === "stream_reset" || t === "turn_end" ||
    t === "turn_cancelled" || t === "turn_failed" || t === "turn_error";
  if (needsTarget && !targetRaw) return;
  // For every needsTarget branch below, targetRaw is guaranteed non-null
  // (we just returned otherwise). TS can't carry that narrowing across the
  // computed `needsTarget` flag, so bind a narrowed alias once — this lets
  // the rest of the function use `target` without per-call `!` assertions.
  // Non-needsTarget branches (automation_run, turn_start, usage, ...) never
  // touch `target`, so casting there is harmless.
  const target = targetRaw as HTMLElement;
  setLiveStreamTarget(target);
  const { activeSid, sessions } = store.get();

  if (t === "subagent_start") {
    const agentId = (ev as any).agent_id;
    const taskDesc = String((ev as any).task || "Background agent");
    const isBg = !!(ev as any).background;
    if (isBg && (!sid || sid === activeSid)) {
      removeTyping();
      const card = document.createElement("div");
      card.className = "agent-card agent-running";
      card.id = `agent-card-${agentId}`;
      card.innerHTML = `
        <div class="agent-card-header">
          <span class="agent-card-icon">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          </span>
          <span class="agent-card-title">${i18n.t("agent.title")}</span>
          <span class="agent-card-status running">${i18n.t("agent.statusRunning")}</span>
        </div>
        <div class="agent-card-task">${esc(taskDesc)}</div>
      `;
      target!.appendChild(card);
      if (updateScroll) scrollBottom(target!);
    }
    return;
  } else if (t === "subagent_end") {
    const agentId = (ev as any).agent_id;
    const isBg = !!(ev as any).background;
    const status = (ev as any).status || "completed";
    if (isBg && agentId) {
      const card = document.getElementById(`agent-card-${agentId}`);
      if (card) {
        card.classList.remove("agent-running");
        card.classList.add(status === "failed" || status === "cancelled" ? "agent-failed" : "agent-done");
        const statusEl = card.querySelector(".agent-card-status");
        if (statusEl) {
          statusEl.classList.remove("running");
          statusEl.classList.add(status === "failed" || status === "cancelled" ? "failed" : "done");
          statusEl.textContent = status === "failed" ? i18n.t("agent.statusFailed") : status === "cancelled" ? i18n.t("agent.statusCancelled") : i18n.t("agent.statusDone");
        }
        const toolsUsed = (ev as any).tools_used || 0;
        const toolsSummary = (ev as any).tools_summary as Record<string, number> | undefined;
        const toolsLine = toolsSummary && Object.keys(toolsSummary).length > 0
          ? Object.entries(toolsSummary).map(([n, c]) => `${n} ×${c}`).join(" · ")
          : i18n.plural(toolsUsed, { one: i18n.t("agent.toolsOne", { n: toolsUsed }), other: i18n.t("agent.toolsMany", { n: toolsUsed }) });
        const resultPreview = String((ev as any).result_preview || "");
        let detail = `<div class="agent-card-meta">${esc(toolsLine)}</div>`;
        if (resultPreview) {
          detail += `<div class="agent-card-result">${renderMarkdown(resultPreview)}</div>`;
        }
        card.innerHTML = card.innerHTML.replace('</div>'.repeat(1), '') + detail + '</div>';
        // Simpler: just rebuild the body
        const taskDesc = card.querySelector(".agent-card-task")?.textContent || "";
        card.innerHTML = `
          <div class="agent-card-header">
            <span class="agent-card-icon">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                ${status === "completed"
                  ? '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'
                  : '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>'
                }
              </svg>
            </span>
            <span class="agent-card-title">${i18n.t("agent.title")}</span>
            <span class="agent-card-status ${status === 'failed' || status === 'cancelled' ? 'failed' : 'done'}">${status === 'failed' ? i18n.t("agent.statusFailed") : status === 'cancelled' ? i18n.t("agent.statusCancelled") : i18n.t("agent.statusDone")}</span>
          </div>
          <div class="agent-card-task">${esc(taskDesc)}</div>
          <div class="agent-card-meta">${esc(toolsLine)}</div>
          ${resultPreview ? `<div class="agent-card-result">${renderMarkdown(resultPreview)}</div>` : ''}
        `;
      }
      if (updateScroll) scrollBottom(target!);
    }
    return;
  }

  if (t === "turn_start") {
    if (sid) {
      const active = sessions.find(s => s.id === sid);
      if (active) {
        active.status = "running";
        store.set({ sessions: [...sessions] });
        renderSessions();
        refreshSessionPreview(sid);
      }
    }
    if (!sid || sid === activeSid) {
      setActiveRunning(true);
      appendTyping();
      updateSendStopButton();
    } else {
      // Secondary split-pane session: reflect running state on its own
      // composer + typing chip (per-sid, no active/background fork).
      setComposerRunning(sid, true);
      appendTyping();
    }
  } else if (t === "usage_update") {
    const usage = ev.usage as { prompt_tokens?: number; completion_tokens?: number } | undefined;
    if (usage?.prompt_tokens !== undefined) {
      const contextWindow = store.get().config.contextWindow || 200000;
      const pct = Math.min(usage.prompt_tokens / contextWindow, 1);
      updateContextProgress(pct, usage.prompt_tokens, sid);
    }
  } else if (t === "model_changed") {
    const modelName = (ev as any).model_name as string | undefined;
    if (!modelName) return;
    const si = sessions.findIndex(s => s.id === sid);
    if (si !== -1 && (sessions[si] as any).model_name !== modelName) {
      const next = [...sessions];
      (next[si] as any).model_name = modelName;
      store.set({ sessions: next });
    }
    const modelSel = composerModelSelect(sid);
    if (modelSel && Array.from(modelSel.options).some((o) => o.value === modelName)) {
      if (modelSel.value !== modelName) {
        modelSel.value = modelName;
      }
    }
  } else if (t === "stream_reset") {
    // Server: provider returned a retryable error (e.g. 1027 output
    // sensitive) mid-stream. The next attempt will replay the same
    // input from scratch. Wipe the partial text the deltas already
    // painted so the new attempt's deltas land in a fresh block.
    // Disk / history are untouched on the server side, so a page
    // refresh mid-retry would still show the right state.
    resetStreamingState(sid);
  } else if (t === "delta") {
    removeTyping(target!);
    clearPaneEmptyPlaceholder(target!);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target!);
    const content = contentToText(ev.content);
    (el as any)._main += content;
    // Throttle expensive DOM operations during streaming
    if (!(el as any)._renderTimer) {
      (el as any)._renderTimer = setTimeout(() => {
        (el as any)._renderTimer = null;
        renderAssistantContent(el);
        if (updateScroll) scrollBottom(target!);
      }, 80);
    }
  } else if (t === "reasoning_delta") {
    // Anthropic / OpenAI o1/o3 with `reasoning_effort` emit chain-of-thought
    // in a separate `reasoning_content` field, surfaced by the runtime as
    // `reasoning_delta` events. Accumulate into a buffer alongside the
    // main content; renderAssistantContent() merges both into the
    // thinking card. We share the same throttle timer as the main
    // `delta` handler so a fast reasoning burst doesn't double-render.
    removeTyping(target!);
    clearPaneEmptyPlaceholder(target!);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target!);
    const content = contentToText(ev.content);
    (el as any)._reasoning += content;
    if (!(el as any)._renderTimer) {
      (el as any)._renderTimer = setTimeout(() => {
        (el as any)._renderTimer = null;
        renderAssistantContent(el);
        if (updateScroll) scrollBottom(target!);
      }, 80);
    }
  } else if (t === "model_response") {
    // Final full response; ensure _main matches exactly to avoid drift from deltas
    removeTyping(target!);
    clearPaneEmptyPlaceholder(target!);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target!);
    // Cancel any pending throttle timer so it doesn't overwrite the final render
    if ((el as any)._renderTimer) {
      clearTimeout((el as any)._renderTimer);
      (el as any)._renderTimer = null;
    }
    const content = contentToText(ev.content);
    (el as any)._main = content;
    // The runtime currently doesn't include `reasoning_content` in the
    // model_response payload — we keep the value accumulated from the
    // earlier `reasoning_delta` events, which is the canonical source.
    renderAssistantContent(el);
    addCopyButtons(el.parentElement!);
    highlightCode(el.parentElement!);
    if (updateScroll) scrollBottom(target);
  } else if (t === "ask_user_question") {
    removeTyping(target!);
    clearPaneEmptyPlaceholder(target!);
    const q = String((ev.question as string) || "");
    const opts = (ev.options as unknown[]) || [];
    const ms = !!ev.multi_select;
    const cid = (ev.call_id as string) || undefined;
    // Skip if renderMessages already rendered an answered card for this
    // question (happens when replaying a running turn whose earlier
    // ask_user calls have already been answered and persisted as tool
    // results in the message history).
    const existing = target.querySelectorAll(".question-card-answered .question-text");
    const alreadyAnswered = Array.from(existing).some(el => (el.textContent || "").trim() === q);
    if (!alreadyAnswered) {
      appendQuestionCard(q, opts, ms, cid, target, sid);
    }
    if (updateScroll) scrollBottom(target);
  } else if (t === "tool_start") {
    removeTyping(target);
    clearPaneEmptyPlaceholder(target);
    const key = `${ev.round}:${ev.call_id || ev.tool}`;
    const card = appendToolCard(ev.tool as string, (ev.arguments || {}) as Record<string, unknown>, "running");
    streamCtx(sid).pendingTools.set(key, card);
    if (updateScroll) scrollBottom(target);
  } else if (t === "tool_end") {
    const key = `${ev.round}:${ev.call_id || ev.tool}`;
    const pending = streamCtx(sid).pendingTools.get(key);
    if (pending) {
      streamCtx(sid).pendingTools.delete(key);
      pending.remove();
    }
    if (ev.tool === "ask_user") {
      // Card is already on screen via the earlier `ask_user_question`
      // event. Don't create a second one — the user has already (or
      // is about to) answer it.
    } else {
      const status = ev.error_class ? "error" : "success";
      let subagentTools: Record<string, number> | undefined;
      if (ev.tool === "spawn_agent") {
        const output = (ev.output || {}) as Record<string, unknown>;
        subagentTools = output.tools_summary as Record<string, number> | undefined;
      }
      // Image tool_end events carry image_url directly so the card can be
      // rendered immediately without a deferred history lookup. Record the
      // card by call_id in case later events need to reference it.
      let output = ev.output;
      const card = appendToolCard(ev.tool as string, (ev.arguments || {}) as Record<string, unknown>, status, output, subagentTools);
      const callId = (ev.call_id || "") as string;
      if (callId) streamCtx(sid).toolCards.set(callId, card);
      // Image tools now carry image_url in the tool_end output so the card
      // is rendered immediately; no need for a deferred history lookup.
      // Update plan tab if this is an update_plan tool. Pass the
      // owning sid so the cache key is per-session; the renderer in
      // right-panel.ts reads/writes store.currentPlanSteps[sid] and
      // only paints the panel if this sid is the active one.
      if (ev.tool === "update_plan") {
        const planSteps = (output as any)?.plan as { id?: string; description?: string; status?: string }[] | undefined;
        if (planSteps) {
          updatePlanTabContent(sid, planSteps);
        }
      }
    }
    // If this tool may have modified workspace files, refresh the diff
    // panel in the background so the user sees changes live. Only when
    // the event belongs to the active session — other sessions' diffs
    // are refreshed when the user switches to them.
    if (sid === activeSid && FILE_MUTATING_TOOLS.has(ev.tool as string)) {
      scheduleDiffRefresh();
    }
    if (updateScroll) scrollBottom(target);
  } else if (t === "permission_request" || t === "approval_request") {
    removeTyping(target);
    const req = (ev.request || ev) as Record<string, unknown>;
    const tool = (req.tool || {}) as Record<string, unknown>;
    const requestId = (req.id || req.request_id || "") as string;
    const toolName = (tool.name || req.tool_name || "unknown") as string;
    const args = (tool.arguments || req.arguments || {}) as Record<string, unknown>;
    appendApprovalCard(requestId, toolName, args, target);
    if (updateScroll) scrollBottom(target);
  } else if (t === "turn_end") {
    // Trust the live streaming events to have already rendered the new
    // user / assistant / tool messages. We previously called `loadHistory`
    // here to "reconcile" with disk, but that wiped the entire message
    // container and re-rendered from scratch on every turn — a jarring
    // "flash" of the full conversation. We only refresh lightweight
    // sidebar / context surfaces that don't drive the chat display.
    //
    // Clear the cancelling flag here too (defensive — turn_cancelled
    // normally clears it, but on a normal-completion turn we still want
    // to drop any stale marker from a prior stop attempt on this sid).
    if (sid) clearSidCancelled(sid);
    const { activeSid, sessions, runningSessions } = store.get();

    if (sid) {
      const active = sessions.find(s => s.id === sid);
      if (active) active.status = "done";
      const next = { ...runningSessions };
      delete next[sid];
      store.set({ sessions: [...sessions], runningSessions: next });
      renderSessions();
    }

    if (!sid || sid === activeSid) {
      removeTyping(target);
      // Any question card still on screen is now abandoned — the round
      // closed without an answer (probably cancelled). Lock its inputs
      // so the user can't submit a reply that will land in a new turn.
      target.querySelectorAll(".question-card:not(.question-card-answered)").forEach((el) => {
        el.classList.add("question-card-cancelled");
        (el.querySelectorAll("input, button") as NodeListOf<HTMLInputElement | HTMLButtonElement>).forEach((b) => {
          b.disabled = true;
        });
      });
      setActiveRunning(false);
      invalidateLiveStreamEl();
      updateSendStopButton();
      refreshPlan();
      if ($("rightPanel").classList.contains("show")) refreshActiveReviewTabs();
    }
    // Flush queued prompts now that this session's turn has closed. This
    // is OUTSIDE the activeSid guard so a queued message still flushes if
    // the user switched sessions while waiting. 200ms lets the server
    // clear turn_task before the next createTurn — a shorter delay races
    // the server's turn teardown and the queued createTurn comes back 429
    // turn_already_running, which strands the rest of the queue (the
    // failed createTurn fires no turn_end to flush the next item).
    flushComposerQueue(sid, 200);
    // Patch rewind buttons onto the just-finished turn's messages in place
    // (no reload → no flash). The streaming append path omits data-idx; this
    // fetches fullMsgs and stamps data-idx + the ↩ button onto the user/tool
    // elements already in the DOM, in document order.
    if (sid === store.get().activeSid) patchRewindButtons(sid);
  } else if (t === "round_complete") {
    invalidateLiveStreamEl();
    const usage = ev.usage as { prompt_tokens?: number; completion_tokens?: number } | undefined;
    if (usage?.prompt_tokens) {
      const contextWindow = store.get().config.contextWindow || 200000;
      const pct = Math.min(usage.prompt_tokens / contextWindow, 1);
      updateContextProgress(pct, usage.prompt_tokens, sid);
    }
  } else if (t === "status" && (ev as any).content === "compact") {
    const activeSid = store.get().activeSid;
    if (activeSid) setCompactToastState("loading", i18n.t("toast.compacting"), activeSid);
  } else if (t === "context_compacted") {
    // Auto-compact finished (or the server's echo of a manual /compact).
    // Use the SAME completion path as /compact — one reload + toast.
    if (sid) applyCompactionComplete(sid, i18n.t("toast.compacted"));
  } else if (t === "doom_loop_detected") {
    removeTyping();
  } else if (t === "turn_error") {
    const { activeSid, sessions, runningSessions } = store.get();
    if (sid) {
      const active = sessions.find(s => s.id === sid);
      if (active) active.status = "failed";
      const next = { ...runningSessions };
      delete next[sid];
      store.set({ sessions: [...sessions], runningSessions: next });
      renderSessions();
    }
    if (!sid || sid === activeSid) {
      removeTyping();
      setActiveRunning(false);
      updateSendStopButton();
      streamCtx(sid).pendingTools.forEach((card) => updateToolCardStatus(card, "cancelled"));
      streamCtx(sid).pendingTools.clear();
      appendError((ev.error as string) || i18n.t("toast.unknownError"));
    }
  } else if (t === "turn_cancelled" || t === "turn_failed") {
    // The syncBackgroundSession handler (line ~2092) already updates
    // the sidebar status. For the active session we ALSO need to
    // clear the "Thinking..." chip + reset running state, otherwise
    // a cancelled turn leaves the UI stuck on the running state
    // (no further events will ever come for it).
    //
    // Race watch: when the user clicks stop and the queue is
    // non-empty, the turn_cancelled handler below flushes the queue
    // with a 200ms delay (see flushComposerQueue call), which spawns
    // a NEW turn. The OLD turn's `turn_cancelled` and the NEW turn's
    // `turn_start` race on the SSE stream. If `turn_cancelled`
    // arrives AFTER `turn_start`, runningSessions is already true
    // for the new turn — we must NOT clobber it. The wasRunning gate
    // below is what makes this safe: a fresh turn_start has already
    // cleared runningSessions[sid], so wasRunning is false and we
    // skip the setActiveRunning(false) / updateSendStopButton() calls.
    //
    // Clear the cancellingSids marker so a subsequent turn_start on
    // the same sid isn't dropped by routeSSEEvent's tail filter.
    const isCancelling = sid ? isSidCancelling(sid) : false;
    if (sid) clearSidCancelled(sid);
    const { activeSid, runningSessions } = store.get();
    const wasRunning = sid ? !!runningSessions[sid] : false;
    
    // If we were cancelling AND wasRunning is still true, it means a NEW turn 
    // has already started (via flushComposerQueue) and we shouldn't clobber it!
    const shouldClobber = !(isCancelling && wasRunning);

    if (sid && shouldClobber) {
      const next = { ...runningSessions };
      delete next[sid];
      store.set({ runningSessions: next });
    }
    if (!sid || sid === activeSid) {
      // Always tear down the live-stream element + typing chip for the
      // active session on cancel/fail — NOT gated on `wasRunning`.
      // cancelComposerTurn sets runningSessions[sid]=false BEFORE
      // turn_cancelled arrives, so gating on wasRunning would skip
      // invalidateLiveStreamEl and the next queued message's assistant
      // deltas flow into the cancelled turn's bubble ("first message
      // appears after the answer").
      if (shouldClobber) removeTyping();
      if (shouldClobber) invalidateLiveStreamEl();
      if (wasRunning && shouldClobber) {
        setActiveRunning(false);
        updateSendStopButton();
        if (t === "turn_failed") {
          appendError(i18n.t("toast.turnFailed"));
        }
      }
      // Any tool cards that were still in the running state are now
      // abandoned. Mark them cancelled so they don't show "running..."
      // forever.
      if (shouldClobber) {
        streamCtx(sid).pendingTools.forEach((card) => updateToolCardStatus(card, "cancelled"));
        streamCtx(sid).pendingTools.clear();
      }
    }
    // Flush queued prompts now that this session's turn has closed (cancel /
    // fail). OUTSIDE the activeSid guard (matches turn_end) so a queued
    // message still flushes even if the user switched sessions. Runs after
    // invalidateLiveStreamEl above (when active), so the next message's
    // assistant stream lands in a fresh element.
    // The previous 200ms delay was meant to let the server clear
    // turn_task, but cancel_turn already does that synchronously, so the
    // queue can flush immediately — the user clicked stop, they expect
    // the queued follow-up to fire right away.
    if (shouldClobber) {
      flushComposerQueue(sid, 0);
    }
  } else if (t === "automation_run") {
    // A background automation run finished. Refresh the list modal (if
    // open) so row previews update, and refresh any open detail page so
    // its last_result shows without reopening. Previously this referenced
    // the wrong element id ("automationModal") and a non-existent ".show"
    // class, so the refresh never fired and the final result never
    // appeared in the UI even though the backend had persisted it.
    if (document.getElementById("automationsModalBackdrop")) {
      void loadAutomationsIntoModal();
    }
    refreshAutomationDetailIfOpen();
  }

  updateConnStatus(sse.isConnected());
  } finally { setLiveStreamSid(null); setLiveStreamTarget(null); }
}


// Bridge into runtime-state's _deps — runtime-state can't import
// setComposerRunning directly (would create a circular import), so
// the activeSid-agnostic side asks main.ts to refresh the active
// composer's button. Kept as a named function (not inlined at the
// 10-ish call sites) so the dependency is grep-able.
function updateSendStopButton() {
  const sid = store.get().activeSid || "";
  if (sid) setComposerRunning(sid, isActiveRunning());
}

function updateContextProgress(pct: number, tokens: number, sid?: string) {
  const normalizedPct = Math.max(0, Math.min(pct, 1));
  // Color: green → yellow → red
  const stroke = normalizedPct > 0.85 ? "var(--red)"
    : normalizedPct > 0.6 ? "var(--orange)"
    : "var(--accent)";
  const pctText = Math.round(normalizedPct * 100) + "%";
  // Every composer's ring is `.pane-context-arc[data-sid]` now (one
  // geometry, r=11). Resolve by sid — works for full-screen and panes alike.
  const targetSid = sid || store.get().activeSid || "";
  const arc = composerContextArc(targetSid);
  const pctLabel = composerContextPct(targetSid);
  if (!arc || !pctLabel) return;
  const circumference = 69.12; // 2 * π * 11
  arc.setAttribute("stroke-dashoffset", String(circumference * (1 - normalizedPct)));
  arc.setAttribute("stroke", stroke);
  // A fresh session reads "2%" like a glitch (3-5k tokens / 200k window).
  // Tap the ring to flip between % and a short token count; remember the
  // preferred mode across updates.
  const label = pctLabel as any;
  label._tokens = tokens;
  if (!label._toggleBound) {
    label._toggleBound = true;
    pctLabel.style.cursor = "pointer";
    pctLabel.addEventListener("click", () => {
      pctLabel.dataset.mode = pctLabel.dataset.mode === "tokens" ? "pct" : "tokens";
      const tk = (pctLabel as any)._tokens || 0;
      const cur = parseFloat(pctLabel.dataset.pct || "0");
      pctLabel.textContent = pctLabel.dataset.mode === "tokens"
        ? (tk >= 1000 ? (tk / 1000).toFixed(1) + "k" : String(tk))
        : Math.round(cur * 100) + "%";
    });
  }
  pctLabel.dataset.pct = String(normalizedPct);
  pctLabel.textContent = pctLabel.dataset.mode === "tokens"
    ? (tokens >= 1000 ? (tokens / 1000).toFixed(1) + "k" : String(tokens))
    : pctText;
  pctLabel.title = `${tokens.toLocaleString()} tokens in context`;
}

// ---- Queue (Codex-style) ----
// While a turn is running, Enter / send-button stashes the typed text
// into the active session's queue instead of opening a parallel turn.
// The `turn_end` event flushes it. The user sees a chip above the
// composer with a one-click edit / clear affordance. Per-session —
// background sessions keep their own queues untouched.

// Shared compaction/prune flow used by both the global composer and the
// split-pane composer, so /compact behaves identically in every pane.
// `messagesEl` selects where the reloaded history lands: null reloads the
// shared #messages container (global composer); otherwise it reloads the
// given pane's messages element directly. The context ring update is
// routed to the right pane via updateContextProgress(sid).
// Shared compaction-completion handler: reload history (to render the new
// fold/summary) + show the success toast. Called by BOTH the manual
// /compact flow (runCompactFlow) and the auto-compact SSE event
// (context_compacted) — one code path, no duplication. A per-sid debounce
// guards against a double reload when a manual /compact also fires the
// server's context_compacted event.
const _compactAppliedAt: Record<string, number> = {};
function applyCompactionComplete(sid: string, successMsg: string): void {
  const now = Date.now();
  if (_compactAppliedAt[sid] && now - _compactAppliedAt[sid] < 1500) return;
  _compactAppliedAt[sid] = now;
  const messagesEl = sessionMessagesEl(sid) || $("messages");
  loadHistoryInto(sid, messagesEl);
  refreshSessionPreview(sid);
  setCompactToastState("success", successMsg, sid);
  setTimeout(() => hideCompactToast(), 3000);
  // Re-render the pending-message bar: loadHistoryInto can re-mount the
  // composer, leaving the pending bar empty even though the queue is still
  // in state — queued messages appeared to vanish right after auto-compact.
  renderComposerPending(sid);
}

async function runCompactFlow(sid: string, isPrune: boolean, messagesEl: HTMLElement | null): Promise<void> {
  const loadingMsg = isPrune ? i18n.t("toast.pruning") : i18n.t("toast.compacting");
  const successMsg = isPrune ? i18n.t("toast.toolOutputsPruned") : i18n.t("toast.compactedSuccess");

  ensureCompactToast();
  setCompactToastState("loading", loadingMsg, sid);

  const startTime = Date.now();
  try {
    const result = isPrune ? await api.pruneSession(sid) : await api.compactSession(sid);

    const minMs = isPrune ? 300 : 600;
    const elapsed = Date.now() - startTime;
    if (elapsed < minMs) await new Promise(r => setTimeout(r, minMs - elapsed));

    if (result.last_usage?.prompt_tokens !== undefined) {
      const contextWindow = store.get().config.contextWindow || 200000;
      const pct = Math.min(result.last_usage.prompt_tokens / contextWindow, 1);
      updateContextProgress(pct, result.last_usage.prompt_tokens, sid);
    }

    if (isPrune) {
      // Prune has no auto-compact equivalent — reload + toast here.
      if (messagesEl) await loadHistoryInto(sid, messagesEl); else await loadHistory(sid);
      refreshSessionPreview(sid);
      setCompactToastState("success", successMsg, sid);
      setTimeout(() => hideCompactToast(), 3000);
    } else {
      // Compact: share the completion path with auto-compact's
      // context_compacted event (single source of truth for the reload
      // + success toast). The debounce absorbs the server's echo.
      const noop = !!(result as any).noop;
      applyCompactionComplete(sid, noop ? i18n.t("toast.nothingToCompact") : successMsg);
    }
  } catch (e: any) {
    setCompactToastState("error", isPrune ? i18n.t("toast.pruneFailed", { err: e?.message || e }) : i18n.t("toast.compactionFailed", { err: e?.message || e }), sid);
    setTimeout(() => hideCompactToast(), 3000);
  }
}

function renderPendingBar() { renderComposerPending(store.get().activeSid || ""); }

// ---------------------------------------------------------------------------
// Unified composer BEHAVIOR layer (sid-parameterized). One implementation
// shared by the full-screen composer and every split pane — there is no
// active/background distinction here. The full-screen and pane composers
// both mount composerTemplate and are driven entirely by these functions.
// ---------------------------------------------------------------------------

// Toggle a session's send button between send (→) and stop (■).
function setComposerRunning(sid: string, running: boolean) {
  const btn = composerSendBtn(sid);
  if (!btn) return;
  if (running) {
    btn.textContent = "■";
    btn.className = "pane-send stop-btn";
    btn.title = i18n.t("composer.stop");
  } else {
    btn.textContent = "→";
    btn.className = "pane-send";
    btn.title = i18n.t("composer.send");
  }
}

// Render this session's draft image previews into its own .pane-previews.
function renderComposerPreviews(sid: string) {
  const wrap = composerPreviewsEl(sid);
  if (!wrap) return;
  const imgs = draftImages(sid);
  wrap.replaceChildren();
  if (imgs.length === 0) { wrap.style.display = "none"; return; }
  wrap.style.display = "flex";
  imgs.forEach((img, i) => {
    const item = document.createElement("div");
    item.className = "image-preview-item";
    const isImage = (img.mime || "").startsWith("image/");
    if (isImage) {
      const im = document.createElement("img");
      // Same blob-vs-disk strategy as the queued bar: live session uses
      // the instant blob URL, post-refresh falls back to /attachments.
      // state.ts also strips blob: URLs from localStorage on serialize,
      // so img.thumbUrl is typically empty after a reload.
      const fallbackSrc = attachmentUrl(img.path);
      im.src = img.thumbUrl || fallbackSrc;
      im.alt = img.name;
      im.addEventListener("error", () => {
        if (im.src === fallbackSrc) return;
        im.src = fallbackSrc;
      }, { once: true });
      item.appendChild(im);
    } else {
      // Non-image attachment (pdf / video / archive / …): render a filename
      // chip with a generic file icon. No <img> — a broken image for a type
      // the browser can't render is worse than a label.
      const chip = document.createElement("div");
      chip.className = "file-preview-chip";
      chip.title = img.name;
      chip.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span class="file-preview-name">${esc(img.name)}</span>`;
      item.appendChild(chip);
    }
    const rm = document.createElement("button");
    rm.className = "image-preview-remove";
    rm.title = i18n.t("composer.removePreview");
    rm.textContent = "×";
    rm.onclick = () => {
      const removed = imgs[i];
      if (removed?.thumbUrl) {
        abortImageUpload(removed.thumbUrl);
        URL.revokeObjectURL(removed.thumbUrl);
      }
      setDraftImages(sid, imgs.filter((_, j) => j !== i));
      renderComposerPreviews(sid);
    };
    item.appendChild(rm);
    wrap.appendChild(item);
  });
}

// Render this session's "排队中" (queued) bar into its own .pane-pending.
function renderComposerPending(sid: string) {
  const bar = composerPendingEl(sid);
  if (!bar) return;
  const queue = getPendingQueue(sid);
  // Clear the entire pending bar content
  bar.innerHTML = "";
  if (queue.length === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  // Render a label + container for each item
  const label = document.createElement("div");
  label.className = "pending-bar-label";
  label.textContent = i18n.t("composer.queued");
  bar.appendChild(label);
  queue.forEach((item) => {
    const itemEl = document.createElement("div");
    itemEl.className = "pending-bar-item";
    itemEl.setAttribute("data-pending-id", item.id);
    // Images (if any)
    if (item.images && item.images.length > 0) {
      const thumbContainer = document.createElement("span");
      thumbContainer.className = "pending-bar-images";
      item.images.forEach(img => {
        if ((img.mime || "").startsWith("image/")) {
          const im = document.createElement("img");
          // Prefer the session-local blob URL (instant preview during a
          // live session); fall back to the on-disk attachment on refresh
          // — the blob URL is page-local (URL.createObjectURL) and dies
          // when the page reloads, while path is durable server-side and
          // served via GET /attachments. Same fallback applies to draft
          // previews below.
          const fallbackSrc = attachmentUrl(img.path);
          im.src = img.thumbUrl || fallbackSrc;
          im.alt = img.name;
          im.className = "pending-bar-thumb";
          im.setAttribute("data-full-src", fallbackSrc);
          im.addEventListener("error", () => {
            if (im.src === fallbackSrc) return;
            im.src = fallbackSrc;
          }, { once: true });
          thumbContainer.appendChild(im);
        } else {
          const chip = document.createElement("span");
          chip.className = "pending-bar-file";
          chip.title = img.name;
          chip.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7;vertical-align:-1px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> ${esc(img.name)}`;
          thumbContainer.appendChild(chip);
        }
      });
      itemEl.appendChild(thumbContainer);
    }
    const textPreview = document.createElement("span");
    textPreview.className = "pending-bar-text";
    const preview = item.text.length > 80 ? item.text.slice(0, 80) + "…" : item.text;
    textPreview.textContent = preview;
    textPreview.title = item.text;
    itemEl.appendChild(textPreview);
    // Edit button
    const editBtn = document.createElement("button");
    editBtn.className = "pending-bar-edit";
    editBtn.textContent = i18n.t("common.edit");
    editBtn.onclick = () => editComposerPending(sid, item.id);
    itemEl.appendChild(editBtn);
    // Remove button
    const rmBtn = document.createElement("button");
    rmBtn.className = "pending-bar-clear";
    rmBtn.textContent = "✕";
    rmBtn.onclick = () => {
      removePendingItem(sid, item.id);
      renderComposerPending(sid);
    };
    itemEl.appendChild(rmBtn);
    bar.appendChild(itemEl);
  });
}

// The single send path. Handles slash commands, optimistic render into the
// session's own messages container, typing indicator, attachments.
async function sendComposerMessage(sid: string) {
  const textarea = composerTextarea(sid);
  if (!textarea) return;
  const text = textarea.value.trim();
  const imgs = draftImages(sid);
  if (!text && imgs.length === 0) return;

  const trimmedCmd = text.trim();
  const isCommand = trimmedCmd.startsWith("/");

  // Hide prompt immediately for responsiveness. CRITICAL: also clear the
  // persisted draft text — otherwise a remount (e.g. entering split mode)
  // rehydrates the textarea with the just-sent text via hydrateComposer.
  textarea.value = "";
  textarea.style.height = "auto";
  setDraftText(sid, "");
  const cc = composerCharCount(sid);
  if (cc) cc.textContent = "";

  const messagesEl = sessionMessagesEl(sid);
  let optimisticEl: HTMLElement | null = null;

  try {
    // Slash commands operate on THIS session.
    if (trimmedCmd === "/new") {
      await createSession();
      appendSystem(SPARK_ICON_SVG, "New conversation");
      return;
    }
    if (trimmedCmd === "/stop") {
      // Mirrors the input-box stop button: posts /cancel and clears UI state.
      cancelComposerTurn(sid);
      appendSystem(STOP_ICON_SVG, "Stopped current turn");
      return;
    }
    if (trimmedCmd === "/model" || trimmedCmd.startsWith("/model ")) {
      // No-arg: render the model picker card. With arg: switch session model.
      const arg = trimmedCmd === "/model" ? "" : trimmedCmd.slice("/model ".length).trim();
      const { config, sessions } = store.get();
      const s = sessions.find(x => x.id === sid);
      const models = (config as any).modelDetails
        || ((config as any).model?.available || []).map((m: string) => ({ name: m }));
      if (!arg) {
        const currentModel = (s as any)?.model_name || (config as any).model;
        const currentProvider = (s as any)?.provider_name || "";
        appendModelPicker(currentModel, currentProvider, models, sid, messagesEl || undefined);
      } else {
        try {
          // Accept "provider:model" (exact) or a bare model name (resolve
          // provider from modelDetails so the composer dropdown lands on the
          // right "provider|model" option instead of going blank).
          let provider_name: string | undefined;
          let model_name = arg;
          if (arg.includes(":")) {
            const idx = arg.indexOf(":");
            provider_name = arg.slice(0, idx).trim();
            model_name = arg.slice(idx + 1).trim();
          } else {
            const found = (models as any[]).find((m) => m.name === arg);
            provider_name = found?.provider;
          }
          await api.updateSession(sid, { model_name, ...(provider_name ? { provider_name } : {}) });
          const ss = store.get().sessions.find(x => x.id === sid);
          if (ss) { (ss as any).model_name = model_name; (ss as any).provider_name = provider_name ?? null; }
          // Rebuild the dropdown — option values are "provider|model" now, so
          // assigning a bare model name leaves the select empty.
          hydrateComposer(sid);
          appendSystem(CHECK_ICON_SVG, i18n.t("system.switchedModel"), provider_name ? `${provider_name}:${model_name}` : model_name);
        } catch (err: any) {
          appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
          console.error("updateSession(model_name) failed:", err);
        }
      }
      return;
    }
    if (trimmedCmd === "/effort" || trimmedCmd.startsWith("/effort ")) {
      const arg = trimmedCmd === "/effort" ? "" : trimmedCmd.slice("/effort ".length).trim();
      const { config, sessions } = store.get();
      const s = sessions.find(x => x.id === sid);
      const cur = (s as any)?.thinking_mode || (config as any).model?.thinking_mode || "disabled";
      // Resolve the current model's supported effort levels (server reports
      // the resolved list — capped set, full default, or [] for non-thinking).
      const models = (config as any).modelDetails || [];
      const curModel = (s as any)?.model_name || (config as any).model?.name || "";
      const curProvider = (s as any)?.provider_name || "";
      const mEntry = (models as any[]).find((m) => m.name === curModel && (!curProvider || m.provider === curProvider))
                    || (models as any[]).find((m) => m.name === curModel);
      const levels: string[] = Array.isArray(mEntry?.effort_levels) ? mEntry.effort_levels : [];
      if (!arg) {
        appendEffortPicker(cur, levels, sid, messagesEl || undefined);
      } else {
        const allowed = ["disabled", ...levels];
        if (!allowed.includes(arg)) {
          appendError(`Unknown effort '${arg}'. Options: ${allowed.join("/")}`);
          return;
        }
        try {
          await api.updateSession(sid, { thinking_mode: arg });
          if (s) (s as any).thinking_mode = arg;
          hydrateComposer(sid);
          appendSystem(CHECK_ICON_SVG, `Effort → ${arg === "disabled" ? "off" : arg}`);
        } catch (err: any) {
          appendError(i18n.t("toast.modelSwitchFailed", { err: err?.message || err }));
        }
      }
      return;
    }
    if (trimmedCmd === "/compact" || trimmedCmd === "/prune") {
      await runCompactFlow(sid, trimmedCmd === "/prune", messagesEl);
      return;
    }
    if (trimmedCmd.startsWith("/automation ")) {
      const prompt = trimmedCmd.slice("/automation ".length).trim();
      const name = (prompt.slice(0, 30) + (prompt.length > 30 ? "..." : "")) || "Chat task";
      ensureCompactToast();
      setCompactToastState("loading", i18n.t("toast.creatingAutomation"), sid);
      await api.createAutomation(name, prompt, { kind: "daily", time: "09:00" });
      setCompactToastState("success", i18n.t("toast.automationCreated", { name }), sid);
      setTimeout(() => hideCompactToast(), 3000);
      return;
    }
    if (trimmedCmd === "/restart") {
      // Tell the running Ziva desktop to relaunch — same UX as the IM
      // bridge's /restart slash command and the macOS top-bar menu item.
      // Goes through the `restart-ziva` IPC (see electron/preload.ts),
      // which the main process resolves via restartApp() →
      // app.relaunch() + app.quit().
      if (!window.electronAPI?.restartZiva) {
        appendError("Restart is only available in the desktop app.");
        return;
      }
      try {
        await window.electronAPI.restartZiva();
        appendSystem(CHECK_ICON_SVG, "Restart scheduled; new process will send confirmation.");
      } catch (err: any) {
        appendError(`Restart failed: ${err?.message || err}`);
      }
      return;
    }

    if (!isCommand) {
      setSessionRunning(sid, true);
      setComposerRunning(sid, true);
    }

    const parts: unknown[] = [];
    // Images travel as image_url blocks (the runtime expands the path to a
    // data URL for vision models). Non-image attachments (pdf / video /
    // archive) can't go in image_url — surface them as a path note in the
    // text so the model knows they exist and can read_file them. Mirrors
    // how the IM bridge surfaces inbound files to the model.
    const fileNotes: string[] = [];
    for (const img of imgs) {
      if ((img.mime || "").startsWith("image/")) {
        parts.push({ type: "image_url", image_url: { url: img.path } });
      } else {
        fileNotes.push(`[Uploaded file: ${img.name}, saved to ${img.path}]`);
      }
    }
    const fullText = fileNotes.length
      ? (text ? `${text}\n${fileNotes.join("\n")}` : fileNotes.join("\n"))
      : text;
    if (fullText) parts.unshift({ type: "text", text: fullText });
    if (messagesEl) optimisticEl = appendUserMsg(parts, messagesEl);
    setDraftImages(sid, []);
    renderComposerPreviews(sid);
    if (messagesEl) appendTyping(messagesEl);
    scrollSessionBottom(sid);
    await api.createTurn(sid, parts);
    // Success: the images are now part of history; release their thumbs.
    disposePendingImageThumbs(imgs);
  } catch (e: any) {
    if (!isCommand) {
      setSessionRunning(sid, false);
      setComposerRunning(sid, false);
    }
    if (messagesEl) removeTyping(messagesEl);
    if (optimisticEl) optimisticEl.remove();
    // Restore the unsent text + images so the user can retry.
    textarea.value = text;
    setDraftImages(sid, imgs);
    renderComposerPreviews(sid);
    console.error("send failed:", e);
  }
}

// Codex-style queue: while a turn is running, stash the typed message
// (text + images) on the pending entry for THIS session, flush on turn_end.
function queueComposerMessage(sid: string) {
  const textarea = composerTextarea(sid);
  if (!textarea) return;
  const text = textarea.value;
  const trimmed = text.trim();
  const imgs = draftImages(sid);
  if (!trimmed && imgs.length === 0) return;
  enqueuePending(sid, text || "", 0, imgs.length > 0 ? imgs : undefined);
  textarea.value = "";
  textarea.style.height = "auto";
  // Mirror sendComposerMessage: also clear the persisted draft so any later
  // hydrateComposer (e.g. after a session switch or remount) doesn't
  // re-populate the textarea with the just-queued text.
  setDraftText(sid, "");
  const cc = composerCharCount(sid);
  if (cc) cc.textContent = "";
  setDraftImages(sid, []);
  renderComposerPreviews(sid);
  renderComposerPending(sid);
}

// Send a previously-queued message for a session (called by flushComposerQueue).
async function sendComposerFromQueue(sid: string, text: string, images: PendingAttachment[], initialRetries: number = 0, itemId?: string) {
  if (!text && images.length === 0) return;
  const messagesEl = sessionMessagesEl(sid);
  setSessionRunning(sid, true);
  setComposerRunning(sid, true);
  let optimisticEl: HTMLElement | null = null;
  try {
    const parts: unknown[] = [];
    const fileNotes: string[] = [];
    for (const img of images) {
      if ((img.mime || "").startsWith("image/")) {
        parts.push({ type: "image_url", image_url: { url: img.path } });
      } else {
        fileNotes.push(`[Uploaded file: ${img.name}, saved to ${img.path}]`);
      }
    }
    const fullText = fileNotes.length
      ? (text ? `${text}\n${fileNotes.join("\n")}` : fileNotes.join("\n"))
      : text;
    if (fullText) parts.unshift({ type: "text", text: fullText });
    if (messagesEl) optimisticEl = appendUserMsg(parts, messagesEl);
    if (messagesEl) appendTyping(messagesEl);
    scrollSessionBottom(sid);
    await api.createTurn(sid, parts);
    disposePendingImageThumbs(images);
  } catch (e: any) {
    setSessionRunning(sid, false);
    setComposerRunning(sid, false);
    if (messagesEl) removeTyping(messagesEl);
    if (optimisticEl) optimisticEl.remove();
    const newRetries = initialRetries + 1;
    if (newRetries >= MAX_QUEUE_RETRIES) {
      // Permanently failed - don't re-enqueue
      disposePendingImageThumbs(images);
      appendError(i18n.t("toast.queueFailed", { n: MAX_QUEUE_RETRIES, err: e?.message || e }), messagesEl || undefined);
      console.error(`Queued message exceeded max retries (${MAX_QUEUE_RETRIES}):`, e);
      return;
    }
    // Re-enqueue with incremented retry count
    const restoredId = itemId || generatePendingId();
    enqueuePending(sid, text, newRetries, images.length > 0 ? images : undefined);
    // Update the restored item's ID to match the original if provided
    if (itemId) {
      const queue = getPendingQueue(sid);
      const lastIdx = queue.length - 1;
      if (lastIdx >= 0 && queue[lastIdx].id !== itemId) {
        updatePendingItem(sid, queue[lastIdx].id, { id: itemId });
      }
    }
    renderComposerPending(sid);
    console.error("Queued message send failed (will retry):", e);
    // Re-flush shortly: the failed createTurn left this item in the queue
    // with no running turn to fire a turn_end, so without a retry it would
    // sit forever. 500ms lets the server settle after the failed attempt.
    setTimeout(() => { flushComposerQueue(sid, 500); }, 500);
  }
}

// Flush a session's queued message after its turn closes. No active-session
// guard needed: the queue is per-sid, so flushing session X always lands in
// session X regardless of what is currently active.
// Per-sid flush lock: turn_end / cancel / retry can all fire
// flushComposerQueue concurrently; without a lock two flushes race two
// createTurns into a 429 turn_already_running.
const _flushingQueue: Record<string, boolean> = {};
function flushComposerQueue(sid: string, delayMs: number) {
  const queue = getPendingQueue(sid);
  if (queue.length === 0) return;
  if (_flushingQueue[sid]) return;  // a flush for this session is already in flight
  _flushingQueue[sid] = true;
  
  // Pop only the first item in the queue (FIFO)
  const item = queue[0];
  if (!item) { _flushingQueue[sid] = false; return; }
  
  // Remove the first item from the queue, leave the rest
  removePendingItem(sid, item.id);
  renderComposerPending(sid);
  
  const combinedText = item.text || "";
  const combinedImages = item.images || [];
  const maxRetries = item.retries || 0;


  setTimeout(async () => {
    try {
      await sendComposerFromQueue(sid, combinedText, combinedImages, maxRetries, item.id);
    } finally {
      _flushingQueue[sid] = false;
    }
  }, delayMs);
}

function cancelComposerTurn(sid: string) {
  if (!sid) return;
  api.cancelTurn(sid).catch(() => { /* ignore */ });
  markSidCancelled(sid);
  setSessionRunning(sid, false);
  setComposerRunning(sid, false);
  // Invalidate the streamCtx for THIS sid, not liveStreamSid — the latter
  // is null outside an event handler so cancel arrives with no live stream
  // and invalidateLiveStreamEl would silently no-op. Next turn creates a
  // fresh assistant element instead of appending into the cancelled bubble.
  invalidateStreamCtx(sid);
  const messagesEl = sessionMessagesEl(sid);
  if (messagesEl) removeTyping(messagesEl);
  renderSessions();

  // Do NOT flush the queue here. Flushing immediately races the cancel
  // HTTP with createTurn, producing two cancelled shells (the queued
  // createTurn wins the 429 retry, gets cancelled by the in-flight
  // cancel, then the original turn's belated turn_cancelled arrives
  // and gets visualised as a second "cancelled" card). Instead, the SSE
  // `turn_cancelled` handler (handleSessionEvent → flushComposerQueue
  // with 200ms delay) flushes once the server confirms cancellation.
}

function clearComposerPending(sid: string) {
  // Clear all pending items for this session
  const queue = getPendingQueue(sid);
  queue.forEach(item => {
    if (item.images) disposePendingImageThumbs(item.images);
  });
  clearAllPending(sid);
  renderComposerPending(sid);
}

function editComposerPending(sid: string, itemId?: string) {
  // Pull the queued item back into the composer textarea for editing.
  // With an itemId we target a specific entry; without one we edit
  // the head of the queue (the next message that would flush).
  const queue = getPendingQueue(sid);
  if (queue.length === 0) return;
  const targetId = itemId || queue[0].id;
  const item = queue.find(i => i.id === targetId);
  if (!item) return;
  // Pull back into composer
  const ta = composerTextarea(sid);
  if (ta) {
    ta.value = item.text;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  }
  const cc = composerCharCount(sid);
  if (cc && ta) cc.textContent = String(ta.value.length);
  // Restore images to draft
  if (item.images && item.images.length > 0) {
    setDraftImages(sid, item.images);
    renderComposerPreviews(sid);
  }
  // Remove from queue
  removePendingItem(sid, targetId);
  renderComposerPending(sid);
  // Sync the persisted draft with the textarea so a session switch /
  // remount between "click edit" and the next keystroke doesn't lose the
  // pulled-back text (the input event handler hasn't fired yet to keep
  // promptDrafts in sync on its own).
  setDraftText(sid, item.text);
  const imgs = queuedImages(sid);
  if (imgs.length > 0) {
    setDraftImages(sid, [...draftImages(sid), ...imgs]);
    renderComposerPreviews(sid);
  }
  setSessionPending(sid, null);
  renderComposerPending(sid);
  if (ta) ta.focus();
}

// ---- Slash menu (sid-aware; one menu visible at a time — the focused one) ----
function showSlashMenuFor(sid: string, text: string) {
  const menu = composerSlashEl(sid);
  if (!menu) return;
  const q = text.startsWith("/") ? text.slice(1).toLowerCase() : "";
  const matches = SLASH_COMMANDS.filter(c => !q || c.name.slice(1).toLowerCase().includes(q));
  if (matches.length === 0) { menu.style.display = "none"; return; }
  slashMenuSid = sid;
  slashMenuIndex = 0;
  menu.replaceChildren();
  matches.forEach(c => {
    const item = document.createElement("div");
    item.className = "slash-item";
    item.dataset.cmd = c.name;
    const name = document.createElement("span");
    name.className = "slash-name"; name.textContent = c.name;
    const desc = document.createElement("span");
    desc.className = "slash-desc"; desc.textContent = c.description || "";
    item.appendChild(name); item.appendChild(desc);
    menu.appendChild(item);
  });
  menu.querySelector(".slash-item")?.classList.add("active");
  menu.style.display = "block";
}
function hideSlashMenuFor(sid: string) {
  const menu = composerSlashEl(sid);
  if (menu) menu.style.display = "none";
  if (slashMenuSid === sid) slashMenuIndex = -1;
}
function moveSlashSelectionFor(sid: string, dir: number) {
  const menu = composerSlashEl(sid);
  if (!menu || menu.style.display === "none") return;
  const items = menu.querySelectorAll(".slash-item");
  if (items.length === 0) return;
  items[slashMenuIndex]?.classList.remove("active");
  slashMenuIndex = (slashMenuIndex + dir + items.length) % items.length;
  items[slashMenuIndex]?.classList.add("active");
}
function selectSlashCommandFor(sid: string) {
  const menu = composerSlashEl(sid);
  if (!menu) return;
  const items = menu.querySelectorAll(".slash-item");
  const item = items[slashMenuIndex] as HTMLElement | undefined;
  if (item) insertSlashCommandFor(sid, item.dataset.cmd || "");
}
function insertSlashCommandFor(sid: string, cmd: string) {
  const ta = composerTextarea(sid);
  if (ta) { ta.value = cmd + " "; ta.focus(); }
  hideSlashMenuFor(sid);
  // Auto-send no-argument commands like /compact.
  if (cmd === "/compact" || cmd === "/prune") sendComposerMessage(sid);
}

// ---- Mount / hydrate / reconcile ----
// Populate a mounted composer from per-session state (model, approval,
// draft text + images, char count, running state). Idempotent + cheap.
// Size a <select> to its currently-selected text instead of the widest
// option. Native selects size to the longest <option>, so a short model
// name next to a long one renders far wider than its own label. We clone
// the select with only the selected option and measure its offsetWidth —
// that captures the native dropdown arrow + padding exactly, so there's no
// guessed-in extra gap on the right.
function fitSelectWidth(sel: HTMLSelectElement | null) {
  if (!sel) return;
  const opt = sel.selectedOptions[0] || sel.options[0];
  if (!opt) { sel.style.width = ""; return; }
  const probe = sel.cloneNode(false) as HTMLSelectElement;
  const o = document.createElement("option");
  o.textContent = opt.textContent || opt.value || "";
  probe.appendChild(o);
  // cloneNode copies the inline style (including a prior width), so reset.
  probe.style.cssText = "position:absolute;visibility:hidden;width:auto;";
  (sel.parentElement || document.body).appendChild(probe);
  const w = probe.offsetWidth;
  probe.remove();
  // A hidden ancestor (collapsed pane, not-yet-laid-out webview) measures
  // 0 — writing that would freeze the select at "0px" forever. Skip and
  // let the refit listeners below re-measure once it becomes visible.
  if (!w) { sel.style.width = ""; return; }
  sel.style.width = w + "px";
}

// Re-measure after layout-affecting changes: Android WebView hydrates
// before fonts/layout settle, so widths measured once at startup go stale
// (chips stuck ellipsized on a wide tablet). Cheap: probe per select.
function refitComposerSelects() {
  document.querySelectorAll(".composer-toolbar select").forEach((s) => fitSelectWidth(s as HTMLSelectElement));
}
if (typeof window !== "undefined") {
  window.addEventListener("resize", refitComposerSelects);
  window.addEventListener("orientationchange", refitComposerSelects);
  try { (document as any).fonts?.ready.then(() => refitComposerSelects()); } catch { /* older webview */ }
}

function hydrateComposer(sid: string) {
  const modelSel = composerModelSelect(sid);
  const approvalSel = composerApprovalSelect(sid);
  const { config, sessions } = store.get();
  const s = sessions.find(x => x.id === sid);
  const models = (config as any).modelDetails || ((config as any).model?.available || []).map((m: string) => ({ name: m }));
  if (modelSel) {
    const currentModel = (s as any)?.model_name || config.model;
    const currentProvider = (s as any)?.provider_name || "";
    modelSel.replaceChildren();
    // Group by provider so same-named models across providers (e.g. glm-5.2
    // under glm + opencode) each get their own row. option.value encodes
    // "provider|model"; the change handler parses it back. Falls back to a
    // flat list when the backend gives no provider attribution.
    if (models.some((m: any) => m.provider)) {
      const byProvider = new Map<string, any[]>();
      models.forEach((m: any) => {
        const p = m.provider || "(default)";
        if (!byProvider.has(p)) byProvider.set(p, []);
        byProvider.get(p)!.push(m);
      });
      [...byProvider.keys()].sort().forEach((p) => {
        const og = document.createElement("optgroup");
        og.label = p;
        byProvider.get(p)!.forEach((m: any) => {
          const opt = document.createElement("option");
          opt.value = `${p}|${m.name}`;
          opt.textContent = m.name;
          if (m.name === currentModel && (p === currentProvider || !currentProvider)) opt.selected = true;
          og.appendChild(opt);
        });
        modelSel.appendChild(og);
      });
    } else {
      models.forEach((m: any) => {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.name;
        if (m.name === currentModel) opt.selected = true;
        modelSel.appendChild(opt);
      });
    }
  }
  const effortSel = document.querySelector(`.pane-effort[data-sid="${sid}"]`) as HTMLSelectElement | null;
  if (effortSel) {
    // Rebuild from the selected model's effort_levels so the dropdown tracks
    // the model: hidden for non-thinking models; downgrades the current level
    // if the new model doesn't support it (max→xhigh→high→...).
    const curModel = (s as any)?.model_name || (config as any).model?.name || "";
    const curProvider = (s as any)?.provider_name || "";
    const mEntry = (models as any[]).find((m) => m.name === curModel && (!curProvider || m.provider === curProvider))
                  || (models as any[]).find((m) => m.name === curModel);
    const levels: string[] = Array.isArray(mEntry?.effort_levels) ? mEntry.effort_levels : [];
    const storedMode = (s as any)?.thinking_mode as string | undefined;
    // A new session has no thinking_mode yet — default to the model's highest
    // supported level (not "off"), so the composer reflects the model's
    // capability. The store's config.model is just the model *name* (a
    // string), so there's no global thinking_mode to fall back to here; the
    // backend's own global default covers sessions created outside the UI.
    const wanted = storedMode || (levels.length ? levels[levels.length - 1] : "disabled");
    effortSel.replaceChildren();
    effortSel.style.display = levels.length ? "" : "none";
    if (levels.length) {
      const ORDER = ["low", "medium", "high", "xhigh", "max"];
      // Keep `wanted` if the model supports it; else step down to the highest
      // supported level at or below it; else disabled.
      let pick = levels.includes(wanted) ? wanted : "disabled";
      if (pick === "disabled" && wanted !== "disabled") {
        for (let i = ORDER.indexOf(wanted); i >= 0; i--) {
          if (levels.includes(ORDER[i])) { pick = ORDER[i]; break; }
        }
      }
      ["disabled", ...levels].forEach((lv) => {
        const o = document.createElement("option");
        o.value = lv;
        o.textContent = lv === "disabled" ? "off" : lv;
        if (lv === pick) o.selected = true;
        effortSel.appendChild(o);
      });
      // Persist so UI and backend agree: both the new-session default
      // (storedMode was unset → pick = model max) and any downgrade.
      if (s && pick !== "disabled" && storedMode !== pick) {
        (s as any).thinking_mode = pick;
        api.updateSession(sid, { thinking_mode: pick }).catch(() => {});
      }
    }
  }
  if (approvalSel) {
    approvalSel.value = (s as any)?.approval_policy || config.approval || "full-auto";
  }
  const ta = composerTextarea(sid);
  if (ta) {
    ta.value = draftText(sid);
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
    if (ta.value.startsWith("/")) showSlashMenuFor(sid, ta.value); else hideSlashMenuFor(sid);
  }
  const cc = composerCharCount(sid);
  if (cc) cc.textContent = ta && ta.value.length > 0 ? String(ta.value.length) : "";
  // Restore the context ring from the session's cached last usage. Without
  // this the ring stays empty because loadHistoryInto runs before the
  // composer is mounted.
  const lu = (s as any)?.lastUsage as { prompt_tokens?: number } | undefined;
  if (lu?.prompt_tokens !== undefined) {
    const contextWindow = config.contextWindow || 200000;
    updateContextProgress(Math.min(lu.prompt_tokens / contextWindow, 1), lu.prompt_tokens, sid);
  } else {
    updateContextProgress(0, 0, sid);
  }
  setComposerRunning(sid, !!store.get().runningSessions[sid]);
  renderComposerPreviews(sid);
  renderComposerPending(sid);
  // Size the model + effort selects to their current labels so short names
  // don't stretch to the width of the longest option in the list.
   fitSelectWidth(modelSel);
   fitSelectWidth(approvalSel);
   fitSelectWidth(document.querySelector(`.pane-effort[data-sid="${sid}"]`) as HTMLSelectElement | null);
}

// Mount the unified composer template into `host` for `sid`. Re-mounts only
// when the sid changes (so rapid re-renders don't blow away focus/state).
function mountComposer(sid: string, host: HTMLElement) {
  if (host.dataset.sid === sid && host.childElementCount > 0) {
    hydrateComposer(sid);
    return;
  }
  host.dataset.sid = sid;
  host.replaceChildren();
  host.insertAdjacentHTML("beforeend", composerTemplate(sid));
  hydrateComposer(sid);
}

// Reconcile mounted composers with the current session/split state. Called
// from switchSession / renderSplitPanes / refreshConfig. Full-screen host
// (#composerHost) is mounted only when not in split mode; pane hosts are
// mounted inside renderSplitPanes.
function renderComposers() {
  const { activeSid, splitSessions } = store.get();
  const host = $("composerHost");
  if (!host) return;
  if (splitSessions.length === 0 && activeSid) {
    mountComposer(activeSid, host);
  } else {
    // Split mode: #composerHost is hidden. Clear any stale composer so it
    // can't shadow the active pane's composer (two elements with the same
    // data-sid would make querySelector-based helpers hit the wrong one).
    host.replaceChildren();
    host.dataset.sid = "";
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

// ---- Project Picker ----
async function refreshGitBranch(sid?: string) {
  // When called without args, prefer the active session (per-session call)
  // so the response reflects the session-scoped git state. When there's no
  // active session (e.g. immediately after switching workspace), fall back
  // to the workspace-level endpoint so the status-bar branch indicator
  // can be refreshed for the new workspace right away.
  const targetSid = sid || store.get().activeSid;
  try {
    const res = targetSid
      ? await api.getGitBranches(targetSid)
      : await api.getWorkspaceGitBranches();
    const gitBranchNameEl = $("gitBranchName") as HTMLElement;
    if (gitBranchNameEl) {
      gitBranchNameEl.textContent = res.current;
    }
  } catch (e) {
    console.error("Failed to fetch git branch", e);
  }
}

async function openGitBranchPicker(e?: Event | MouseEvent) {
  const target = e?.currentTarget as HTMLElement;
  // When a session is active, the per-session endpoint gives the same
  // result anyway (both read from runtime.workspace_root on the backend).
  // Falling back to the workspace-level endpoint keeps the picker
  // working right after switching workspace, when activeSid is null.
  const { activeSid } = store.get();
  let res: { current: string; branches: string[] };
  try {
    res = activeSid
      ? await api.getGitBranches(activeSid)
      : await api.getWorkspaceGitBranches();
  } catch (err: any) {
    console.error("Failed to load git branches:", err);
    alert(i18n.t("alert.gitBranchLoadFailed", { err: err.message || "unknown error" }));
    return;
  }
  const current = res.current;
  const branches = res.branches;

  document.querySelectorAll(".popup-menu").forEach((p) => p.remove());

  const popup = document.createElement("div");
  popup.className = "popup-menu";

  const rect = target.getBoundingClientRect();
  popup.style.bottom = `${window.innerHeight - rect.top + 8}px`;
  popup.style.left = `${rect.left}px`;

  const searchBox = document.createElement("div");
  searchBox.className = "popup-search-box";
  searchBox.innerHTML = `<input type="text" placeholder="Search or create branch..." />`;
  popup.appendChild(searchBox);

  const listDiv = document.createElement("div");
  listDiv.className = "popup-list";

  const renderBranches = (filter: string) => {
    listDiv.innerHTML = "";
    const filtered = branches.filter((b: string) => b.toLowerCase().includes(filter.toLowerCase()));
    filtered.forEach((b: string) => {
      const el = document.createElement("div");
      el.className = "popup-item";
      el.innerHTML = `<span class="popup-icon">${b === current ? '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>' : ""}</span><span class="popup-text">${esc(b)}</span>`;
      el.onclick = async () => {
        popup.remove();
        if (b !== current) {
          try {
            if (activeSid) {
              await api.gitCheckout(activeSid, b, false);
            } else {
              await api.gitCheckoutWorkspace(b, false);
            }
            await refreshGitBranch();
          } catch (err: any) {
            alert(i18n.t("alert.checkoutFailed", { err: err.message }));
          }
        }
      };
      listDiv.appendChild(el);
    });

    if (filter && !branches.includes(filter)) {
      const divider = document.createElement("div");
      divider.className = "popup-divider";
      listDiv.appendChild(divider);
      const createEl = document.createElement("div");
      createEl.className = "popup-item";
      createEl.innerHTML = `<span class="popup-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg></span><span class="popup-text">Create & checkout: <b>${esc(filter)}</b></span>`;
      createEl.onclick = async () => {
        popup.remove();
        try {
          if (activeSid) {
            await api.gitCheckout(activeSid, filter, true);
          } else {
            await api.gitCheckoutWorkspace(filter, true);
          }
          await refreshGitBranch();
        } catch (err: any) {
          alert(i18n.t("alert.createBranchFailed", { err: err.message }));
        }
      };
      listDiv.appendChild(createEl);
    }
  };

  renderBranches("");

  const input = searchBox.querySelector("input")!;
  input.oninput = (ev) => {
    renderBranches((ev.target as HTMLInputElement).value.trim());
  };

  popup.appendChild(listDiv);
  document.body.appendChild(popup);

  setTimeout(() => {
    const closer = (ev: MouseEvent) => {
      if (!popup.contains(ev.target as Node)) {
        popup.remove();
        document.removeEventListener("click", closer);
      }
    };
    document.addEventListener("click", closer);
    input.focus();
  }, 0);
}

async function openProjectPicker(e?: Event | MouseEvent) {
  const target = e?.currentTarget as HTMLElement;
  const { config } = store.get();
  const current = config.workspace || "";
  let recent: string[] = [];
  try {
    const res = await api.getRecentWorkspaces();
    recent = res.workspaces || [];
  } catch (err) {}

  document.querySelectorAll(".popup-menu").forEach((p) => p.remove());

  const popup = document.createElement("div");
  popup.className = "popup-menu";

  const rect = target.getBoundingClientRect();
  popup.style.bottom = `${window.innerHeight - rect.top + 8}px`;
  popup.style.left = `${rect.left}px`;

  const listDiv = document.createElement("div");
  listDiv.className = "popup-list";

  const currentName = current.split("/").pop() || "Project";
  listDiv.innerHTML = `<div class="popup-item" style="opacity:0.6; pointer-events:none;"><span class="popup-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></span><span class="popup-text" title="${esc(current)}">${esc(currentName)}</span></div>`;

  recent.filter((r) => r !== current).forEach((r) => {
    const el = document.createElement("div");
    el.className = "popup-item popup-item-with-action";
    const name = r.split("/").pop() || r;
    el.innerHTML = `
      <span class="popup-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-1.22-1.8A2 2 0 0 0 7.53 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg></span>
      <span class="popup-text" title="${esc(r)}">${esc(name)}</span>
      <span class="popup-action remove-workspace-btn" title="Remove from recent">&times;</span>`;
    el.onclick = async (ev) => {
      if ((ev.target as HTMLElement).classList.contains("remove-workspace-btn")) return;
      popup.remove();
      await openProjectInSidebar(r);
    };
    const removeBtn = el.querySelector(".remove-workspace-btn");
    if (removeBtn) {
      (removeBtn as HTMLElement).onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(i18n.t("confirm.removeRecent", { name }))) return;
        try {
          await api.removeWorkspace(r);
          popup.remove();
          await refreshSessions();
        } catch (e: any) {
          alert(i18n.t("alert.removeProjectFailed", { err: e?.message || "unknown" }));
        }
      };
    }
    listDiv.appendChild(el);
  });

  if (recent.length > 0) {
    const divider = document.createElement("div");
    divider.className = "popup-divider";
    listDiv.appendChild(divider);
  }

  const addBtn = document.createElement("div");
  addBtn.className = "popup-item";
  addBtn.innerHTML = `<span class="popup-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg></span><span class="popup-text">Add new project</span>`;
  addBtn.onclick = async () => {
    popup.remove();
    const res = await api.chooseSystemFolder();
    if (res.path) {
      await openProjectInSidebar(res.path);
    } else if (res.error && res.error !== "No folder selected") {
      alert(i18n.t("alert.chooseFolderFailed", { err: res.error }));
    }
  };
  listDiv.appendChild(addBtn);

  popup.appendChild(listDiv);
  document.body.appendChild(popup);

  setTimeout(() => {
    const closer = (ev: MouseEvent) => {
      if (!popup.contains(ev.target as Node)) {
        popup.remove();
        document.removeEventListener("click", closer);
      }
    };
    document.addEventListener("click", closer);
  }, 0);
}

/**
 * Switch the active project in the sidebar without reloading the page.
 * The server-side `Runtime.workspace_root` is updated in place so
 * subsequent /sessions/{sid}/... calls target the new project, and the
 * sidebar re-renders to move the active group to the top.
 */
async function openProjectInSidebar(
  workspace: string,
  opts: { thenSwitchTo?: string } = {},
): Promise<void> {
  const { config } = store.get();
  const current = config.workspace || "";
  if (workspace === current) {
    if (opts.thenSwitchTo) {
      await switchSession(opts.thenSwitchTo);
    }
    return;
  }
  // If the current active session is empty AND has no unsent draft text /
  // pending image attachments, delete it before switching workspaces so we
  // don't leave a stray "Empty session" behind in the old project. Sessions
  // the user has typed into but not sent (draft text or attached images)
  // must survive the workspace switch — otherwise the composer input is
  // silently lost on every project hop.
  const { activeSid, sessions, promptDrafts } = store.get();
  if (activeSid) {
    const draft = promptDrafts[activeSid];
    // An image that is still uploading lives in `inFlightUploads`, not
    // `promptDrafts` (it only lands in the draft on the upload-finish
    // callback). Without this check, attaching an image and switching
    // workspaces before the upload completes deletes the empty session and
    // aborts the in-flight upload, silently losing both.
    const hasInFlightUpload = inFlightUploads.some(u => u.sid === activeSid);
    const hasDraftInput =
      !!((draft?.text || "").trim()) ||
      (draft?.images?.length ?? 0) > 0 ||
      hasInFlightUpload;
    if (!hasDraftInput) {
      try {
        const msgData = await api.getMessages(activeSid);
        if ((msgData.messages || []).length === 0) {
          await api.deleteSession(activeSid);
          store.set({ sessions: sessions.filter(s => s.id !== activeSid) });
        }
      } catch {
        // Session not persisted yet — just drop it from local state.
        store.set({ sessions: sessions.filter(s => s.id !== activeSid) });
      }
    }
  }

  try {
    await api.switchWorkspace(workspace);
  } catch (err: any) {
    alert(i18n.t("alert.switchWorkspaceFailed", { err: err?.message || "unknown" }));
    return;
  }
  // Update local config and the status-bar label immediately so the user
  // sees the change even before /sessions returns.
  const wn = workspace.split("/").filter(Boolean).pop() || workspace;
  store.set({ config: { ...config, workspace } });
  const workspaceNameEl = $("workspaceName");
  if (workspaceNameEl) workspaceNameEl.textContent = wn;
  const contextWorkspaceEl = $("contextWorkspace");
  if (contextWorkspaceEl) contextWorkspaceEl.title = workspace;

  // Keep the local recent list in sync so the sidebar can show the new
  // project immediately even before the next refreshSessions() call.
  const recentWorkspaces = [workspace, ...store.get().recentWorkspaces.filter(w => w !== workspace)];
  store.set({ recentWorkspaces });

  // Reset the "show all" toggle since it's per-project.
  const list = $("sessionList");
  if (list) list.dataset.showAll = "false";

  // Reset the active session — the old activeSid belongs to the previous
  // project, and re-rendering with a stale active highlight would be wrong.
  // Also clear any split-screen panes since they reference the old workspace.
  store.set({ activeSid: null, splitSessions: [] });
  $("messages").innerHTML = "";
  renderSplitPanes();
  showEmptyState(true);

  await refreshSessions();
  // Refresh the git branch indicator so the status bar shows the new
  // workspace's current branch even before a session is selected.
  await refreshGitBranch();
  if (opts.thenSwitchTo) {
    await switchSession(opts.thenSwitchTo, { skipGitRefresh: true });
  } else {
    // Always start the new workspace on a fresh empty session. Previously
    // this jumped to sessions[0], but refreshSessions lists sessions across
    // ALL recent workspaces — so sessions[0] could belong to a different
    // project, leaking its conversation into the new workspace ("对话在两
    // 个项目下都显示") or showing non-empty history right after switching
    // ("切项目后显示非空白"). A new empty session belongs to the new
    // workspace and gives the composer an anchor.
    await createSession();
  }
}

export function closeSettingsModal() {
  document.getElementById("settingsModalBackdrop")?.remove();
}


// ---- Diff ----

// Tools that may mutate workspace files. When one of these finishes
// while the diff panel is open, we kick a debounced refresh so the
// user sees file changes live instead of waiting for turn_end.

// ---- MCP Status ----

// ---- Settings modal ----
// ---- Bootstrap ----
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
