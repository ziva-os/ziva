import "./styles/base.css";
import "./styles/theme-dark.css";
import "./styles/theme-light.css";
import "./styles/components.css";
import "./styles/browser-shell.css";
import "@xterm/xterm/css/xterm.css";
import * as api from "./api";
import { SSEPool } from "./sse";
import { renderMarkdown, addCopyButtons, highlightCode, extractThinking } from "./markdown";
import { Store, store } from "./state";
import type { AppState, PendingAttachment, RightPanelTab } from "./state";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import Prism from "prismjs";
import { $, esc, bindResizer } from "./dom";
import { openLinkInBrowser, initMessageLinkInterceptor } from "./links";
import { initBrowserShell, openInBrowserTab } from "./browser-shell";
import { closeAllFullpageOverlays } from "./modals";
import { openSkillsBrowser, closeSkillViewer } from "./modals/skills";
import { openSettingsModal, setSettingsDeps } from "./modals/settings";
import { openAutomationsModal, closeAutomationsModal, loadAutomationsIntoModal, setAutomationsDeps } from "./modals/automations";
import { formatRelativeTime } from "./format";
import { refreshStatus, refreshMCPStatus, updateConnStatus, setStatusDeps } from "./status";
import {   toggleRightPanel, initResizablePanel, updatePlanTabContent, scheduleDiffRefresh, refreshActiveReviewTabs, refreshActivePlanTab, ensurePlanSubscriber } from "./right-panel";
import {
  isActiveRunning, getActivePending, setActivePending, setActiveRunning,
  setSessionRunning, isSessionRunning, getSessionPending, setSessionPending,
  generatePendingId, enqueuePending, getPendingQueue, updatePendingItem,
  removePendingItem, clearAllPending,
  streamCtx, clearStreamCtx, invalidateLiveStreamEl,
  getLiveStreamSid, setLiveStreamSid, getLiveStreamTarget, setLiveStreamTarget,
  draftImages, setDraftImages, draftText, setDraftText,
  queuedImages,
  markSidCancelled, clearSidCancelled, isSidCancelling,
  setRuntimeStateDeps,
} from "./runtime-state";

// Strip <think>...</think> blocks (and any unclosed <think>…EOF) from
// automation outputs. The model's chain-of-thought is internal noise;
// the UI only shows the user-facing part. Runs before line-clamp
// previews so the visible lines are the real content.
function stripThinking(text: string): string {
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
  // In split mode we keep #messages visible and show a per-pane placeholder.
  const inSplit = !!center?.classList.contains("multi");
  if (!inSplit) {
    $("messages").style.display = show ? "none" : "block";
  } else {
    $("messages").style.display = "";
  }
}

function setPaneEmptyPlaceholder(target: HTMLElement) {
  target.innerHTML = `<div class="pane-empty-state"><svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><div>No messages yet</div></div>`;
}

function clearPaneEmptyPlaceholder(target: HTMLElement) {
  target.querySelectorAll(".pane-empty-state").forEach((el) => el.remove());
}

// ---- Right Panel Tab System ----


// ---- Relative time formatting ----

