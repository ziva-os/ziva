/** Automations modal + detail — extracted verbatim from main.ts. */

import * as api from "../api";
import * as i18n from "../i18n";
import { stripThinking } from "../main";
import { renderMarkdown } from "../markdown";
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
        <div class="fullpage-title">${esc(i18n.t("automations.title"))}</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body" id="automationsModalBody">
        <div class="skills-modal-loading">${esc(i18n.t("automations.loading"))}</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  await loadAutomationsIntoModal();
}

export function closeAutomationsModal() {
  document.getElementById("automationsModalBackdrop")?.remove();
}

// When a detail page is open, this holds a closure that re-fetches the
// automation and re-renders its body. Set on open, cleared on close, and
// invoked by main.ts's `automation_run` SSE handler so a run that finishes
// in the background updates the open detail view (last_result / last_run)
// without the user reopening it. The run-now endpoint is fire-and-forget,
// so the result only ever arrives via that SSE event.
let _detailRefresh: (() => void) | null = null;

// Called from main.ts when an `automation_run` SSE event arrives. No-op if
// no detail page is open. Also re-renders the list modal if it's open so
// the row previews (last run / last output) stay fresh.
export function refreshAutomationDetailIfOpen() {
  if (_detailRefresh) void _detailRefresh();
}

function closeAutomationDetail() {
  _detailRefresh = null;
  document.getElementById("automationDetailBackdrop")?.remove();
}

function closeRunDetail() {
  document.getElementById("automationRunDetailBackdrop")?.remove();
}

