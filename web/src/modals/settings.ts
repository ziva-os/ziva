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

    // Build hooks HTML per hook type
    const hookTypes = ["before_turn", "after_turn", "before_tool", "after_tool"];
    const hookPhaseLabel: Record<string, string> = {
      before_turn: i18n.t("settings.hookPhase.beforeTurn"),
      after_turn: i18n.t("settings.hookPhase.afterTurn"),
      before_tool: i18n.t("settings.hookPhase.beforeTool"),
      after_tool: i18n.t("settings.hookPhase.afterTool"),
    };
    let hooksHtml = "";
    for (const ht of hookTypes) {
      const items: string[] = hooks[ht] || [];
      let rows = "";
      for (let i = 0; i < items.length; i++) {
        rows += `<div class="settings-hook-row"><input class="settings-input" data-hook="${ht}" data-hook-idx="${i}" value="${esc(items[i])}" /><button class="settings-hook-remove" data-hook-remove="${ht}:${i}" title="Remove">×</button></div>`;
      }
      hooksHtml += `
        <div class="settings-section">
          <div class="settings-section-title">${hookPhaseLabel[ht] || ht}</div>
          <div class="settings-desc">${i18n.t("settings.hooksDesc", { phase: hookPhaseLabel[ht] || ht })}</div>
          <div data-hook-list="${ht}">${rows}</div>
          <button class="settings-add-btn" data-hook-add="${ht}">${i18n.t("settings.addCommand")}</button>
        </div>`;
    }

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
      const agentTools: string[] = def.tools || [];
      const agentSkills: string[] = def.skills || [];
      const agentHooks: string[] = def.hooks || [];
      const background = !!def.background;
      // Build dropdown + removable selected-tag boxes for tools/skills/hooks.
      const buildSelect = (cls: string, kind: string, all: string[], selected: string[]) => {
        const options = all.filter((x) => !selected.includes(x)).map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
        return `<select class="settings-select ${cls}" data-agent-select-${kind}="${esc(name)}"><option value="">${addKindLabel(kind)}</option>${options}</select>`;
      };
      const buildBox = (kind: string, selected: string[]) => {
        const tags = selected.map((x) => `<span class="agent-selected-tag" data-kind="${kind}" data-value="${esc(x)}">${esc(x)}<button type="button" class="agent-selected-remove" data-remove-kind="${kind}" data-remove="${esc(x)}">×</button></span>`).join("");
        return `<div class="agent-selected-box" data-agent-box-${kind}="${esc(name)}">${tags}</div>`;
      };
      const toolSelect = buildSelect("agent-tools-select", "tools", allToolNames, agentTools);
      const toolBox = buildBox("tools", agentTools);
      const skillSelect = buildSelect("agent-skills-select", "skills", allSkillNames, agentSkills);
      const skillBox = buildBox("skills", agentSkills);
      const hookSelect = buildSelect("agent-hooks-select", "hooks", hookTypes, agentHooks);
      const hookBox = buildBox("hooks", agentHooks);
      return `
        <div class="settings-agent-card" data-agent-name="${esc(name)}">
          <div class="settings-agent-card-header">
            <input class="settings-input settings-agent-name" data-agent-rename="${esc(name)}" value="${esc(name)}" placeholder="${i18n.t("settings.agentNameExample")}" style="font-weight:600;font-size:13px" />
            <label class="agent-bg-label"><input type="checkbox" data-agent-bg="${esc(name)}" ${background ? "checked" : ""} /> ${i18n.t("settings.background")}</label>
            <button class="settings-hook-remove" data-agent-remove="${esc(name)}" title="${i18n.t("settings.removeAgent")}">×</button>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.instructions")}</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(name)}" rows="8" placeholder="${i18n.t("settings.instructionsPlaceholder")}">${esc(instructions)}</textarea>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.tools")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: agentTools.length })}</span></div>
            <div class="settings-desc">${i18n.t("settings.toolsDesc")}</div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="tools" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="tools" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            ${toolSelect}
            ${toolBox}
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.skills")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: agentSkills.length })}</span></div>
            <div class="settings-desc">${i18n.t("settings.skillsDesc")}</div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="skills" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="skills" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            ${skillSelect}
            ${skillBox}
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.hooks")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: agentHooks.length })}</span></div>
            <div class="settings-desc">${i18n.t("settings.agentHooksDesc")}</div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="hooks" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="hooks" data-agent-name="${esc(name)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            ${hookSelect}
            ${hookBox}
          </div>
        </div>`;
    }).join("");

    // Wire the dropdown + removable tag UX for a single agent card.
    function wireAgentSelections(card: HTMLElement, name: string) {
      const updateCount = (kind: string) => {
        const count = card.querySelectorAll(`.agent-selected-tag[data-kind="${kind}"]`).length;
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`);
        const titleSpan = box?.parentElement?.querySelector(".settings-section-title span");
        if (titleSpan) titleSpan.textContent = i18n.t("settings.nSelected", { n: count });
      };
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
        updateCount(kind);
      };
      const removeTag = (kind: string, value: string) => {
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`) as HTMLElement | null;
        const select = card.querySelector(`[data-agent-select-${kind}="${name}"]`) as HTMLSelectElement | null;
        if (!box || !select) return;
        box.querySelector(`[data-value="${esc(value)}"]`)?.remove();
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        const remaining = all.filter((x) => !Array.from(box.querySelectorAll(".agent-selected-tag")).map(t => (t as HTMLElement).dataset.value).includes(x));
        select.innerHTML = `<option value="">${addKindLabel(kind)}</option>` + remaining.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
        updateCount(kind);
      };
      const selectAll = (kind: string) => {
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        for (const x of all) addTag(kind, x);
      };
      const clearAll = (kind: string) => {
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`) as HTMLElement | null;
        const select = card.querySelector(`[data-agent-select-${kind}="${name}"]`) as HTMLSelectElement | null;
        if (!box || !select) return;
        box.innerHTML = "";
        const all = kind === "tools" ? allToolNames : kind === "skills" ? allSkillNames : hookTypes;
        select.innerHTML = `<option value="">${addKindLabel(kind)}</option>` + all.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
        updateCount(kind);
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
      models: (p.models || []).map((m2: any) => ({ name: m2.name || "", capabilities: { vision: m2.capabilities?.vision ?? true } })),
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
                <div class="settings-row"><label class="settings-label">${i18n.t("settings.profile")}</label>
                  <select class="settings-select" id="s_prompt_profile">
                    <option value="default" ${prompt.profile === "default" || !prompt.profile ? "selected" : ""}>${i18n.t("settings.profile.default")}</option>
                    <option value="concise" ${prompt.profile === "concise" ? "selected" : ""}>${i18n.t("settings.profile.concise")}</option>
                    <option value="detailed" ${prompt.profile === "detailed" ? "selected" : ""}>${i18n.t("settings.profile.detailed")}</option>
                    <option value="" ${!["default","concise","detailed"].includes(prompt.profile) && prompt.profile ? "selected" : ""}>${i18n.t("settings.profile.custom")}</option>
                  </select>
                </div>
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

    // Hook add/remove
    body.querySelectorAll<HTMLButtonElement>("[data-hook-add]").forEach(btn => {
      btn.onclick = () => {
        const ht = btn.dataset.hookAdd!;
        const list = body.querySelector(`[data-hook-list="${ht}"]`)!;
        const idx = list.children.length;
        const row = document.createElement("div");
        row.className = "settings-hook-row";
        row.innerHTML = `<input class="settings-input" data-hook="${ht}" data-hook-idx="${idx}" value="" placeholder="${i18n.t("settings.hookPlaceholder")}" /><button class="settings-hook-remove" data-hook-remove="${ht}:${idx}" title="${i18n.t("common.remove")}">×</button>`;
        (row.querySelector(".settings-hook-remove") as HTMLElement | null)!.onclick = () => row.remove();
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
        const toolOptions = allToolNames.map((tn) => `<option value="${esc(tn)}">${esc(tn)}</option>`).join("");
        const skillOptions = allSkillNames.map((sn) => `<option value="${esc(sn)}">${esc(sn)}</option>`).join("");
        const hookOptions = hookTypes.map((hk) => `<option value="${esc(hk)}">${esc(hk)}</option>`).join("");
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
            <div class="settings-section-title">${i18n.t("settings.instructions")}</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(n)}" rows="8" placeholder="${i18n.t("settings.instructionsPlaceholder")}"></textarea>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.tools")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: 0 })}</span></div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="tools" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="tools" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            <select class="settings-select agent-tools-select" data-agent-select-tools="${esc(n)}"><option value="">${i18n.t("settings.addTools")}</option>${toolOptions}</select>
            <div class="agent-selected-box" data-agent-box-tools="${esc(n)}"></div>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.skills")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: 0 })}</span></div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="skills" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="skills" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            <select class="settings-select agent-skills-select" data-agent-select-skills="${esc(n)}"><option value="">${i18n.t("settings.addSkills")}</option>${skillOptions}</select>
            <div class="agent-selected-box" data-agent-box-skills="${esc(n)}"></div>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">${i18n.t("settings.hooks")} <span style="color:var(--muted);font-weight:400;font-size:11px">${i18n.t("settings.nSelected", { n: 0 })}</span></div>
            <div class="settings-row" style="margin:6px 0;gap:8px">
              <button type="button" class="settings-add-btn agent-select-all" data-agent-select-all="hooks" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.selectAll")}</button>
              <button type="button" class="settings-add-btn agent-clear-all" data-agent-clear-all="hooks" data-agent-name="${esc(n)}" style="padding:3px 10px">${i18n.t("settings.clear")}</button>
            </div>
            <select class="settings-select agent-hooks-select" data-agent-select-hooks="${esc(n)}"><option value="">${i18n.t("settings.addHooks")}</option>${hookOptions}</select>
             <div class="agent-selected-box" data-agent-box-hooks="${esc(n)}"></div>
          </div>`;
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
          const models: Array<{ name: string; capabilities: { vision: boolean } }> = [];
          card.querySelectorAll(".settings-model-row").forEach(row => {
            const name = (row.querySelector(".s-model-name") as HTMLInputElement)?.value.trim() || "";
            if (!name) return;
            const vision = (row.querySelector(".s-model-image") as HTMLInputElement)?.checked ?? true;
            models.push({ name, capabilities: { vision } });
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
        updated.prompt = { ...updated.prompt, profile: (backdrop.querySelector("#s_prompt_profile") as HTMLSelectElement).value };

        // Agents — rebuild from DOM. Each card has: name (key),
        // instructions, tools[], skills[], background, memory.
        const newAgents: Record<string, any> = {};
        backdrop.querySelectorAll<HTMLElement>(".settings-agent-card").forEach(card => {
          const origName = card.dataset.agentName!;
          const renameInput = card.querySelector(`[data-agent-rename="${origName}"]`) as HTMLInputElement;
          const newName = (renameInput?.value?.trim()) || origName;
          const instr = ((card.querySelector(`[data-agent-instructions="${origName}"]`) as HTMLTextAreaElement)?.value || "").trim();
          const tools = Array.from(card.querySelectorAll<HTMLElement>(`.agent-selected-tag[data-kind="tools"]`)).map(c => c.dataset.value!);
          const skills = Array.from(card.querySelectorAll<HTMLElement>(`.agent-selected-tag[data-kind="skills"]`)).map(c => c.dataset.value!);
          const hooks = Array.from(card.querySelectorAll<HTMLElement>(`.agent-selected-tag[data-kind="hooks"]`)).map(c => c.dataset.value!);
          const bg = !!(card.querySelector(`input[data-agent-bg="${origName}"]`) as HTMLInputElement)?.checked;
          newAgents[newName] = {
            instructions: instr,
            tools,
            skills,
            hooks,
            background: bg,
          };
        });
        updated.agents = newAgents;

        // Hooks — rebuild from DOM
        const newHooks: Record<string, string[]> = {};
        for (const ht of hookTypes) {
          newHooks[ht] = Array.from(backdrop.querySelectorAll<HTMLInputElement>(`[data-hook="${ht}"]`)).map(i => i.value).filter(Boolean);
        }
        updated.hooks = newHooks;

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