// ---- DOM Bootstrap — Ziva layout ----
function init() {
  // Inject main.ts dependencies into the extracted modal modules. Done here
  // (not at module top) because `store` is a `const` defined below — calling
  // these at module-load would hit the temporal dead zone and crash init.
  setSettingsDeps({ refreshConfig });
  setAutomationsDeps({ store, composerTextarea, formatRelativeTime });
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
          <span class="sidebar-title">Ziva</span>
          <button class="sidebar-toggle-btn" id="btnToggleSidebar" title="Toggle sidebar" aria-label="Toggle sidebar">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
        </div>
        <div class="sidebar-top">
          <button id="btnNewSession" class="sidebar-btn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            <span>新对话</span>
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
          <button class="sidebar-nav-item" id="btnSettings">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            <span>Settings</span>
          </button>
        </div>
      </aside>
      <main class="ziva-center">
        <div class="ziva-toolbar" id="zivaToolbar">
          <button class="toolbar-sidebar-open" id="btnOpenSidebar" title="Open sidebar" aria-label="Open sidebar">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
          <div class="toolbar-title" id="toolbarTitle"></div>
          <div class="toolbar-actions">
            <button class="toolbar-right-toggle" id="btnOpenRightPanel" title="Toggle panel" aria-label="Toggle panel">
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
            <div class="status-item" id="contextWorkspace" title="Switch workspace">
              <span class="status-icon"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-1.22-1.8A2 2 0 0 0 7.53 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg></span>
              <span id="workspaceName">ziva</span>
              <span class="status-chevron"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span>
            </div>
            <div class="status-item" id="gitBranchContext" title="Switch Git branch">
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
  }
  bindEvents();
  refreshStatus();
  refreshMCPStatus();
  refreshConfig();
  refreshSessions().then(() => {
    const s = store.get();
    // Only auto-select a session on initial load if the user hasn't already
    // picked one (e.g. by clicking "New Session" while refreshSessions was
    // still in flight). Otherwise we clobber the user's explicit choice and
    // the composer swaps to a different session unexpectedly.
    if (!s.activeSid) {
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
  { name: "/compact", description: "Compact context window" },
  { name: "/prune", description: "Prune tool outputs" },
  { name: "/automation", description: "Create a scheduled automation" },
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
      btn.title = "Voice input";
      const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || "audio/webm" });
      try {
        btn.title = "Transcribing…";
        const formData = new FormData();
        formData.append("audio", blob, "recording.webm");
        const res = await fetch("/api/stt", { method: "POST", body: formData });
        const data = await res.json();
        if (data.text) {
          const ta = composerTextarea(sid);
          if (ta) {
            ta.value = ta.value ? ta.value + "\n" + data.text : data.text;
            ta.dispatchEvent(new Event("input"));
            ta.focus();
          }
        }
      } catch (err: any) {
        console.error("STT failed:", err);
      } finally {
        btn.title = "Voice input";
      }
    };
    mediaRecorder.start();
    isRecording = true;
    btn.classList.add("recording");
    btn.title = "Stop recording";
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
      const sid = (target as HTMLSelectElement).dataset.sid || "";
      const model = (target as HTMLSelectElement).value;
      if (sid) {
        try { await api.updateSession(sid, { model_name: model }); } catch { /* ignore */ }
        const { sessions } = store.get();
        const s = sessions.find(x => x.id === sid);
        if (s) (s as any).model_name = model;
      }
      return;
    }
    if (target.classList.contains("pane-approval")) {
      const sid = (target as HTMLSelectElement).dataset.sid || "";
      const policy = (target as HTMLSelectElement).value;
      if (sid) {
        try { await api.updateSession(sid, { approval_policy: policy }); } catch { /* ignore */ }
        const { sessions } = store.get();
        const s = sessions.find(x => x.id === sid);
        if (s) (s as any).approval_policy = policy;
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
    for (const f of Array.from(files)) {
      if (f.type.startsWith("image/")) { e.preventDefault(); addImageFile(f, sid); }
    }
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
    if (files) for (const f of Array.from(files)) { if (f.type.startsWith("image/")) addImageFile(f, sid); }
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
  };

  $("batchDeleteBtn").onclick = async () => {
    const checked = $("sessionList").querySelectorAll<HTMLInputElement>(".session-checkbox:checked");
    const items = Array.from(checked).map(cb => ({ sid: cb.dataset.sid!, workspace: cb.dataset.workspace }));
    if (items.length === 0) return;
    if (!confirm(`Delete ${items.length} sessions?`)) return;
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
      appendError(`Cannot attach image: failed to create session (${e?.message || "unknown"})`);
      return;
    }
    uploadSid = sid || store.get().activeSid;
    if (!uploadSid) {
      appendError("Cannot attach image: failed to create a session");
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
      appendError(`Failed to attach ${file.name}: ${err || "upload failed"}`);
      return;
    }

    // The image belongs to uploadSid's composer regardless of whether
    // the user has since switched sessions — it is a live draft
    // attachment for that session, restored from the draft on return.
    const image = { ...result, name: file.name, thumbUrl };
    setDraftImages(uploadSid, [...draftImages(uploadSid), image]);
    renderComposerPreviews(uploadSid);
  };

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
        // Upload was cancelled (e.g. session switch). Silently discard;
        // the thumbUrl is revoked in cancelInFlightUploads.
        const idx = inFlightUploads.indexOf(uploadEntry);
        if (idx !== -1) inFlightUploads.splice(idx, 1);
        return;
      }
      finish(null, String(e));
    });
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
      <span class="pending-bar-label">排队中</span>
      <span class="pending-bar-text"></span>
      <button class="pending-bar-clear" title="取消排队" type="button">×</button>
    </div>
    <div class="image-previews pane-previews" data-sid="${esc(sid)}" style="display:none"></div>
    <input type="file" class="pane-image-input" data-sid="${esc(sid)}" accept="image/*" multiple style="display:none" />
    <textarea class="pane-prompt" data-sid="${esc(sid)}" placeholder="Ask anything, @ to mention, / for workflows" rows="1"></textarea>
    <div class="slash-menu pane-slash" data-sid="${esc(sid)}" style="display:none"></div>
    <div class="composer-toolbar">
      <div class="toolbar-left">
        <button class="composer-action-btn pane-btn-attach" data-sid="${esc(sid)}" title="Attach image">+</button>
        <select class="pane-approval" data-sid="${esc(sid)}" title="Mode">
          <option value="suggest">Fast</option>
          <option value="auto-edit">Auto Edit</option>
          <option value="full-auto">Full Auto</option>
        </select>
        <select class="pane-model" data-sid="${esc(sid)}" title="Model"></select>
      </div>
      <div class="toolbar-right">
        <span class="char-count pane-charcount" data-sid="${esc(sid)}"></span>
        <div class="context-ring" title="Context usage">
          <svg viewBox="0 0 24 24" width="28" height="28">
            <circle cx="12" cy="12" r="11" fill="none" stroke="var(--line)" stroke-width="2.5" />
            <circle cx="12" cy="12" r="11" fill="none" stroke="var(--accent)" stroke-width="2.5"
              stroke-dasharray="69.12" stroke-dashoffset="69.12" stroke-linecap="round"
              transform="rotate(-90 12 12)" class="pane-context-arc" data-sid="${esc(sid)}" />
          </svg>
          <span class="context-pct pane-context-pct" data-sid="${esc(sid)}"></span>
        </div>
        <button class="composer-action-btn mic-btn pane-btn-mic" data-sid="${esc(sid)}" title="Voice input">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>
        </button>
        <button class="pane-send" data-sid="${esc(sid)}" title="Send">→</button>
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
    const modelDetails = (cfg.model as any).models || (cfg.model.available || []).map((m: string) => ({ name: m, supports_image: true }));
    store.set({ config: { ...store.get().config, model: cfg.model.current, models: cfg.model.available, modelDetails, approval: cfg.approval.current } });
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
  // Enrichment (preview / turnCount / status) requires hitting endpoints
  // that are scoped to the active runtime project. Cross-project sessions
  // keep their disk-side name (or id stub) until the user switches to
  // that project.
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
      s.preview = userMsg ? previewText(userMsg.content) : "Empty session";
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
function renderSessions() {
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
    empty.textContent = "No conversations yet";
    list.appendChild(empty);
    return;
  }

  for (const group of groups) {
    const isActive = group.workspace === activeWs;
    const hasRunning = group.sessions.some(s => store.get().runningSessions[s.id] || s.status === "running");
    const projectDiv = document.createElement("div");
    projectDiv.className = "session-project-group" + (isActive ? " active-project" : "");
    const trimmedBadge = group.trimmed
      ? `<span class="project-trimmed">showing ${group.sessions.length} of ${group.totalCount}</span>`
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
      empty.textContent = isActive ? "No conversations in this project" : "No conversations";
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
            if (confirm("Delete this session?")) deleteSession(s.id, s.workspace);
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
      btn.textContent = `Show all ${activeGroup.totalCount} conversations in ${activeGroup.label}`;
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
}

