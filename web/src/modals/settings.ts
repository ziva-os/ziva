/** Settings modal — extracted verbatim from main.ts (one large async fn). */

import * as api from "../api";
import { esc } from "../dom";
import * as i18n from "../i18n";
import { closeSettingsModal, renderSessions } from "../main";
import { closeAllFullpageOverlays } from "../modals";

// refreshConfig() lives in main.ts; injected at init to avoid a circular import.
let _refreshConfig: () => Promise<void> = async () => {};
export function setSettingsDeps(opts: { refreshConfig: () => Promise<void> }): void {
  _refreshConfig = opts.refreshConfig;
}

// Label for the "Add <kind>..." dropdown placeholder inside agent cards.
function addKindLabel(kind: string): string {
  return kind === "tools" ? i18n.t("settings.addTools")
    : kind === "skills" ? i18n.t("settings.addSkills")
    : i18n.t("settings.addHooks");
}

export async function openSettingsModal() {
  // Toggle: if already open, clicking Settings again closes it.
  if (document.getElementById("settingsModalBackdrop")) { closeSettingsModal(); return; }
  closeAllFullpageOverlays();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "settingsModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">${i18n.t("settings.title")}</div>
        <div class="fullpage-topbar-spacer"></div>
        <button class="settings-save-btn" id="settingsSaveBtn">${i18n.t("settings.save")}</button>
      </div>
      <div class="fullpage-body settings-body">
        <div class="settings-loading">${i18n.t("settings.loadingConfig")}</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const body = backdrop.querySelector(".fullpage-body") as HTMLElement;

  try {
    const cfg = await api.getConfigJson();
    const ap = cfg.approval || {};
    const mem = cfg.memory || {};
    const tool = cfg.tool || {};
    const mcp = cfg.mcp || {};
    const mcpServers = mcp.servers || {};
    const sandbox = cfg.sandbox || {};
    const hooks = cfg.hooks || {};
    const prompt = cfg.prompt || {};
    const agents = (cfg.agents || {}) as Record<string, any>;

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
      agents: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="7" r="3"/><circle cx="17" cy="7" r="2.5"/><path d="M3 21v-1a6 6 0 0 1 12 0v1"/><path d="M14 14a5 5 0 0 1 7 4v1"/></svg>`,
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
            <input class="settings-input settings-mcp-name" data-mcp-name="${esc(sname)}" value="${esc(sname)}" placeholder="${i18n.t("settings.serverName")}" style="font-weight:600;font-size:13px" />
            <div>
              <select class="settings-select" style="width:auto;padding:4px 8px;font-size:12px" data-mcp-enabled="${esc(sname)}">
                <option value="true" ${srv.enabled !== false ? "selected" : ""}>${i18n.t("common.enabled")}</option>
                <option value="false" ${srv.enabled === false ? "selected" : ""}>${i18n.t("common.disabled")}</option>
              </select>
              <button class="settings-hook-remove" data-mcp-remove="${esc(sname)}" title="${i18n.t("common.remove")}">×</button>
            </div>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.command")}</label><input class="settings-input" data-mcp-command="${esc(sname)}" value="${esc(cmd)}" /></div>
          <div class="settings-row"><label class="settings-label">${i18n.t("common.type")}</label>
            <select class="settings-select" data-mcp-type="${esc(sname)}">
              <option value="local" ${srv.type !== "remote" ? "selected" : ""}>${i18n.t("settings.mcpTypeLocal")}</option>
              <option value="remote" ${srv.type === "remote" ? "selected" : ""}>${i18n.t("settings.mcpTypeRemote")}</option>
            </select>
          </div>
        </div>`;
    }

    // Build hooks HTML — fetch registered hooks from backend + folder register input
    // Hook options for the per-agent dimension selector come from the registry,
    // not from a hard-coded list of event names. The agent config then stores
    // hook IDs (e.g. "hook.image_guard"), which the runtime matches directly.
    // A fallback empty list keeps the modal usable if the backend isn't ready.
    const registeredHooks: Array<{ id: string; label: string; eventName: string }> = [];
    const hookPhaseLabel: Record<string, string> = {
      before_turn: i18n.t("settings.hookPhase.beforeTurn"),
      after_turn: i18n.t("settings.hookPhase.afterTurn"),
      before_tool: i18n.t("settings.hookPhase.beforeTool"),
      after_tool: i18n.t("settings.hookPhase.afterTool"),
    };
    try {
      for (const h of await api.listHooks()) {
        const phase = hookPhaseLabel[h.event_name] || h.event_name;
        registeredHooks.push({ id: h.id, label: `${h.name} · ${phase}`, eventName: h.event_name });
      }
    } catch (_e) {
      // leave registeredHooks empty; selectors will be empty
    }
    const hookTypes = registeredHooks;
    let hooksHtml = '<div id="hooksPanel"></div>';

    // Build agents HTML
    // Each agent has: name (key), instructions (textarea), tools
    // (multi-select from cfg.tools), skills (multi-select from
    // enabled skills), memory (backend selector), background (bool).
    // `tools` / `skills` / `memory` are pre-populated from the agent
    // def but the user can override per-agent.
    const [status, skillIndex] = await Promise.all([
      api.getStatus().catch(() => ({ tools: [] as string[] })),
      api.listSkills().catch(() => [] as { name: string; description?: string }[]),
    ]);
    const allToolNames: string[] = status.tools || [];
    const allSkillNames: string[] = skillIndex.map((s: any) => s.name).filter(Boolean);
    const agentEntries = Object.entries(agents);
    const agentsHtml = agentEntries.map(([name, def]) => {
      const instructions = (def.instructions || "") as string;
      const description = (def.description || "") as string;
      const background = !!def.background;
      // Three-state mode detection: allow / deny / inherit
      const toolMode = def.deny_tools ? "deny" : def.tools ? "allow" : "inherit";
      const skillMode = def.deny_skills ? "deny" : def.skills ? "allow" : "inherit";
      const hookMode = def.deny_hooks ? "deny" : def.hooks ? "allow" : "inherit";
      const agentTools: string[] = def.tools || def.deny_tools || [];
      const agentSkills: string[] = def.skills || def.deny_skills || [];
      const agentHooks: string[] = def.hooks || def.deny_hooks || [];

      // Build a mode selector + tag selector section for one dimension.
      // ``all`` accepts either a flat list of strings (tools/skills) or
      // ``Array<{id, label}>`` (hooks) so each kind can carry its own
      // display metadata. ``selected`` is always a flat list of ids.
      //
      // Progressive disclosure: the section defaults to a static
      // "Inherit all" chip with a Customize link. Only after the user
      // clicks Customize does the mode dropdown + tag picker appear.
      // This keeps the common case (no restriction) visually quiet.
      type HookOption = { id: string; label: string };
      const buildDimension = (kind: string, label: string, desc: string, all: Array<string | HookOption>, selected: string[], mode: string) => {
        const valueOf = (x: string | HookOption) => typeof x === "string" ? x : x.id;
        const labelOf = (x: string | HookOption) => typeof x === "string" ? x : x.label;
        // isCustom drives whether the section shows the static "Inherit
        // all" chip + Customize link (default) or the dropdown + tag
        // picker (after the user opts in). Must be declared BEFORE any
        // template literal that references it — otherwise the templates
        // evaluate while `isCustom` is still in the temporal dead zone
        // and the modal blows up with "Cannot access 'is' before
        // initialization".
        const isCustom = mode !== "inherit";
        const inheritChip = `
          <span class="agent-inherit-chip" data-kind="${kind}" data-agent-name="${esc(name)}"
                style="display:${isCustom ? "none" : "inline-flex"};align-items:center;gap:6px;margin-left:8px;font-size:11px;color:var(--muted);background:var(--surface-alt);padding:3px 8px;border-radius:4px">
            <span style="font-weight:500">${i18n.t("settings.modeInherit") || "Inherit all"}</span>
            <button type="button" class="agent-customize-btn" data-customize-kind="${kind}" data-agent-name="${esc(name)}"
                    style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:11px;padding:0;text-decoration:underline">
              ${i18n.t("settings.customize") || "Customize…"}
            </button>
          </span>`;
        const modeOpts = [
          { v: "inherit", l: i18n.t("settings.modeInherit") || "Inherit all" },
          { v: "allow", l: i18n.t("settings.modeAllow") || "Allow specific" },
          { v: "deny", l: i18n.t("settings.modeDeny") || "Deny specific" },
        ].map(o => `<option value="${o.v}" ${mode === o.v ? "selected" : ""}>${o.l}</option>`).join("");
        const modeSelect = `<select class="settings-select agent-mode-select" data-agent-mode="${kind}" data-agent-name="${esc(name)}" style="display:${isCustom ? "inline-block" : "none"};margin-left:8px;font-size:11px;width:auto;padding:2px 6px">${modeOpts}</select>`;
        const options = all.filter((x) => !selected.includes(valueOf(x))).map((x) => `<option value="${esc(valueOf(x))}">${esc(labelOf(x))}</option>`).join("");
        const tagSelect = `<select class="settings-select agent-${kind}-select" data-agent-select-${kind}="${esc(name)}"><option value="">${addKindLabel(kind)}</option>${options}</select>`;
        // For tags, render the human label when the selected id corresponds to a
        // hook option; fall back to the raw id for tools/skills.
        const labelFor = (id: string) => {
          for (const x of all) if (typeof x !== "string" && x.id === id) return x.label;
          return id;
        };
        const tags = selected.map((x) => `<span class="agent-selected-tag" data-kind="${kind}" data-value="${esc(x)}">${esc(labelFor(x))}<button type="button" class="agent-selected-remove" data-remove-kind="${kind}" data-remove="${esc(x)}">×</button></span>`).join("");
        const tagBox = `<div class="agent-selected-box" data-agent-box-${kind}="${esc(name)}">${tags}</div>`;
        return `
          <div class="settings-section">
            <div class="settings-section-title">${label}${inheritChip}${modeSelect}</div>
            <div class="settings-desc">${desc}</div>
            <div class="agent-tags-area" data-agent-tags-area="${kind}" data-agent-name="${esc(name)}" style="display:${isCustom ? "block" : "none"}">
              <div class="settings-row" style="margin:6px 0;gap:8px">
                <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="${kind}" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
                <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="${kind}" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
              </div>
              ${tagSelect}
              ${tagBox}
            </div>
          </div>`;
      };
      return `
        <div class="settings-agent-card" data-agent-name="${esc(name)}">
          <div class="settings-agent-card-header">
            <input class="settings-input settings-agent-name" data-agent-rename="${esc(name)}" value="${esc(name)}" placeholder="${i18n.t("settings.agentNameExample")}" style="font-weight:600;font-size:13px" />
            <label class="agent-bg-label"><input type="checkbox" data-agent-bg="${esc(name)}" ${background ? "checked" : ""} /> ${i18n.t("settings.background")}</label>
            <button class="settings-hook-remove" data-agent-remove="${esc(name)}" title="${i18n.t("settings.removeAgent")}">×</button>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.agentDescription") || "Description"}</div>
            <div class="settings-desc">${i18n.t("settings.agentDescriptionHint") || "One-line summary shown to the parent agent in the spawn_agent tool description."}</div>
            <input class="settings-input settings-agent-description" data-agent-description="${esc(name)}" value="${esc(description)}" placeholder="${i18n.t("settings.agentDescriptionPlaceholder") || "e.g. Read-only investigation agent"}" />
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.instructions")}</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(name)}" rows="8" placeholder="${i18n.t("settings.instructionsPlaceholder")}">${esc(instructions)}</textarea>
          </div>
          ${buildDimension("tools", i18n.t("settings.tools"), i18n.t("settings.toolsDesc"), allToolNames, agentTools, toolMode)}
          ${buildDimension("skills", i18n.t("settings.skills"), i18n.t("settings.skillsDesc"), allSkillNames, agentSkills, skillMode)}
          ${buildDimension("hooks", i18n.t("settings.hooks"), i18n.t("settings.agentHooksDesc"), hookTypes, agentHooks, hookMode)}
        </div>`;
    }).join("");

    // Wire the dropdown + removable tag UX for a single agent card.
    function wireAgentSelections(card: HTMLElement, name: string) {
      // Customize button: swap the static "Inherit all" chip for the
      // dropdown (defaulting to "allow"), reveal the tag picker area.
      // This is the entry point from the default low-noise state.
      card.querySelectorAll<HTMLElement>(".agent-customize-btn").forEach(btn => {
        btn.addEventListener("click", () => {
          const kind = btn.dataset.customizeKind!;
          const chip = card.querySelector(`.agent-inherit-chip[data-kind="${kind}"][data-agent-name="${name}"]`) as HTMLElement | null;
          const dropdown = card.querySelector(`select.agent-mode-select[data-agent-mode="${kind}"][data-agent-name="${name}"]`) as HTMLSelectElement | null;
          const area = card.querySelector(`[data-agent-tags-area="${kind}"][data-agent-name="${name}"]`) as HTMLElement | null;
          if (!chip || !dropdown) return;
          // Switch to allow so the user sees a working picker (deny with
          // no entries selected is empty; allow shows the full pool).
          dropdown.value = "allow";
          chip.style.display = "none";
          dropdown.style.display = "inline-block";
          if (area) area.style.display = "block";
        });
      });
      // Mode change: show/hide the tag selector area, and toggle the
      // chip back in if the user reverts to "inherit".
      card.querySelectorAll<HTMLSelectElement>(".agent-mode-select").forEach(modeSel => {
        modeSel.addEventListener("change", () => {
          const kind = modeSel.dataset.agentMode!;
          const chip = card.querySelector(`.agent-inherit-chip[data-kind="${kind}"][data-agent-name="${name}"]`) as HTMLElement | null;
          const area = card.querySelector(`[data-agent-tags-area="${kind}"][data-agent-name="${name}"]`) as HTMLElement | null;
          if (modeSel.value === "inherit") {
            // Back to the static chip — no restrictions.
            if (chip) chip.style.display = "inline-flex";
            modeSel.style.display = "none";
            if (area) area.style.display = "none";
          } else {
            if (chip) chip.style.display = "none";
            if (area) area.style.display = "block";
          }
        });
      });
      const addTag = (kind: string, value: string) => {
        if (!value) return;
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`) as HTMLElement | null;
        const select = card.querySelector(`[data-agent-select-${kind}="${name}"]`) as HTMLSelectElement | null;
        if (!box || !select || box.querySelector(`[data-value="${esc(value)}"]`)) return;
        const tag = document.createElement("span");
        tag.className = "agent-selected-tag";
        tag.dataset.kind = kind;
        tag.dataset.value = value;
        tag.innerHTML = `${esc(value)}<button type="button" class="agent-selected-remove" data-remove-kind="${kind}" data-remove="${esc(value)}">×</button>`;
        box.appendChild(tag);
        select.querySelector(`option[value="${esc(value)}"]`)?.remove();
        select.value = "";
      };
      const removeTag = (kind: string, value: string) => {
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`) as HTMLElement | null;
        const select = card.querySelector(`[data-agent-select-${kind}="${name}"]`) as HTMLSelectElement | null;
        if (!box || !select) return;
        box.querySelector(`[data-value="${esc(value)}"]`)?.remove();
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        const valueOf = (x: any) => typeof x === "string" ? x : x.id;
        const labelOf = (x: any) => typeof x === "string" ? x : x.label;
        const taken = new Set(Array.from(box.querySelectorAll(".agent-selected-tag")).map(t => (t as HTMLElement).dataset.value!));
        const remaining = all.filter((x) => !taken.has(valueOf(x)));
        select.innerHTML = `<option value="">${addKindLabel(kind)}</option>` + remaining.map((x) => `<option value="${esc(valueOf(x))}">${esc(labelOf(x))}</option>`).join("");
      };
      const selectAll = (kind: string) => {
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        const valueOf = (x: any) => typeof x === "string" ? x : x.id;
        for (const x of all) addTag(kind, valueOf(x));
      };
      const clearAll = (kind: string) => {
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`) as HTMLElement | null;
        const select = card.querySelector(`[data-agent-select-${kind}="${name}"]`) as HTMLSelectElement | null;
        if (!box || !select) return;
        box.innerHTML = "";
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        const valueOf = (x: any) => typeof x === "string" ? x : x.id;
        const labelOf = (x: any) => typeof x === "string" ? x : x.label;
        select.innerHTML = `<option value="">${addKindLabel(kind)}</option>` + all.map((x) => `<option value="${esc(valueOf(x))}">${esc(labelOf(x))}</option>`).join("");
      };
      card.addEventListener("change", (e) => {
        const sel = e.target as HTMLSelectElement;
        if (!sel.classList.contains("settings-select")) return;
        const kind = sel.classList.contains("agent-tools-select") ? "tools" : sel.classList.contains("agent-skills-select") ? "skills" : sel.classList.contains("agent-hooks-select") ? "hooks" : null;
        if (kind) addTag(kind, sel.value);
      });
      card.addEventListener("click", (e) => {
        const btn = (e.target as HTMLElement).closest(".agent-selected-remove") as HTMLElement | null;
        if (btn) {
          const kind = btn.dataset.removeKind!;
          const value = btn.dataset.remove!;
          removeTag(kind, value);
          return;
        }
        const selBtn = (e.target as HTMLElement).closest(".agent-select-all") as HTMLElement | null;
        if (selBtn) {
          const kind = selBtn.dataset.agentSelectAll!;
          selectAll(kind);
          return;
        }
        const clearBtn = (e.target as HTMLElement).closest(".agent-clear-all") as HTMLElement | null;
        if (clearBtn) {
          const kind = clearBtn.dataset.agentClearAll!;
          clearAll(kind);
          return;
        }
      });
    }

    // Build providers HTML for Model tab
    const rawProviders = (cfg.providers || []) as any[];
    const defaultModelName = (cfg.model || {}).name || "";
    let providersHtml = "";
    const normProviders = rawProviders.map((p: any) => ({
      name: p.name || "",
      api_type: p.api_type || "openai_compatible",
      api_key: p.api_key || "",
      base_url: p.base_url || "",
      models: (p.models || []).map((m2: any) => ({ name: m2.name || "", capabilities: { vision: m2.capabilities?.vision ?? true, ...(Array.isArray(m2.capabilities?.effort_levels) ? { effort_levels: m2.capabilities.effort_levels } : {}) } })),
    }));
    for (let pi = 0; pi < normProviders.length; pi++) {
      const p = normProviders[pi];
      const isOpenAI = p.api_type !== "anthropic";
      let modelRows = "";
      for (const model of p.models) {
        const supportsImage = model.capabilities?.vision ?? true;  // default True = vision-capable
        modelRows += `
          <div class="settings-model-row">
            <input class="settings-input s-model-name" value="${esc(model.name)}" placeholder="${i18n.t("settings.modelName")}" style="flex:1" />
            <label class="settings-model-check" title="${i18n.t("settings.visionTitle")}"><input type="checkbox" class="s-model-image" ${supportsImage ? "checked" : ""} /> ${i18n.t("settings.vision")}</label>
            <label class="settings-model-check" title="${i18n.t("settings.defaultModelTitle")}"><input type="radio" name="modelDefault" class="s-model-default" ${model.name === defaultModelName ? "checked" : ""} /> ${i18n.t("settings.default")}</label>
            <select class="settings-input s-model-effort" title="Highest effort this model supports (default = max)">${(() => { const lv = model.capabilities?.effort_levels || []; const top = lv.length ? lv[lv.length - 1] : "max"; return ["low", "medium", "high", "xhigh", "max"].map(o => `<option value="${o}" ${o === top ? "selected" : ""}>${o}</option>`).join(""); })()}</select>
            <button class="settings-hook-remove s-model-remove" title="${i18n.t("common.remove")}">×</button>
          </div>`;
      }
      providersHtml += `
        <div class="settings-provider-card" data-provider-idx="${pi}">
          <div class="settings-provider-card-header">
            <input class="settings-input settings-provider-name" data-field="provider_name" value="${esc(p.name)}" placeholder="${i18n.t("settings.providerName")}" />
            <button class="settings-hook-remove" data-provider-remove title="${i18n.t("settings.removeProvider")}">×</button>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.apiType")}</label>
            <select class="settings-select" data-field="api_type">
              <option value="openai_compatible" ${isOpenAI ? "selected" : ""}>OpenAI Compatible</option>
              <option value="anthropic" ${!isOpenAI ? "selected" : ""}>Anthropic</option>
            </select>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.apiKey")}</label><input class="settings-input" type="password" data-field="api_key" value="${esc(p.api_key)}" /></div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.baseUrl")}</label><input class="settings-input" data-field="base_url" value="${esc(p.base_url)}" placeholder="${i18n.t("settings.baseUrlPlaceholder")}" /></div>
          <div class="settings-section-title" style="margin-top:8px">${i18n.t("settings.models")}</div>
          <div class="settings-provider-models">${modelRows}</div>
          <button class="settings-add-btn s-add-model-btn">${i18n.t("settings.addModel")}</button>
        </div>`;
    }

    body.innerHTML = `
      <div class="settings-layout">
        <div class="settings-tabs">
          <button class="settings-tab active" data-tab="model">${icons.model}<span>${i18n.t("settings.tab.model")}</span></button>
          <button class="settings-tab" data-tab="approval">${icons.approval}<span>${i18n.t("settings.tab.approval")}</span></button>
          <button class="settings-tab" data-tab="mcp">${icons.mcp}<span>${i18n.t("settings.tab.mcp")}</span></button>
          <button class="settings-tab" data-tab="tool">${icons.tool}<span>${i18n.t("settings.tab.tool")}</span></button>
          <button class="settings-tab" data-tab="hooks">${icons.hooks}<span>${i18n.t("settings.tab.hooks")}</span></button>
          <button class="settings-tab" data-tab="memory">${icons.memory}<span>${i18n.t("settings.tab.memory")}</span></button>
          <button class="settings-tab" data-tab="sandbox">${icons.sandbox}<span>${i18n.t("settings.tab.sandbox")}</span></button>
          <button class="settings-tab" data-tab="prompt">${icons.prompt}<span>${i18n.t("settings.tab.prompt")}</span></button>
          <button class="settings-tab" data-tab="agents">${icons.agents}<span>${i18n.t("settings.tab.agents")}</span></button>
        </div>
        <div class="settings-content">
          <!-- Model -->
          <div class="settings-panel active" data-panel="model">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.thinkingMode")}</div>
                <div class="settings-desc">${i18n.t("settings.thinkingModeDesc")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("common.mode")}</label>
                  <select class="settings-select" id="s_thinking_mode">
                    <option value="disabled" ${(cfg.model?.thinking_mode || "disabled") === "disabled" ? "selected" : ""}>${i18n.t("settings.thinking.disabled")}</option>
                    <option value="low" ${(cfg.model?.thinking_mode || "disabled") === "low" ? "selected" : ""}>${i18n.t("settings.thinking.low")}</option>
                    <option value="medium" ${(cfg.model?.thinking_mode || "disabled") === "medium" ? "selected" : ""}>${i18n.t("settings.thinking.medium")}</option>
                    <option value="high" ${(cfg.model?.thinking_mode || "disabled") === "high" ? "selected" : ""}>${i18n.t("settings.thinking.high")}</option>
                    <option value="xhigh" ${(cfg.model?.thinking_mode || "disabled") === "xhigh" ? "selected" : ""}>xhigh</option>
                    <option value="max" ${(cfg.model?.thinking_mode || "disabled") === "max" ? "selected" : ""}>max</option>
                  </select>
                </div>
              </div>
              <div class="settings-section-title" style="margin-top:16px;margin-bottom:8px;">${i18n.t("settings.providers")}</div>
              <div id="sProvidersList">${providersHtml}</div>
              <button class="settings-add-btn" id="addProviderBtn">${i18n.t("settings.addProvider")}</button>
            </div>
          </div>
          <!-- Approval -->
          <div class="settings-panel" data-panel="approval">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.approvalPolicy")}</div>
                <div class="settings-desc">${i18n.t("settings.approvalPolicyDesc")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.policy")}</label>
                  <select class="settings-select" id="s_approval_policy">
                    <option value="suggest" ${ap.policy === "suggest" ? "selected" : ""}>${i18n.t("settings.policy.suggest")}</option>
                    <option value="auto-edit" ${ap.policy === "auto-edit" ? "selected" : ""}>${i18n.t("settings.policy.autoEdit")}</option>
                    <option value="full-auto" ${ap.policy === "full-auto" ? "selected" : ""}>${i18n.t("settings.policy.fullAuto")}</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
          <!-- MCP -->
          <div class="settings-panel" data-panel="mcp">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.mcp")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.mcpEnabled")}</label>
                  <select class="settings-select" id="s_mcp_enabled">
                    <option value="true" ${mcp.enabled ? "selected" : ""}>${i18n.t("common.yes")}</option>
                    <option value="false" ${!mcp.enabled ? "selected" : ""}>${i18n.t("common.no")}</option>
                  </select>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.servers")}</div>
                <div id="mcpServersList">${mcpServersHtml}</div>
                <button class="settings-add-btn" id="addMcpServer">${i18n.t("settings.addMcpServer")}</button>
              </div>
            </div>
          </div>
          <!-- Tool -->
          <div class="settings-panel" data-panel="tool">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.toolSettings")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.maxRounds")}</label><input class="settings-input" type="number" id="s_tool_max_rounds" value="${tool.max_rounds || 0}" /><span style="font-size:12px;color:var(--muted);margin-left:4px">${i18n.t("settings.maxRoundsHint")}</span></div>
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
                <div class="settings-section-title">${i18n.t("settings.memory")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.backend")}</label>
                  <select class="settings-select" id="s_memory_backend">
                    <option value="inmemory" ${mem.backend === "inmemory" || !mem.backend ? "selected" : ""}>${i18n.t("settings.backend.inmemory")}</option>
                  </select>
                </div>
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.contextWindow")}</label><input class="settings-input" type="number" id="s_memory_tokens" value="${mem.context_window_tokens || 200000}" /><span style="font-size:12px;color:var(--muted);margin-left:4px">${i18n.t("settings.tokens")}</span></div>
              </div>
            </div>
          </div>
          <!-- Sandbox -->
          <div class="settings-panel" data-panel="sandbox">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.sandbox")}</div>
                <div class="settings-row"><label class="settings-label">${i18n.t("common.mode")}</label>
                  <select class="settings-select" id="s_sandbox_mode">
                    <option value="off" ${sandbox.mode !== "docker" && sandbox.mode !== "restrictive" ? "selected" : ""}>${i18n.t("settings.sandbox.off")}</option>
                    <option value="docker" ${sandbox.mode === "docker" ? "selected" : ""}>${i18n.t("settings.sandbox.docker")}</option>
                    <option value="restrictive" ${sandbox.mode === "restrictive" ? "selected" : ""}>${i18n.t("settings.sandbox.restrictive")}</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
          <!-- Prompt -->
          <div class="settings-panel" data-panel="prompt">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.promptProfile")}</div>
                <div class="settings-desc">${i18n.t("settings.promptDesc")}</div>
                <textarea class="settings-input settings-textarea" id="s_prompt_system_prompt" rows="20" placeholder="${i18n.t("settings.promptPlaceholder")}">${esc(prompt.system_prompt || "")}</textarea>
              </div>
            </div>
          </div>
          <!-- Agents -->
          <div class="settings-panel" data-panel="agents">
            <div class="settings-panel-inner settings-panel-wide">
              <div class="settings-section">
                <div class="settings-section-title">${i18n.t("settings.subAgents")}</div>
                <div class="settings-desc">${i18n.t("settings.agentsDesc")}</div>
                <div id="agentsList">${agentsHtml || `<div style="color:var(--muted);font-size:12px;padding:12px 0">${i18n.t("settings.noAgents")}</div>`}</div>
                <button class="settings-add-btn" id="addAgentBtn">${i18n.t("settings.addAgent")}</button>
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

    // Hooks panel — fetch registered hooks and render read-only cards + register input
    async function renderHooksPanel() {
      const panel = body.querySelector<HTMLElement>("#hooksPanel");
      if (!panel) return;
      panel.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px 0">${i18n.t("common.loading") || "Loading..."}</div>`;
      try {
        const hooks = await api.listHooks();
        // Tracks which hook ids are toggled off in this UI session. We
        // only persist on Save — the toggles live on the cards until
        // then, mirroring how the rest of the settings modal works.
        const disabledNow = new Set<string>(
          hooks.filter((h: any) => h.enabled === false).map((h: any) => h.id as string),
        );
        let cardsHtml = "";
        for (const h of hooks) {
          const phaseLabel = hookPhaseLabel[h.event_name] || h.event_name;
          // 后端用 "builtin" 或原始路径区分 source；非 builtin 时显示
          // 真实路径让用户知道 hook 是从哪里加载的。
          const sourceLabel = h.source === "builtin"
            ? (i18n.t("settings.hookSourceBuiltin") || "Built-in")
            : h.source;
          const typeLabel = h.type === "shell" ? "Shell" : "Python";
          const isEnabled = !disabledNow.has(h.id);
          cardsHtml += `
            <div class="settings-hook-card" data-hook-id="${esc(h.id)}" style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;opacity:${isEnabled ? "1" : "0.5"}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;gap:12px">
                <span style="font-weight:600;font-size:13px">${esc(h.name)}</span>
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:11px;color:var(--muted)">${esc(sourceLabel)} · ${typeLabel}</span>
                  <label class="settings-toggle" style="font-size:11px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer">
                    <input type="checkbox" class="hook-enable-toggle" data-hook-toggle="${esc(h.id)}" ${isEnabled ? "checked" : ""} />
                    ${i18n.t("settings.enabled") || "Enabled"}
                  </label>
                </div>
              </div>
              <div style="font-size:12px;color:var(--muted)">
                ${i18n.t("settings.event")}: ${esc(phaseLabel)} ·
                ${i18n.t("settings.matcher")}: ${esc(h.matcher || "—")}
                ${h.block !== null ? ` · ${h.block ? "☑" : "☐"} ${i18n.t("settings.block")}` : ""}
                ${h.timeout !== null ? ` · ${i18n.t("settings.timeout")}: ${h.timeout === 0 ? i18n.t("settings.noTimeout") || "no limit" : h.timeout + "s"}` : ""}
                ${h.async_run !== null ? ` · ${h.async_run ? "☑" : "☐"} ${i18n.t("settings.async")}` : ""}
              </div>
            </div>`;
        }
        if (!cardsHtml) {
          cardsHtml = `<div style="color:var(--muted);font-size:12px;padding:8px 0">${i18n.t("settings.noHooks") || "No hooks registered."}</div>`;
        }
        panel.innerHTML = `
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.registeredHooks") || "Registered Hooks"}</div>
            ${cardsHtml}
          </div>
          <div class="settings-section" style="margin-top:16px">
            <div class="settings-section-title">${i18n.t("settings.addHook") || "Add Hook"}</div>
            <div class="settings-desc">${i18n.t("settings.addHookDesc") || "Enter a folder path containing manifest.yaml (+ impl.sh or impl.py)"}</div>
            <div class="settings-row" style="gap:8px">
              <input class="settings-input" id="s_hook_folder_path" placeholder="~/.ziva/hooks/my-hook/" style="flex:1" />
              <button class="settings-add-btn" id="s_hook_register_btn" style="white-space:nowrap">${i18n.t("settings.register") || "Register"}</button>
            </div>
          </div>`;
        // Wire toggle switches — update visual state immediately; the
        // actual save happens when the user clicks the global Save
        // button (alongside the other sections).
        panel.querySelectorAll<HTMLInputElement>(".hook-enable-toggle").forEach(t => {
          t.addEventListener("change", () => {
            const id = t.dataset.hookToggle!;
            const card = panel.querySelector<HTMLElement>(`.settings-hook-card[data-hook-id="${id}"]`);
            if (!card) return;
            if (t.checked) {
              disabledNow.delete(id);
              card.style.opacity = "1";
            } else {
              disabledNow.add(id);
              card.style.opacity = "0.5";
            }
          });
        });
        const registerBtn = panel.querySelector<HTMLElement>("#s_hook_register_btn");
        if (registerBtn) {
          registerBtn.onclick = async () => {
            const input = panel.querySelector<HTMLInputElement>("#s_hook_folder_path");
            const folderPath = input?.value?.trim();
            if (!folderPath) return;
            registerBtn.textContent = "...";
            try {
              await api.registerHook(folderPath);
              input!.value = "";
              await renderHooksPanel();
            } catch (e) {
              alert((e as Error).message);
              registerBtn.textContent = i18n.t("settings.register") || "Register";
            }
          };
        }
        // Stash the in-progress disable set on the panel element so the
        // global Save handler can pick it up.
        (panel as any).__disabledHooks = disabledNow;
      } catch (e) {
        panel.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px 0">${(e as Error).message}</div>`;
      }
    }
    renderHooksPanel();

    // MCP server remove
    body.querySelectorAll<HTMLButtonElement>("[data-mcp-remove]").forEach(btn => {
      btn.onclick = () => (btn.closest(".settings-mcp-card") as HTMLElement)?.remove();
    });

    // Agent card remove
    body.querySelectorAll<HTMLButtonElement>("[data-agent-remove]").forEach(btn => {
      btn.onclick = () => (btn.closest(".settings-agent-card") as HTMLElement)?.remove();
    });

    // Wire dropdown + removable-tag UX for every existing agent card.
    body.querySelectorAll<HTMLElement>(".settings-agent-card").forEach(card => {
      const name = card.dataset.agentName!;
      if (name) wireAgentSelections(card, name);
    });

    // Add agent button — spawn an empty card the user can fill in
    const addAgentBtn = body.querySelector("#addAgentBtn") as HTMLButtonElement | null;
    if (addAgentBtn) {
        addAgentBtn.onclick = () => {
        const list = body.querySelector("#agentsList")!;
        // Find a non-colliding placeholder name
        let idx = 1;
        while (list.querySelector(`[data-agent-name="new_agent_${idx}"]`)) idx++;
        const n = `new_agent_${idx}`;
        // Build dimension section inline (mirrors buildDimension from agentsHtml)
        const modeOpts = [
          { v: "inherit", l: i18n.t("settings.modeInherit") || "Inherit all" },
          { v: "allow", l: i18n.t("settings.modeAllow") || "Allow specific" },
          { v: "deny", l: i18n.t("settings.modeDeny") || "Deny specific" },
        ].map(o => `<option value="${o.v}">${o.l}</option>`).join("");
        const buildNewDim = (kind: string, label: string, desc: string, all: Array<string | { id: string; label: string }>) => {
          const valueOf = (x: string | { id: string; label: string }) => typeof x === "string" ? x : x.id;
          const labelOf = (x: string | { id: string; label: string }) => typeof x === "string" ? x : x.label;
          const options = all.map((x) => `<option value="${esc(valueOf(x))}">${esc(labelOf(x))}</option>`).join("");
          // Mirror buildDimension: default to the static "Inherit all"
          // chip with a Customize link. Only when the user clicks
          // Customize does the dropdown + tag picker become visible.
          const inheritChip = `
            <span class="agent-inherit-chip" data-kind="${kind}" data-agent-name="${esc(n)}"
                  style="display:inline-flex;align-items:center;gap:6px;margin-left:8px;font-size:11px;color:var(--muted);background:var(--surface-alt);padding:3px 8px;border-radius:4px">
              <span style="font-weight:500">${i18n.t("settings.modeInherit") || "Inherit all"}</span>
              <button type="button" class="agent-customize-btn" data-customize-kind="${kind}" data-agent-name="${esc(n)}"
                      style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:11px;padding:0;text-decoration:underline">
                ${i18n.t("settings.customize") || "Customize…"}
              </button>
            </span>`;
          const modeSelect = `<select class="settings-select agent-mode-select" data-agent-mode="${kind}" data-agent-name="${esc(n)}" style="display:none;margin-left:8px;font-size:11px;width:auto;padding:2px 6px"><option value="inherit" selected>${i18n.t("settings.modeInherit") || "Inherit all"}</option><option value="allow">${i18n.t("settings.modeAllow") || "Allow specific"}</option><option value="deny">${i18n.t("settings.modeDeny") || "Deny specific"}</option></select>`;
          return `
            <div class="settings-section">
              <div class="settings-section-title">${label}${inheritChip}${modeSelect}</div>
              <div class="settings-desc">${desc}</div>
              <div class="agent-tags-area" data-agent-tags-area="${kind}" data-agent-name="${esc(n)}" style="display:none">
                <div class="settings-row" style="margin:6px 0;gap:8px">
                  <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="${kind}" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
                  <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="${kind}" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
                </div>
                <select class="settings-select agent-${kind}-select" data-agent-select-${kind}="${esc(n)}"><option value="">${addKindLabel(kind)}</option>${options}</select>
                <div class="agent-selected-box" data-agent-box-${kind}="${esc(n)}"></div>
              </div>
            </div>`;
        };
        const card = document.createElement("div");
        card.className = "settings-agent-card";
        card.dataset.agentName = n;
        card.innerHTML = `
          <div class="settings-agent-card-header">
            <input class="settings-input settings-agent-name" data-agent-rename="${esc(n)}" value="${esc(n)}" placeholder="${i18n.t("settings.agentNamePlaceholder")}" style="font-weight:600;font-size:13px" />
            <label class="agent-bg-label"><input type="checkbox" data-agent-bg="${esc(n)}" /> ${i18n.t("settings.background")}</label>
            <button class="settings-hook-remove" data-agent-remove="${esc(n)}" title="${i18n.t("settings.removeAgent")}">×</button>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.agentDescription") || "Description"}</div>
            <div class="settings-desc">${i18n.t("settings.agentDescriptionHint") || "One-line summary shown to the parent agent in the spawn_agent tool description."}</div>
            <input class="settings-input settings-agent-description" data-agent-description="${esc(n)}" value="" placeholder="${i18n.t("settings.agentDescriptionPlaceholder") || "e.g. Read-only investigation agent"}" />
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.instructions")}</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(n)}" rows="8" placeholder="${i18n.t("settings.instructionsPlaceholder")}"></textarea>
          </div>
          ${buildNewDim("tools", i18n.t("settings.tools"), i18n.t("settings.toolsDesc"), allToolNames)}
          ${buildNewDim("skills", i18n.t("settings.skills"), i18n.t("settings.skillsDesc"), allSkillNames)}
          ${buildNewDim("hooks", i18n.t("settings.hooks"), i18n.t("settings.agentHooksDesc"), hookTypes)}
        `;
        // Wire the new card's remove button and selections
        const removeBtn = card.querySelector("[data-agent-remove]") as HTMLButtonElement;
        removeBtn.onclick = () => card.remove();
        wireAgentSelections(card, n);
        list.appendChild(card);
        // Focus the name field for quick rename
        (card.querySelector(`[data-agent-rename="${n}"]`) as HTMLInputElement)?.focus();
        // Clear the "no agents" placeholder if it was showing
        const empty = list.querySelector("div[style*='var(--muted)']");
        if (empty) empty.remove();
      };
    }

    // Provider card management
    function wireProviderCardEvents(card: HTMLElement) {
      // Remove provider
      const removeBtn = card.querySelector("[data-provider-remove]") as HTMLElement | null;
      if (removeBtn) removeBtn.onclick = () => card.remove();

      // Model rows: remove + default radio
      card.querySelectorAll(".s-model-remove").forEach((btn) => {
        (btn as HTMLElement).onclick = () => (btn as HTMLElement).closest(".settings-model-row")!.remove();
      });
      card.querySelectorAll(".s-model-default").forEach((radio) => {
        (radio as HTMLInputElement).onchange = () => {
          body.querySelectorAll(".s-model-default").forEach((r) => {
            if (r !== radio) (r as HTMLInputElement).checked = false;
          });
        };
      });

      // Add model button
      const addModelBtn = card.querySelector(".s-add-model-btn") as HTMLElement | null;
      if (addModelBtn) {
        addModelBtn.onclick = () => {
          const modelsDiv = card.querySelector(".settings-provider-models")!;
          const row = document.createElement("div");
          row.className = "settings-model-row";
          row.innerHTML = `
            <input class="settings-input s-model-name" value="" placeholder="${i18n.t("settings.modelName")}" style="flex:1" />
            <label class="settings-model-check"><input type="checkbox" class="s-model-image" /> ${i18n.t("settings.image")}</label>
            <label class="settings-model-check"><input type="radio" name="modelDefault" class="s-model-default" /> ${i18n.t("settings.default")}</label>
            <select class="settings-input s-model-effort" title="Highest effort this model supports (default = max)">${["low", "medium", "high", "xhigh", "max"].map(o => `<option value="${o}" ${o === "max" ? "selected" : ""}>${o}</option>`).join("")}</select>
            <button class="settings-hook-remove s-model-remove" title="${i18n.t("common.remove")}">×</button>`;
          (row.querySelector(".s-model-remove") as HTMLElement).onclick = () => row.remove();
          (row.querySelector(".s-model-default") as HTMLElement).onchange = () => {
            body.querySelectorAll(".s-model-default").forEach((r) => {
              if (r !== row.querySelector(".s-model-default")) (r as HTMLInputElement).checked = false;
            });
          };
          modelsDiv.appendChild(row);
          row.querySelector("input")?.focus();
        };
      }
    }

    body.querySelectorAll(".settings-provider-card").forEach(card => {
      wireProviderCardEvents(card as HTMLElement);
    });

    // Add provider
    const addProviderBtn = body.querySelector("#addProviderBtn") as HTMLElement;
    if (addProviderBtn) {
      addProviderBtn.onclick = () => {
        const list = body.querySelector("#sProvidersList")!;
        const card = document.createElement("div");
        card.className = "settings-provider-card";
        card.dataset.providerIdx = String(list.children.length);
        card.innerHTML = `
          <div class="settings-provider-card-header">
            <input class="settings-input settings-provider-name" data-field="provider_name" value="" placeholder="${i18n.t("settings.providerName")}" />
            <button class="settings-hook-remove" data-provider-remove title="${i18n.t("settings.removeProvider")}">×</button>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.apiType")}</label>
            <select class="settings-select" data-field="api_type">
              <option value="openai_compatible" selected>OpenAI Compatible</option>
              <option value="anthropic">Anthropic</option>
            </select>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.apiKey")}</label><input class="settings-input" type="password" data-field="api_key" value="" /></div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.baseUrl")}</label><input class="settings-input" data-field="base_url" value="" placeholder="${i18n.t("settings.baseUrlPlaceholder")}" /></div>
          <div class="settings-section-title" style="margin-top:8px">${i18n.t("settings.models")}</div>
          <div class="settings-provider-models"></div>
          <button class="settings-add-btn s-add-model-btn">${i18n.t("settings.addModel")}</button>`;
        wireProviderCardEvents(card);
        list.appendChild(card);
        card.querySelector("input")?.focus();
      };
    }

    // MCP add server — inline card, no prompt() dialog
    const addBtn = body.querySelector("#addMcpServer") as HTMLElement;
    if (addBtn) {
      addBtn.onclick = () => {
        const name = "server-" + Date.now().toString(36);
        const list = body.querySelector("#mcpServersList")!;
        const card = document.createElement("div");
        card.className = "settings-mcp-card";
        card.dataset.mcpServer = name;
        card.innerHTML = `
          <div class="settings-mcp-card-header">
            <input class="settings-input settings-mcp-name" data-mcp-name="${esc(name)}" value="${esc(name)}" placeholder="${i18n.t("settings.serverName")}" style="font-weight:600;font-size:13px" />
            <div>
              <select class="settings-select" style="width:auto;padding:4px 8px;font-size:12px" data-mcp-enabled="${esc(name)}">
                <option value="true" selected>${i18n.t("common.enabled")}</option>
                <option value="false">${i18n.t("common.disabled")}</option>
              </select>
              <button class="settings-hook-remove" data-mcp-remove="${esc(name)}" title="${i18n.t("common.remove")}">×</button>
            </div>
          </div>
          <div class="settings-row"><label class="settings-label">${i18n.t("settings.command")}</label><input class="settings-input" data-mcp-command="${esc(name)}" value="" placeholder="${i18n.t("settings.commandPlaceholder")}" /></div>
          <div class="settings-row"><label class="settings-label">${i18n.t("common.type")}</label>
            <select class="settings-select" data-mcp-type="${esc(name)}">
              <option value="local" selected>${i18n.t("settings.mcpTypeLocal")}</option>
              <option value="remote">${i18n.t("settings.mcpTypeRemote")}</option>
            </select>
          </div>`;
        (card.querySelector(".settings-hook-remove") as HTMLElement).onclick = () => card.remove();
        list.appendChild(card);
        card.querySelector("input")?.focus();
      };
    }

    // Save
    (backdrop.querySelector("#settingsSaveBtn") as HTMLElement).onclick = async () => {
      const btn = backdrop.querySelector("#settingsSaveBtn") as HTMLElement;
      btn.textContent = i18n.t("settings.saving");
      btn.setAttribute("disabled", "true");
      try {
        const updated = { ...cfg };

        // Model — collect from provider cards
        const newProviders: any[] = [];
        let defaultName = "";
        backdrop.querySelectorAll(".settings-provider-card").forEach(card => {
          const pName = (card.querySelector("[data-field='provider_name']") as HTMLInputElement)?.value.trim() || "";
          const apiType = (card.querySelector("[data-field='api_type']") as HTMLSelectElement)?.value || "openai_compatible";
          const apiKey = (card.querySelector("[data-field='api_key']") as HTMLInputElement)?.value || "";
          const baseUrl = (card.querySelector("[data-field='base_url']") as HTMLInputElement)?.value || "";
          const models: Array<any> = [];
          card.querySelectorAll(".settings-model-row").forEach(row => {
            const name = (row.querySelector(".s-model-name") as HTMLInputElement)?.value.trim() || "";
            if (!name) return;
            const vision = (row.querySelector(".s-model-image") as HTMLInputElement)?.checked ?? true;
            const caps: any = { vision };
            const effortTop = (row.querySelector(".s-model-effort") as HTMLSelectElement)?.value || "max";
            const ORDER = ["low", "medium", "high", "xhigh", "max"];
            // max is the default — leave effort_levels unset so the runtime
            // default (full list) applies and the config stays minimal. Only
            // persist an explicit cap for models that cap below max.
            if (effortTop !== "max" && ORDER.includes(effortTop)) {
              caps.effort_levels = ORDER.slice(0, ORDER.indexOf(effortTop) + 1);
            }
            models.push({ name, capabilities: caps });
            if ((row.querySelector(".s-model-default") as HTMLInputElement)?.checked) defaultName = name;
          });
          if (models.length > 0) {
            newProviders.push({ name: pName, api_type: apiType, api_key: apiKey, base_url: baseUrl, models });
          }
        });
        if (!defaultName && newProviders.length > 0 && newProviders[0].models.length > 0) {
          defaultName = newProviders[0].models[0].name;
        }
        updated.providers = newProviders;
        const tm = (backdrop.querySelector("#s_thinking_mode") as HTMLSelectElement)?.value || "disabled";
        updated.model = { name: defaultName || "", thinking_mode: tm };

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
          const nameInput = card.querySelector(`[data-mcp-name="${sname}"]`) as HTMLInputElement | null;
          const displayName = (nameInput?.value?.trim()) || sname;
          const cmdStr = (card.querySelector(`[data-mcp-command="${sname}"]`) as HTMLInputElement)?.value || "";
          const srvEnabled = (card.querySelector(`[data-mcp-enabled="${sname}"]`) as HTMLSelectElement)?.value !== "false";
          const srvType = (card.querySelector(`[data-mcp-type="${sname}"]`) as HTMLSelectElement)?.value || "local";
          const existing = mcpServers[sname] || {};
          newMcpServers[displayName] = {
            ...existing,
            type: srvType,
            command: cmdStr,
            enabled: srvEnabled,
          };
        });
        updated.mcp = { ...updated.mcp, enabled: mcpEnabled, servers: newMcpServers };

        // Sandbox
        updated.sandbox = { ...updated.sandbox, mode: (backdrop.querySelector("#s_sandbox_mode") as HTMLSelectElement).value };

        // Prompt
        updated.prompt = { ...updated.prompt, system_prompt: (backdrop.querySelector("#s_prompt_system_prompt") as HTMLTextAreaElement).value };

        // Agents — rebuild from DOM. Each dimension uses three-state mode:
        // inherit (omit key) / allow (store `tools`) / deny (store `deny_tools`).
        const newAgents: Record<string, any> = {};
        backdrop.querySelectorAll<HTMLElement>(".settings-agent-card").forEach(card => {
          const origName = card.dataset.agentName!;
          const renameInput = card.querySelector(`[data-agent-rename="${origName}"]`) as HTMLInputElement;
          const newName = (renameInput?.value?.trim()) || origName;
          const instr = ((card.querySelector(`[data-agent-instructions="${origName}"]`) as HTMLTextAreaElement)?.value || "").trim();
          const desc = ((card.querySelector(`[data-agent-description="${origName}"]`) as HTMLInputElement)?.value || "").trim();
          const bg = !!(card.querySelector(`input[data-agent-bg="${origName}"]`) as HTMLInputElement)?.checked;
          const agentObj: Record<string, any> = { instructions: instr, background: bg };
          if (desc) agentObj.description = desc;
          // For each kind, read the mode selector and store accordingly.
          // Three-state: inherit / allow / deny. The "irrelevant" key for the
          // current mode is sent as ``null`` so the server's deep-merge deletes
          // it from disk; otherwise stale allow/deny lists would silently round
          // trip back from disk the next time the modal opens.
          for (const kind of ["tools", "skills", "hooks"] as const) {
            const modeSel = card.querySelector(`select.agent-mode-select[data-agent-mode="${kind}"]`) as HTMLSelectElement | null;
            const mode = modeSel?.value || "inherit";
            const selected = Array.from(card.querySelectorAll<HTMLElement>(`.agent-selected-tag[data-kind="${kind}"]`)).map(c => c.dataset.value!);
            const denyKey = kind === "tools" ? "deny_tools" : kind === "skills" ? "deny_skills" : "deny_hooks";
            if (mode === "allow") {
              agentObj[kind] = selected;
              agentObj[denyKey] = null;
            } else if (mode === "deny") {
              agentObj[denyKey] = selected;
              agentObj[kind] = null;
            } else {
              // inherit: explicitly delete both keys from disk
              agentObj[kind] = null;
              agentObj[denyKey] = null;
            }
          }
          newAgents[newName] = agentObj;
        });

        updated.agents = newAgents;

        // Hook disable list — collected from the toggles on the hooks
        // panel. Empty list means "all enabled"; explicit list lets the
        // user mute a hook globally without removing its folder from
        // ``plugin.paths``.
        const hooksPanel = body.querySelector<HTMLElement>("#hooksPanel");
        const disabledSet: Set<string> = (hooksPanel as any)?.__disabledHooks || new Set();
        updated.hooks = { ...(updated.hooks || {}), disabled: Array.from(disabledSet) };

        // Remove skill_index metadata before saving
        delete (updated as any)._skill_index;

        await api.saveConfigJson(updated);
        await _refreshConfig();
        renderSessions();
        btn.textContent = i18n.t("settings.saved");
        setTimeout(() => { btn.textContent = i18n.t("settings.save"); btn.removeAttribute("disabled"); }, 1500);
      } catch (e) {
        btn.textContent = i18n.t("settings.error");
        alert((e as Error).message);
        setTimeout(() => { btn.textContent = i18n.t("settings.save"); btn.removeAttribute("disabled"); }, 1500);
      }
    };
  } catch (e) {
    body.innerHTML = `<div class="skills-modal-error">${i18n.t("settings.loadFailed", { err: (e as Error).message })}</div>`;
  }
}