// Open a fullpage view for a single run's input + output (markdown) + error.
// Clicking a run card in the automation detail navigates here instead of
// expanding inline, so long outputs stay readable on their own screen.
function openRunDetail(run: NonNullable<api.Automation["runs"]>[number]) {
  closeRunDetail();
  const ok = run.status === "done";
  const time = run.ts ? new Date(run.ts * 1000).toLocaleString() : "—";
  const outputHtml = run.result ? renderMarkdown(stripThinking(run.result)) : "";
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "automationRunDetailBackdrop";
  const shell = document.createElement("div");
  shell.className = "fullpage-shell";
  shell.innerHTML = `
    <div class="fullpage-topbar">
      <button class="fullpage-back-btn" id="runDetailBackBtn" title="${esc(i18n.t("automations.detail.back"))}">← ${esc(i18n.t("automations.detail.back"))}</button>
      <div class="fullpage-title">${esc(i18n.t("automations.detail.runTitle", { time }))}</div>
      <div class="fullpage-topbar-spacer"></div>
    </div>
    <div class="fullpage-body">
      <div class="automation-run-detail">
        <div class="automation-run-detail-status ${ok ? "ok" : "fail"}">
          <span>${ok ? esc(i18n.t("automations.detail.completed")) : esc(i18n.t("automations.detail.failed"))}</span>
          <span class="automation-run-detail-status-time">${esc(time)}</span>
        </div>
        <div class="automation-detail-section">
          <div class="automation-detail-section-header">${esc(i18n.t("automations.detail.input"))}</div>
          <pre class="automation-detail-block"></pre>
        </div>
        <div class="automation-detail-section">
          <div class="automation-detail-section-header">${esc(i18n.t("automations.detail.output"))}</div>
          <div class="run-output-host"></div>
        </div>
        ${run.error ? `<div class="automation-detail-section"><div class="automation-detail-section-header">${esc(i18n.t("automations.detail.error"))}</div><pre class="automation-detail-block error"></pre></div>` : ""}
      </div>
    </div>`;
  // Set untrusted text via textContent, markdown output via innerHTML
  // (renderMarkdown is the same path the chat view uses for model output).
  const inputPre = shell.querySelector<HTMLElement>(".automation-detail-block:not(.error)");
  if (inputPre) inputPre.textContent = run.prompt || "";
  const outputHost = shell.querySelector<HTMLElement>(".run-output-host");
  if (outputHost) {
    if (outputHtml) outputHost.innerHTML = `<div class="md run-output-markdown">${outputHtml}</div>`;
    else outputHost.textContent = i18n.t("automations.detail.noOutput");
  }
  const errorPre = shell.querySelector<HTMLElement>("pre.error");
  if (errorPre) errorPre.textContent = run.error || "";
  backdrop.appendChild(shell);
  document.body.appendChild(backdrop);
  document.getElementById("runDetailBackBtn")!.onclick = () => closeRunDetail();
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
        <button class="fullpage-back-btn" id="automationDetailBackBtn" title="${esc(i18n.t("automations.detail.back"))}">← ${esc(i18n.t("automations.detail.back"))}</button>
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
  const scheduleLabel = a.schedule_time ? i18n.t("automations.row.scheduleAt", { time: a.schedule_time }) : "";
  const lastRunLabel = a.last_run ? _deps.formatRelativeTime(Math.floor(a.last_run)) || i18n.t("automations.row.justNow") : i18n.t("automations.row.never");
  const promptText = a.prompt || i18n.t("automations.row.noPrompt");
  const runs = a.runs || [];
  const createdLabel = a.created_at ? new Date(a.created_at * 1000).toLocaleString() : "";
  const runsHtml = runs.length === 0
    ? `<div class="automation-detail-block muted">${esc(i18n.t("automations.detail.noRunsHint"))}</div>`
    : runs.map((r) => {
        const time = r.ts ? new Date(r.ts * 1000).toLocaleString() : "—";
        const ok = r.status === "done";
        const previewText = (r.result || r.error || "").replace(/\s+/g, " ").trim();
        const preview = previewText.slice(0, 160);
        return `<div class="automation-run-card" data-run-id="${esc(r.id)}" tabindex="0" role="button" aria-label="Open run details">
          <div class="automation-run-card-row">
            <span class="automation-run-status ${ok ? "ok" : "fail"}">${ok ? "✓" : "✗"}</span>
            <span class="automation-run-time">${esc(time)}</span>
            <span class="automation-run-arrow" aria-hidden="true">→</span>
          </div>
          <div class="automation-run-preview">${preview ? esc(preview) + (previewText.length > 160 ? "…" : "") : `<span class="muted">${esc(i18n.t("automations.detail.noOutput"))}</span>`}</div>
        </div>`;
      }).join("");
  return `
    <div class="automation-detail">
      <div class="automation-detail-header">
        <div class="automation-detail-status ${a.enabled ? "on" : "off"}">${esc(a.enabled ? i18n.t("automations.detail.statusRunning") : i18n.t("automations.detail.statusStopped"))}</div>
        <div class="automation-detail-actions">
          <button class="automation-detail-btn" id="automationRunNowBtn">${esc(i18n.t("automations.detail.runNow"))}</button>
          <button class="automation-detail-btn" id="automationToggleBtn">${esc(a.enabled ? i18n.t("automations.detail.pause") : i18n.t("automations.detail.resume"))}</button>
          <button class="automation-detail-btn danger" id="automationDeleteBtn">${esc(i18n.t("automations.detail.delete"))}</button>
        </div>
      </div>
      <div class="automation-detail-meta">
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">${esc(i18n.t("automations.detail.metaInterval"))}</span><span class="automation-detail-meta-value">⏰ ${esc(intervalLabel)}${esc(scheduleLabel)}</span></div>
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">${esc(i18n.t("automations.detail.metaLastRun"))}</span><span class="automation-detail-meta-value">${esc(lastRunLabel)}</span></div>
        <div class="automation-detail-meta-item"><span class="automation-detail-meta-label">${esc(i18n.t("automations.detail.metaRunCount"))}</span><span class="automation-detail-meta-value">${a.run_count ?? 0}</span></div>
        ${createdLabel ? `<div class="automation-detail-meta-item"><span class="automation-detail-meta-label">${esc(i18n.t("automations.detail.metaCreated"))}</span><span class="automation-detail-meta-value">${esc(createdLabel)}</span></div>` : ""}
      </div>
      <div class="automation-detail-section">
        <div class="automation-detail-section-header">${esc(i18n.t("automations.detail.prompt"))}</div>
        <pre class="automation-detail-block">${esc(promptText)}</pre>
      </div>
      <div class="automation-detail-section">
        <div class="automation-detail-section-header">${esc(i18n.t("automations.detail.runs", { n: runs.length }))}</div>
        <div class="automation-runs-list">${runsHtml}</div>
      </div>
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

  // Expose a refresh closure so the SSE `automation_run` handler can update
  // this detail page when a background run completes.
  _detailRefresh = async () => { await refetch(); rerender(); };

  const wire = () => {
    const back = document.getElementById("automationDetailBackBtn") as HTMLElement | null;
    if (back) back.onclick = () => {
      closeAutomationDetail();
      // Refresh the list behind us so any state change shows up.
      void loadAutomationsIntoModal();
    };
    // Click a run card → open a fullpage view of that run's input/output.
    document.querySelectorAll<HTMLElement>("#automationDetailBody .automation-run-card").forEach((card) => {
      card.onclick = () => {
        const runId = card.dataset.runId;
        const run = current.runs?.find((r) => r.id === runId);
        if (run) openRunDetail(run);
      };
    });
    const run = document.getElementById("automationRunNowBtn") as HTMLButtonElement | null;
    if (run) run.onclick = async (e) => {
      const btn = e.currentTarget as HTMLButtonElement;
      btn.disabled = true;
      btn.textContent = i18n.t("automations.detail.running");
      try {
        // Run in the automation's hidden backing session — runs (manual and
        // scheduled) surface as cards in this detail view, not in the chat.
        await api.runAutomationNow(current.id);
        // Server runs the turn async; the result arrives via the SSE
        // `automation_run` event, which calls refreshAutomationDetailIfOpen
        // → refetch + rerender, rebuilding this button (so it resets to
        // "Run now") and showing the new last_result. Keep a long safety
        // timeout in case the event is missed so the button isn't stuck.
        setTimeout(() => { btn.disabled = false; btn.textContent = i18n.t("automations.detail.runNow"); }, 300000);
      } catch (err) {
        alert(i18n.t("automations.alert.runFailed", { err: (err as Error).message }));
        btn.disabled = false;
        btn.textContent = i18n.t("automations.detail.runNow");
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
        alert(i18n.t("automations.alert.toggleFailed", { err: (err as Error).message }));
        btn.disabled = false;
      }
    };
    const del = document.getElementById("automationDeleteBtn") as HTMLButtonElement | null;
    if (del) del.onclick = async () => {
      if (!confirm(i18n.t("automations.confirm.deleteName", { name: current.name }))) return;
      try {
        await api.deleteAutomation(current.id);
        closeAutomationDetail();
        void loadAutomationsIntoModal();
      } catch (err) {
        alert(i18n.t("automations.alert.deleteFailed", { err: (err as Error).message }));
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
    body.innerHTML = `<div class="skills-modal-error">${esc(i18n.t("automations.loadFailed", { err: (e as Error).message }))}</div>`;
    return;
  }
  let html = "";
  if (automations.length === 0) {
    html = `<div class="automations-empty">${esc(i18n.t("automations.empty"))}</div>`;
  } else {
    html = '<div class="automations-list">' + automations
      .map((a) => renderAutomationRow(a))
      .join("") + '</div>';
  }
  html += `
    <div class="automation-create-form" id="automationCreateForm">
      <div class="automation-create-header">${esc(i18n.t("automations.form.newHeader"))}</div>
      <label class="automation-label">${esc(i18n.t("automations.form.name"))}<input type="text" class="automation-input" id="automationNameInput" placeholder="${esc(i18n.t("automations.form.namePlaceholder"))}" /></label>
      <label class="automation-label">${esc(i18n.t("automations.form.prompt"))}<textarea class="automation-input automation-textarea" id="automationPromptInput" placeholder="${esc(i18n.t("automations.form.promptPlaceholder"))}"></textarea></label>
      <div class="automation-label-row">
        <label class="automation-label">${esc(i18n.t("automations.form.interval"))}
          <select class="automation-input" id="automationIntervalInput">
            <option value="60">${esc(i18n.t("automations.interval.1m"))}</option>
            <option value="300" selected>${esc(i18n.t("automations.interval.5m"))}</option>
            <option value="900">${esc(i18n.t("automations.interval.15m"))}</option>
            <option value="3600">${esc(i18n.t("automations.interval.hour"))}</option>
            <option value="21600">${esc(i18n.t("automations.interval.6h"))}</option>
            <option value="86400">${esc(i18n.t("automations.interval.day"))}</option>
            <option value="604800">${esc(i18n.t("automations.interval.week"))}</option>
          </select>
        </label>
        <label class="automation-label">${esc(i18n.t("automations.form.time"))}
          <input type="time" class="automation-input" id="automationTimeInput" step="1" />
        </label>
      </div>
      <div class="automation-form-actions">
        <button class="automation-submit-btn" id="automationSubmitBtn">${esc(i18n.t("automations.form.create"))}</button>
        <button class="automation-submit-btn secondary" id="automationFromChatBtn" title="${esc(i18n.t("automations.form.fromChatTitle"))}">${esc(i18n.t("automations.form.fromChat"))}</button>
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
      if (!confirm(i18n.t("automations.confirm.deleteName", { name }))) return;
      try {
        await api.deleteAutomation(aid);
        await loadAutomationsIntoModal();
      } catch (e) {
        alert(i18n.t("automations.alert.deleteFailed", { err: (e as Error).message }));
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
      statusEl.textContent = i18n.t("automations.form.promptRequired");
      statusEl.className = "automation-form-status error";
      return;
    }
    statusEl.textContent = i18n.t("automations.form.creating");
    statusEl.className = "automation-form-status";
    try {
      await api.createAutomation(name || i18n.t("automations.untitled"), prompt, interval, scheduleTime);
      statusEl.textContent = i18n.t("automations.form.created");
      statusEl.className = "automation-form-status success";
      // Brief success flash, then re-render the list
      setTimeout(() => loadAutomationsIntoModal(), 400);
    } catch (e) {
      statusEl.textContent = (e as Error).message || i18n.t("automations.form.createFailed");
      statusEl.className = "automation-form-status error";
    }
  };
}