async function createSession() {
  closeAllFullpageOverlays();
  const id = await api.createSession();
  // New sessions always belong to the active workspace, so tag them here
  // so they show up in the right project group without waiting for the
  // next /sessions refresh.
  const activeWs = store.get().config.workspace || "";
  const sessions = [...store.get().sessions];
  sessions.unshift({ id, turnCount: 0, status: "idle", preview: "Empty session", workspace: activeWs });
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
    const sel = composerModelSelect(oldSid);
    if (sel && sel.value) {
      try { await api.updateSession(oldSid, { model_name: sel.value }); } catch { /* ignore */ }
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
    setCompactToastState("loading", "Compacting context...", sid);
  } else {
    hideCompactToast();
  }
}

async function deleteSession(sid: string, workspace?: string) {
  try {
    await api.deleteSession(sid, workspace ? { workspace } : undefined);
  } catch (e: any) {
    alert("Failed to delete session: " + (e?.message || "unknown"));
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
    if (modelSel && fullData.model_name) {
      if (Array.from(modelSel.options).some((o) => o.value === fullData.model_name)) {
        if (modelSel.value !== fullData.model_name) {
          modelSel.value = fullData.model_name;
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
    // Skip the previous summary message itself — it is the HEAD of the
    // already-rendered tail (or the previous fold's boundary), not part of
    // the range that should be folded AGAIN. Without this, summary N ends
    // up inside the fold for summary N+1, and expanding the second boundary
    // shows the raw summary text as a regular assistant message.
    const foldStart = (prev > 0 && (fullMsgs[prev] as any)._compaction_summary) ? prev + 1 : prev;
    const count = si - foldStart;
    if (count > 0) appendCompactBoundary(sid, count, foldStart, si, target);
    prev = si;
  }
  // Expanded tail: everything after the last folded summary (or the whole
  // history when there were no summaries).
  if (prev < fullMsgs.length) {
    renderMessages(target, fullMsgs.slice(prev));
  }
  return true;
}

// Render a list of messages into a target container using the same DOM
// construction as the live chat (user / assistant / tool cards). Used
// both by `loadHistory` (target = #messages) and by the compact-history
// expand affordance (target = .compact-dropped inside the collapse bar),
// so the folded messages look identical to the live chat — just visually
// scaled down via the wrapper's CSS.
function renderMessages(target: HTMLElement, msgs: any[]): void {
  let pendingToolCalls: { id: string; name: string; arguments: Record<string, unknown> }[] = [];

  // Build index of _hidden image URLs keyed by "[Image file read: <path>]" text
  const hiddenImages = new Map<string, string>();
  for (const m of msgs) {
    if (m.role === "user" && (m as any)._hidden && Array.isArray(m.content)) {
      let textPart = "";
      let imgUrl = "";
      for (const part of m.content) {
        if (typeof part === "object" && part !== null) {
          if ((part as any).type === "text") textPart = (part as any).text || "";
          if ((part as any).type === "image_url" && (part as any).image_url?.url) imgUrl = (part as any).image_url.url;
        }
      }
      if (textPart && imgUrl) {
        // textPart is like "[Image from path]" — match to tool content "[Image file read: path]"
        const pathMatch = textPart.match(/\[Image from (.+)\]/);
        if (pathMatch) hiddenImages.set(`[Image file read: ${pathMatch[1]}]`, imgUrl);
      }
    }
  }

  for (let mi = 0; mi < msgs.length; mi++) {
    const m = msgs[mi];
    const isSub = (m as any)._subagent === true;

    if (m.role === "user") {
      if ((m as any)._hidden) continue;
      appendUserMsg(m.content, target);
    } else if (m.role === "assistant") {
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
          proseDiv.innerHTML = `<div class="msg-inner"><div class="md">${renderMarkdown(inlineMain)}</div></div>`;
          target.appendChild(proseDiv);
          highlightCode(proseDiv);
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

      // If tool content is "[Image file read: ...]", look up the image URL
      // from hiddenImages map (populated from _hidden user messages).
      if (typeof output === "string" && output.startsWith("[Image file read:")) {
        const imgUrl = hiddenImages.get(output);
        if (imgUrl) output = { type: "image", image_url: imgUrl };
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
          card.innerHTML = `<div class="question-text">${esc(q)}</div><div class="question-reply">You: ${esc(answer)}</div>`;
          target.appendChild(card);
        }
        continue;
      }

      appendToolCard(toolName, args, "success", output, subagentTools, isPruned, target);
    }
  }
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
      dropZone.innerHTML = "";
      const originals = fullMsgs.slice(start, end);
      renderMessages(dropZone, originals);
      dropZone.dataset.loaded = "1";
    } catch (e) {
      dropZone.textContent = `加载失败: ${(e as Error).message}`;
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
function appendUserMsg(text: string | unknown[], target: HTMLElement = (getLiveStreamTarget() || $("messages"))): HTMLElement {
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
  el.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div> Thinking...';
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

async function loadHiddenImageForTool(sid: string, imagePath: string, target: HTMLElement = $("messages")) {
  try {
    const data = await api.getMessages(sid, { includeDropped: true });
    const msgs = data.messages || [];
    for (const m of msgs) {
      if (m.role === "user" && (m as any)._hidden && Array.isArray(m.content)) {
        let textPart = "";
        let imgUrl = "";
        for (const part of m.content) {
          if (typeof part === "object" && part !== null) {
            if ((part as any).type === "text") textPart = (part as any).text || "";
            if ((part as any).type === "image_url" && (part as any).image_url?.url) imgUrl = (part as any).image_url.url;
          }
        }
        if (textPart && imgUrl) {
          const pathMatch = textPart.match(/\[Image from (.+)\]/);
          if (pathMatch && pathMatch[1] === imagePath) {
            // Find the last tool card for read_file with this path and inject the image
            const toolCards = target.querySelectorAll(".tool-card");
            for (let i = toolCards.length - 1; i >= 0; i--) {
              const card = toolCards[i];
              const argsEl = card.querySelector(".tool-args");
              if (argsEl && argsEl.textContent?.includes(imagePath)) {
                const body = card.querySelector(".tool-card-body");
                if (body && !body.querySelector("img")) {
                  const imgDiv = document.createElement("div");
                  imgDiv.className = "section-content tool-output-image";
                  imgDiv.innerHTML = `<img src="${esc(imgUrl)}" alt="tool output" loading="lazy" />`;
                  body.appendChild(imgDiv);
                }
                break;
              }
            }
            return;
          }
        }
      }
    }
  } catch { /* best-effort */ }
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
): HTMLElement {
  const card = document.createElement("div");
  // spawn_agent cards should default to expanded (open) for both running and success states
  const isOpen = status === "running" || toolName === "spawn_agent";
  card.className = "tool-card" + (isOpen ? " open" : "") + (isPruned ? " pruned" : "");
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
    if (toolName === "spawn_agent") {
      // Show the agent's completion text, which carries the grouped tools
      // summary ("grep ×3, read_file ×5") on its first line. Read from _text
      // (live) or the string output (replay — persisted content), so the
      // tools summary survives a restart instead of vanishing with the
      // non-persisted event metadata.
      const outText = (typeof output === "object" && output !== null && (output as any)._text)
        ? String((output as any)._text)
        : (typeof output === "string" ? output : "");
      if (outText) {
        body += `<div class="section-label">Output</div>`;
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
      if (imgUrl) {
        body += `<div class="section-label">Output</div>`;
        body += `<div class="section-content tool-output-image"><img src="${esc(imgUrl)}" alt="tool output" loading="lazy" /></div>`;
      }
    } else if (typeof output === "string" && /^data:image\//.test(output)) {
      body += `<div class="section-label">Output</div>`;
      body += `<div class="section-content tool-output-image"><img src="${esc(output)}" alt="tool output" loading="lazy" /></div>`;
    } else {
      // Prefer plain text from _text field (what the model sees), not the
      // full metadata dict that SSE carries for structured data.
      let outStr: string;
      if (typeof output === "object" && output !== null && (output as any)._text) {
        outStr = String((output as any)._text);
        body += `<div class="section-label">Output</div>`;
        body += `<div class="section-content"><pre>${esc(outStr)}</pre></div>`;
      } else {
        outStr = typeof output === "string" ? output : JSON.stringify(output, null, 2);
        body += `<div class="section-label">Output</div>`;
        body += `<div class="section-content"><code>${esc(outStr)}</code></div>`;
      }
    }
  }
  if (subagentTools && Object.keys(subagentTools).length > 0) {
    const summary = Object.entries(subagentTools)
      .map(([n, c]) => `${esc(n)} ×${c}`)
      .join(", ");
    body += `<div class="section-label">used tools</div>`;
    body += `<div class="section-content subagent-tools"><span class="subagent-tool-summary">${summary}</span></div>`;
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
  invalidateLiveStreamEl();
  return card;
}

function appendApprovalCard(requestId: string, toolName: string, args: Record<string, unknown>, target: HTMLElement = $("messages")) {
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
        <input type="text" class="question-input" placeholder="Or type your own answer..." />
        <button class="question-submit" aria-label="Submit">↑</button>
      </div>`;
    } else {
      html += `<div class="question-options">${options.map((o, i) =>
        `<button class="question-option-btn" data-opt="${i}" data-submit="${esc(o.submitValue)}">${o.display}</button>`
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
    api.replyQuestion(questionSid, "（用户放弃当前选项，希望直接讨论这个话题）", callId).catch((e) => {
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
  const sid = (ev as any).session_id as string | undefined;
  if (!sid) return;
  // User pressed stop — drop in-flight tail events until the server
  // confirms with turn_cancelled/turn_failed. Without this, the next
  // 50-200ms of buffered stream chunks keep growing the assistant
  // bubble and re-pulsing the typing chip between stop click and
  // turn_cancelled arrival, which the user perceives as "slow".
  // We still let terminal events through so the cancellation
  // teardown (clear stream / pendingTools / runningSessions) fires.
  const t0 = ev.type as string;
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
    s.status = t === "turn_failed" ? "failed" : (t === "turn_cancelled" ? "idle" : "done");
    const next = { ...runningSessions };
    delete next[sid];
    store.set({ sessions: [...sessions], runningSessions: next });
    renderSessions();
    // Reflect the just-finished turn in the per-pane send/stop button
    // (matters when the session is visible in a split pane, not just active).
    setComposerRunning(sid, false);
    // If the session is shown in a non-active split pane, the live SSE
    // stream only updates #messages, so the secondary pane's optimistic
    // copy plus the streamed assistant turn are stale. Re-fetch the
    // pane from the server to pick up the finalised assistant message.
    const { activeSid: curActive, splitSessions: curSplit } = store.get();
    if (sid !== curActive && curSplit.includes(sid)) {
      const paneMessages = sessionMessagesEl(sid);
      if (paneMessages) loadHistoryInto(sid, paneMessages);
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

  const target = sessionMessagesEl(sid) || $("messages");
  setLiveStreamTarget(target);
  const t = ev.type as string;
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
          <span class="agent-card-title">Background Agent</span>
          <span class="agent-card-status running">Running</span>
        </div>
        <div class="agent-card-task">${esc(taskDesc)}</div>
      `;
      target.appendChild(card);
      if (updateScroll) scrollBottom(target);
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
          statusEl.textContent = status === "failed" ? "Failed" : status === "cancelled" ? "Cancelled" : "Done";
        }
        const toolsUsed = (ev as any).tools_used || 0;
        const toolsSummary = (ev as any).tools_summary as Record<string, number> | undefined;
        const toolsLine = toolsSummary && Object.keys(toolsSummary).length > 0
          ? Object.entries(toolsSummary).map(([n, c]) => `${n} ×${c}`).join(" · ")
          : `${toolsUsed} tool${toolsUsed === 1 ? "" : "s"} used`;
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
            <span class="agent-card-title">Background Agent</span>
            <span class="agent-card-status ${status === 'failed' || status === 'cancelled' ? 'failed' : 'done'}">${status === 'failed' ? 'Failed' : status === 'cancelled' ? 'Cancelled' : 'Done'}</span>
          </div>
          <div class="agent-card-task">${esc(taskDesc)}</div>
          <div class="agent-card-meta">${esc(toolsLine)}</div>
          ${resultPreview ? `<div class="agent-card-result">${renderMarkdown(resultPreview)}</div>` : ''}
        `;
      }
      if (updateScroll) scrollBottom(target);
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
  } else if (t === "stream_reset") {
    // Server: provider returned a retryable error (e.g. 1027 output
    // sensitive) mid-stream. The next attempt will replay the same
    // input from scratch. Wipe the partial text the deltas already
    // painted so the new attempt's deltas land in a fresh block.
    // Disk / history are untouched on the server side, so a page
    // refresh mid-retry would still show the right state.
    resetStreamingState(sid);
  } else if (t === "delta") {
    removeTyping(target);
    clearPaneEmptyPlaceholder(target);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target);
    const content = (ev.content as string) || "";
    (el as any)._main += content;
    // Throttle expensive DOM operations during streaming
    if (!(el as any)._renderTimer) {
      (el as any)._renderTimer = setTimeout(() => {
        (el as any)._renderTimer = null;
        renderAssistantContent(el);
        if (updateScroll) scrollBottom(target);
      }, 80);
    }
  } else if (t === "reasoning_delta") {
    // Anthropic / OpenAI o1/o3 with `reasoning_effort` emit chain-of-thought
    // in a separate `reasoning_content` field, surfaced by the runtime as
    // `reasoning_delta` events. Accumulate into a buffer alongside the
    // main content; renderAssistantContent() merges both into the
    // thinking card. We share the same throttle timer as the main
    // `delta` handler so a fast reasoning burst doesn't double-render.
    removeTyping(target);
    clearPaneEmptyPlaceholder(target);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target);
    const content = (ev.content as string) || "";
    (el as any)._reasoning += content;
    if (!(el as any)._renderTimer) {
      (el as any)._renderTimer = setTimeout(() => {
        (el as any)._renderTimer = null;
        renderAssistantContent(el);
        if (updateScroll) scrollBottom(target);
      }, 80);
    }
  } else if (t === "model_response") {
    // Final full response; ensure _main matches exactly to avoid drift from deltas
    removeTyping(target);
    clearPaneEmptyPlaceholder(target);
    if (target === $("messages")) showEmptyState(false);
    const el = getOrCreateAssistantEl(sid, target);
    // Cancel any pending throttle timer so it doesn't overwrite the final render
    if ((el as any)._renderTimer) {
      clearTimeout((el as any)._renderTimer);
      (el as any)._renderTimer = null;
    }
    const content = (ev.content as string) || "";
    (el as any)._main = content;
    // The runtime currently doesn't include `reasoning_content` in the
    // model_response payload — we keep the value accumulated from the
    // earlier `reasoning_delta` events, which is the canonical source.
    renderAssistantContent(el);
    addCopyButtons(el.parentElement!);
    highlightCode(el.parentElement!);
    if (updateScroll) scrollBottom(target);
  } else if (t === "ask_user_question") {
    removeTyping(target);
    clearPaneEmptyPlaceholder(target);
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
      // Image tool_end events have type:"image" but no image_url (stripped
      // to keep SSE payloads small). Fetch the image from _hidden messages.
      let output = ev.output;
      if (output && typeof output === "object" && (output as any).type === "image" && !(output as any).image_url) {
        const imgMeta = (output as any).metadata || {};
        const imgPath = imgMeta.path || "";
        if (imgPath) {
          // Load _hidden messages to find the image URL (this session's).
          loadHiddenImageForTool(sid, imgPath, target);
          // Show placeholder while loading
          output = { type: "image", metadata: imgMeta };
        }
      }
      appendToolCard(ev.tool as string, (ev.arguments || {}) as Record<string, unknown>, status, output, subagentTools);
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
    if (activeSid) setCompactToastState("loading", "Compacting context...", activeSid);
  } else if (t === "context_compacted") {
    // Auto-compact finished (or the server's echo of a manual /compact).
    // Use the SAME completion path as /compact — one reload + toast.
    if (sid) applyCompactionComplete(sid, "Context compacted");
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
      appendError(ev.error as string || "Unknown error");
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
    if (sid) clearSidCancelled(sid);
    const { activeSid, runningSessions } = store.get();
    const wasRunning = sid ? !!runningSessions[sid] : false;
    if (sid) {
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
      removeTyping();
      invalidateLiveStreamEl();
      if (wasRunning) {
        setActiveRunning(false);
        updateSendStopButton();
        if (t === "turn_failed") {
          appendError("Turn failed");
        }
      }
      // Any tool cards that were still in the running state are now
      // abandoned. Mark them cancelled so they don't show "running..."
      // forever.
      streamCtx(sid).pendingTools.forEach((card) => updateToolCardStatus(card, "cancelled"));
      streamCtx(sid).pendingTools.clear();
      // else: the user already replaced this turn via cancel →
      // flushComposerQueue → createTurn. A fresh turn_start has bumped
      // runningSessions back to true; don't tear that down.
    }
    // Flush queued prompts now that this session's turn has closed (cancel /
    // fail). OUTSIDE the activeSid guard (matches turn_end) so a queued
    // message still flushes even if the user switched sessions. Runs after
    // invalidateLiveStreamEl above (when active), so the next message's
    // assistant stream lands in a fresh element. 200ms lets the server
    // clear turn_task before the next createTurn.
    flushComposerQueue(sid, 200);
  } else if (t === "automation_run") {
    // Refresh automation list when a run completes/fails
    const modal = $("automationModal");
    if (modal && modal.classList.contains("show")) {
      void loadAutomationsIntoModal();
    }
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
  pctLabel.textContent = pctText;
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
  const loadingMsg = isPrune ? "Pruning tool outputs..." : "Compacting context...";
  const successMsg = isPrune ? "Tool outputs pruned" : "Context compacted successfully";

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
      applyCompactionComplete(sid, noop ? "Nothing to compact — context is already minimal" : successMsg);
    }
  } catch (e: any) {
    setCompactToastState("error", (isPrune ? "Prune failed: " : "Compaction failed: ") + (e?.message || e), sid);
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
    btn.title = "Stop";
  } else {
    btn.textContent = "→";
    btn.className = "pane-send";
    btn.title = "Send";
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
    const rm = document.createElement("button");
    rm.className = "image-preview-remove";
    rm.title = "Remove";
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
    item.appendChild(im);
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
  label.textContent = "排队中";
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
    editBtn.textContent = "[编辑]";
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
    if (trimmedCmd === "/compact" || trimmedCmd === "/prune") {
      await runCompactFlow(sid, trimmedCmd === "/prune", messagesEl);
      return;
    }
    if (trimmedCmd.startsWith("/automation ")) {
      const prompt = trimmedCmd.slice("/automation ".length).trim();
      const name = (prompt.slice(0, 30) + (prompt.length > 30 ? "..." : "")) || "Chat task";
      ensureCompactToast();
      setCompactToastState("loading", "Creating automation...", sid);
      await api.createAutomation(name, prompt, 86400, "09:00:00");
      setCompactToastState("success", `Automation "${name}" created (daily at 09:00)`, sid);
      setTimeout(() => hideCompactToast(), 3000);
      return;
    }

    if (!isCommand) {
      setSessionRunning(sid, true);
      setComposerRunning(sid, true);
    }

    const parts: unknown[] = [];
    if (text) parts.push({ type: "text", text });
    for (const img of imgs) parts.push({ type: "image_url", image_url: { url: img.path } });
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
    if (text) parts.push({ type: "text", text });
    for (const img of images) parts.push({ type: "image_url", image_url: { url: img.path } });
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
      appendError(`Queued message permanently failed after ${MAX_QUEUE_RETRIES} attempts: ${e?.message || e}. Please re-type and send again.`, messagesEl || undefined);
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
  // Take only the FIRST item from the queue (FIFO)
  const first = queue[0];
  if (!first) { _flushingQueue[sid] = false; return; }
  setTimeout(async () => {
    try {
      // Remove the item we're about to send
      removePendingItem(sid, first.id);
      renderComposerPending(sid);
      await sendComposerFromQueue(sid, first.text, first.images || [], first.retries, first.id);
    } finally {
      _flushingQueue[sid] = false;
    }
  }, delayMs);
}

function cancelComposerTurn(sid: string) {
  if (!sid) return;
  api.cancelTurn(sid).catch(() => { /* ignore */ });
  // Mark this sid so routeSSEEvent swallows the in-flight tail events
  // (delta / reasoning_delta / tool_start / usage_update) that are
  // already buffered in the SSE pipe or still being generated by the
  // upstream model. Without this, the assistant bubble keeps growing
  // for ~50-200ms after the stop click — the user sees "Thinking..."
  // and new text even though they already pressed stop. Cleared by the
  // turn_cancelled / turn_failed handler below.
  markSidCancelled(sid);
  setSessionRunning(sid, false);
  setComposerRunning(sid, false);
  renderSessions();
  // Don't flush here. The turn_cancelled SSE handler runs
  // invalidateLiveStreamEl BEFORE its flush, so the next queued message's
  // assistant stream lands in a fresh element. Flushing here (before
  // invalidate) raced and let the new turn's deltas flow into the cancelled
  // turn's assistant bubble → "first queued message appears after the
  // answer". turn_cancelled only fires after the server confirmed the
  // cancel, so flushing there is both ordered (invalidate first) and
  // race-free (no 429).
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
  setDraftText(sid, pending);
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
function hydrateComposer(sid: string) {
  const modelSel = composerModelSelect(sid);
  const approvalSel = composerApprovalSelect(sid);
  const { config, sessions } = store.get();
  const s = sessions.find(x => x.id === sid);
  const models = (config as any).modelDetails || ((config as any).model?.available || []).map((m: string) => ({ name: m }));
  if (modelSel) {
    const currentModel = (s as any)?.model_name || config.model;
    modelSel.replaceChildren();
    models.forEach((m: any) => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name;
      if (m.name === currentModel) opt.selected = true;
      modelSel.appendChild(opt);
    });
  }
  if (approvalSel) {
    approvalSel.value = (s as any)?.approval_policy || "full-auto";
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

async function openGitBranchPicker(e: MouseEvent) {
  const target = e.currentTarget as HTMLElement;
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
    alert("Failed to load git branches: " + (err.message || "unknown error"));
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
            alert("Failed to checkout branch: " + err.message);
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
          alert("Failed to create branch: " + err.message);
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

async function openProjectPicker(e: MouseEvent) {
  const target = e.currentTarget as HTMLElement;
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
        if (!confirm(`Remove "${name}" from recent projects?\nThis does not delete any data.`)) return;
        try {
          await api.removeWorkspace(r);
          popup.remove();
          await refreshSessions();
        } catch (e: any) {
          alert("Failed to remove project: " + (e?.message || "unknown"));
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
      alert("Failed to choose folder: " + res.error);
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
  // If the current active session is empty (no messages sent yet), delete
  // it before switching workspaces so we don't leave a stray "Empty session"
  // behind in the old project.
  const { activeSid, sessions } = store.get();
  if (activeSid) {
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

  try {
    await api.switchWorkspace(workspace);
  } catch (err: any) {
    alert("Failed to switch workspace: " + (err?.message || "unknown"));
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

function closeSettingsModal() {
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
