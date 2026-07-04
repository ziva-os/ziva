/** Automations modal + detail — extracted verbatim from main.ts. */

import * as api from "../api";
import { stripThinking } from "../main";
import { esc } from "../dom";
import { closeAllFullpageOverlays } from "../modals";
import type { AppState, Store } from "../state";

// main.ts dependencies (store instance + two small helpers) injected at init
// to avoid a circular import.
interface AutomationsDeps {
  store: Store<AppState>;
  composerTextarea: (sid: string) => HTMLTextAreaElement | null;
  formatRelativeTime: (ts?: number) => string;
}
let _deps: AutomationsDeps;
export function setAutomationsDeps(deps: AutomationsDeps): void { _deps = deps; }

// ---- Automations ----
// The "Scheduled Tasks" nav button opens a modal with a list of running
// automations (name, interval, last run, last result) and a "+ New
// automation" affordance. New automations take a name, a prompt, and
// an interval (in seconds) — the server schedules a background task
// that re-sends the prompt to the runtime every `interval` seconds.
export async function openAutomationsModal() {
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

export function closeAutomationsModal() {
  document.getElementById("automationsModalBackdrop")?.remove();
}

function closeAutomationDetail() {
  document.getElementById("automationDetailBackdrop")?.remove();
}


// ---- Automation detail (fullpage) ----
// The automations modal shows a one-line prompt preview + a few lines
// of last-result preview per card. Clicking a card opens this fullpage
// view with the full prompt, the full last output, schedule / run
// metadata, and actions (run now, pause/resume, delete). It refetches
// the automation on open and after each action so the displayed data
// stays in sync with the server.
async function openAutomationDetail(a: api.Automation) {
  closeAutomationDetail();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "automationDetailBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <button class="fullpage-back-btn" id="automationDetailBackBtn" title="Back">← Back</button>
        <div class="fullpage-title" id="automationDetailTitle">${esc(a.name)}</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body" id="automationDetailBody">
        ${renderAutomationDetailBody(a)}
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  wireAutomationDetailActions(a);
}

function renderAutomationDetailBody(a: api.Automation): string {
  const intervalLabel = formatInterval(a.interval_seconds);
  const scheduleLabel = a.schedule_time ? ` at ${esc(a.schedule_time)}` : "";
  const lastRunLabel = a.last_run ? _deps.formatRelativeTime(Math.floor(a.last_run)) || "just now" : "never";
  const promptText = a.prompt || "(no prompt)";
  const cleanedResult = stripThinking(a.last_result || "");
  const errorText = a.last_error || "";
  const createdLabel = a.created_at ? new Date(a.created_at * 1000).toLocaleString() : "";
  const updatedLabel = a.updated_at ? new Date(a.updated_at * 1000).toLocaleString() : "";
  return `
    <div class="automation-detail">
      <div class="automation-detail-header">
        <div class="automation-detail-status ${a.enabled ? "on" : "off"}">${a.enabled ? "● running" : "○ stopped"}</div>
        <div class="automation-detail-actions">
          <button class="automation-detail-btn" id="automationRunNowBtn">▶ Run now</button>
          <button class="automation-detail-btn" id="automationToggleBtn">${a.enabled ? "⏸ Pause" : "▶ Resume"}</button>
          <button class="automation-detail-btn danger" id="automationDeleteBtn">🗑 Delete</button>
        </div>
      </div>
      <div class="automation-detail-meta">
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">Interval</span><span class="automation-detail-meta-value">⏰ ${esc(intervalLabel)}${scheduleLabel}</span></div>
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">Last run</span><span class="automation-detail-meta-value">${esc(lastRunLabel)}</span></div>
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">Run count</span><span class="automation-detail-meta-value">${a.run_count ?? 0}</span></div>
        ${createdLabel ? `<div class="automation-detail-meta-item"><span class="automation-detail-meta-label">Created</span><span class="automation-detail-meta-value">${esc(createdLabel)}</span></div>` : ""}
        ${updatedLabel ? `<div class="automation-detail-meta-item"><span class="automation-detail-meta-label">Updated</span><span class="automation-detail-meta-value">${esc(updatedLabel)}</span></div>` : ""}
      </div>
      <div class="automation-detail-section">
        <div class="automation-detail-section-header">📝 Prompt</div>
        <pre class="automation-detail-block">${esc(promptText)}</pre>
      </div>
      <div class="automation-detail-section">
        <div class="automation-detail-section-header">📤 Last output</div>
        ${cleanedResult
          ? `<pre class="automation-detail-block">${esc(cleanedResult)}</pre>`
          : `<div class="automation-detail-block muted">No runs yet</div>`}
      </div>
      ${errorText ? `
      <div class="automation-detail-section">
        <div class="automation-detail-section-header">⚠️ Last error</div>
        <pre class="automation-detail-block error">${esc(errorText)}</pre>
      </div>` : ""}
    </div>`;
}

function wireAutomationDetailActions(initial: api.Automation) {
  let current: api.Automation = initial;

  const rerender = () => {
    const body = document.getElementById("automationDetailBody");
    if (body) body.innerHTML = renderAutomationDetailBody(current);
    wire(); // re-wire buttons against the new DOM
  };

  const refetch = async (): Promise<api.Automation | null> => {
    try {
      const list = await api.listAutomations();
      const fresh = list.find((x) => x.id === current.id) || null;
      if (fresh) current = fresh;
      return fresh;
    } catch { return null; }
  };

  const wire = () => {
    const back = document.getElementById("automationDetailBackBtn") as HTMLElement | null;
    if (back) back.onclick = () => {
      closeAutomationDetail();
      // Refresh the list behind us so any state change shows up.
      void loadAutomationsIntoModal();
    };
    const run = document.getElementById("automationRunNowBtn") as HTMLButtonElement | null;
    if (run) run.onclick = async (e) => {
      const btn = e.currentTarget as HTMLButtonElement;
      btn.disabled = true;
      btn.textContent = "▶ Running…";
      try {
        await api.runAutomationNow(current.id);
        // Server runs async — result comes via SSE automation_run event
        setTimeout(() => { btn.disabled = false; btn.textContent = "▶ Run now"; }, 3000);
      } catch (err) {
        alert(`Run failed: ${(err as Error).message}`);
        btn.disabled = false;
        btn.textContent = "▶ Run now";
      }
    };
    const toggle = document.getElementById("automationToggleBtn") as HTMLButtonElement | null;
    if (toggle) toggle.onclick = async (e) => {
      const btn = e.currentTarget as HTMLButtonElement;
      btn.disabled = true;
      try {
        const nextEnabled = !current.enabled;
        await api.updateAutomation(current.id, { enabled: nextEnabled });
        await refetch();
        rerender();
      } catch (err) {
        alert(`Toggle failed: ${(err as Error).message}`);
        btn.disabled = false;
      }
    };
    const del = document.getElementById("automationDeleteBtn") as HTMLButtonElement | null;
    if (del) del.onclick = async () => {
      if (!confirm(`Delete "${current.name}"?`)) return;
      try {
        await api.deleteAutomation(current.id);
        closeAutomationDetail();
        void loadAutomationsIntoModal();
      } catch (err) {
        alert(`Delete failed: ${(err as Error).message}`);
      }
    };
  };

  wire();
}

export async function loadAutomationsIntoModal() {
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
        <label class="automation-label">Time (HH:MM:SS)
          <input type="time" class="automation-input" id="automationTimeInput" step="1" />
        </label>
      </div>
      <div class="automation-form-actions">
        <button class="automation-submit-btn" id="automationSubmitBtn">Create</button>
        <button class="automation-submit-btn secondary" id="automationFromChatBtn" title="Fill prompt from last user message">From chat</button>
        <span class="automation-form-status" id="automationFormStatus"></span>
      </div>
    </div>`;
  body.innerHTML = html;

  // Wire row-level delete buttons
  body.querySelectorAll<HTMLElement>(".automation-row-delete").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
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

  // Wire row click to open the detail page (the delete button stops
  // propagation above so it doesn't trigger navigation).
  const cardToAutomation = new Map<string, api.Automation>();
  automations.forEach((a) => cardToAutomation.set(a.id, a));
  body.querySelectorAll<HTMLElement>(".automation-row").forEach((row) => {
    const aid = row.dataset.aid!;
    const a = cardToAutomation.get(aid);
    if (!a) return;
    const open = () => void openAutomationDetail(a);
    row.onclick = open;
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    };
  });

  // Wire "From chat" button — close modal, pre-fill composer with /automation command
  (body.querySelector("#automationFromChatBtn") as HTMLElement).onclick = async () => {
    const { activeSid } = _deps.store.get();
    closeAutomationsModal();
    if (!activeSid) return;
    try {
      const data = await api.getMessages(activeSid);
      const msgs = data.messages || [];
      const lastUser = [...msgs].reverse().find((m: any) => m.role === "user");
      if (lastUser) {
        const text = typeof lastUser.content === "string" ? lastUser.content : JSON.stringify(lastUser.content);
        // Pre-fill the global #prompt textarea with the automation
        // command + the last user message.
        const promptEl = _deps.composerTextarea(_deps.store.get().activeSid || "");
        if (promptEl) {
          promptEl.value = `/automation ${text}`;
          promptEl.style.height = "auto";
          promptEl.style.height = Math.min(promptEl.scrollHeight, 160) + "px";
          promptEl.focus();
        }
      }
    } catch { /* ignore */ }
  };

  // Wire the form submit
  (body.querySelector("#automationSubmitBtn") as HTMLElement).onclick = async () => {
    const name = (body.querySelector("#automationNameInput") as HTMLInputElement).value.trim();
    const prompt = (body.querySelector("#automationPromptInput") as HTMLTextAreaElement).value.trim();
    const interval = parseInt((body.querySelector("#automationIntervalInput") as HTMLSelectElement).value, 10);
    const timeVal = (body.querySelector("#automationTimeInput") as HTMLInputElement).value;
    const scheduleTime = timeVal || undefined;
    const statusEl = body.querySelector("#automationFormStatus") as HTMLElement;
    if (!prompt) {
      statusEl.textContent = "Prompt is required";
      statusEl.className = "automation-form-status error";
      return;
    }
    statusEl.textContent = "Creating...";
    statusEl.className = "automation-form-status";
    try {
      await api.createAutomation(name || "Untitled task", prompt, interval, scheduleTime);
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
  const scheduleLabel = a.schedule_time ? ` at ${esc(a.schedule_time)}` : "";
  const lastRunLabel = a.last_run ? _deps.formatRelativeTime(Math.floor(a.last_run)) || "just now" : "never";
  const promptText = (a.prompt || "").trim() || "(no prompt)";
  const cleanedResult = stripThinking(a.last_result || "");
  const resultText = cleanedResult || "No runs yet";
  const hasResult = !!cleanedResult;
  return `
    <div class="automation-row" data-aid="${esc(a.id)}" tabindex="0" role="button" aria-label="Open automation details">
      <div class="automation-row-main">
        <div class="automation-row-name">${esc(a.name)}</div>
        <div class="automation-row-meta">
          <span class="automation-row-interval">⏰ ${esc(intervalLabel)}${scheduleLabel}</span>
          <span class="automation-row-lastrun">Last run: ${esc(lastRunLabel)}</span>
          <span class="automation-row-status ${a.enabled ? "on" : "off"}">${a.enabled ? "● running" : "○ stopped"}</span>
        </div>
        <div class="automation-row-preview automation-row-preview-prompt" title="${esc(promptText)}">
          <span class="automation-row-preview-icon">📝</span><span class="automation-row-preview-text">${esc(promptText)}</span>
        </div>
        <div class="automation-row-preview automation-row-preview-result${hasResult ? "" : " muted"}" title="${esc(resultText)}">
          <span class="automation-row-preview-icon">📤</span><span class="automation-row-preview-text">${esc(resultText)}</span>
        </div>
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