function renderAutomationRow(a: api.Automation): string {
  const intervalLabel = formatInterval(a.interval_seconds);
  const scheduleLabel = a.schedule_time ? i18n.t("automations.row.scheduleAt", { time: a.schedule_time }) : "";
  const lastRunLabel = a.last_run ? _deps.formatRelativeTime(Math.floor(a.last_run)) || i18n.t("automations.row.justNow") : i18n.t("automations.row.never");
  const promptText = (a.prompt || "").trim() || i18n.t("automations.row.noPrompt");
  const cleanedResult = stripThinking(a.last_result || "");
  const resultText = cleanedResult || i18n.t("automations.row.noRuns");
  const hasResult = !!cleanedResult;
  return `
    <div class="automation-row" data-aid="${esc(a.id)}" data-enabled="${a.enabled ? "true" : "false"}" tabindex="0" role="button" aria-label="Open automation details">
      <div class="automation-row-main">
        <div class="automation-row-name">${esc(a.name)}</div>
        <div class="automation-row-meta">
          <span class="automation-row-meta-item">⏰ ${esc(intervalLabel)}${esc(scheduleLabel)}</span>
          <span class="automation-row-meta-item">${esc(i18n.t("automations.row.lastRun", { when: lastRunLabel }))}</span>
          <span class="automation-row-meta-item automation-row-status ${a.enabled ? "on" : "off"}">${esc(a.enabled ? i18n.t("automations.row.running") : i18n.t("automations.row.stopped"))}</span>
        </div>
        <div class="automation-row-preview automation-row-preview-prompt" title="${esc(promptText)}">
          <span class="automation-row-preview-icon">📝</span><span class="automation-row-preview-text">${esc(promptText)}</span>
        </div>
        <div class="automation-row-preview automation-row-preview-result${hasResult ? "" : " muted"}" title="${esc(resultText)}">
          <span class="automation-row-preview-icon">📤</span><span class="automation-row-preview-text">${esc(resultText)}</span>
        </div>
      </div>
      <div class="automation-row-actions">
        <button class="automation-row-delete" data-aid="${esc(a.id)}" data-name="${esc(a.name)}" title="${esc(i18n.t("automations.detail.delete"))}">🗑</button>
      </div>
    </div>`;
}

function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
