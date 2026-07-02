/** Settings modal — extracted verbatim from main.ts (one large async fn). */

import * as api from "../api";
import { esc } from "../dom";
import { closeAllFullpageOverlays } from "../modals";

// refreshConfig() lives in main.ts; injected at init to avoid a circular import.
let _refreshConfig: () => Promise<void> = async () => {};
export function setSettingsDeps(opts: { refreshConfig: () => Promise<void> }): void {
  _refreshConfig = opts.refreshConfig;
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
            <input class="settings-input settings-mcp-name" data-mcp-name="${esc(sname)}" value="${esc(sname)}" placeholder="Server name" style="font-weight:600;font-size:13px" />
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
      const memory = def.memory || "inherited";
      // Build dropdown + removable selected-tag boxes for tools/skills/hooks.
      const buildSelect = (cls: string, kind: string, all: string[], selected: string[]) => {
        const options = all.filter((x) => !selected.includes(x)).map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
        return `<select class="settings-select ${cls}" data-agent-select-${kind}="${esc(name)}"><option value="">Add ${kind}...</option>${options}</select>`;
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
            <input class="settings-input settings-agent-name" data-agent-rename="${esc(name)}" value="${esc(name)}" placeholder="agent name (e.g. explore)" style="font-weight:600;font-size:13px" />
            <label class="agent-bg-label"><input type="checkbox" data-agent-bg="${esc(name)}" ${background ? "checked" : ""} /> background</label>
            <button class="settings-hook-remove" data-agent-remove="${esc(name)}" title="Remove agent">×</button>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Instructions</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(name)}" rows="8" placeholder="System prompt for the sub-agent">${esc(instructions)}</textarea>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Tools <span style="color:var(--muted);font-weight:400;font-size:11px">(${agentTools.length} selected)</span></div>
            <div class="settings-desc">Whitelist of tools the sub-agent can call. Empty = inherit all tools except spawn_agent.</div>
            ${toolSelect}
            ${toolBox}
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Skills <span style="color:var(--muted);font-weight:400;font-size:11px">(${agentSkills.length} selected)</span></div>
            <div class="settings-desc">Skills available to the sub-agent.</div>
            ${skillSelect}
            ${skillBox}
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Hooks <span style="color:var(--muted);font-weight:400;font-size:11px">(${agentHooks.length} selected)</span></div>
            <div class="settings-desc">Hook types this sub-agent triggers. Each selected type runs the matching <code>hooks.&lt;type&gt;</code> commands from config on the sub-agent's own turns/tools. Empty = inherit all hook types from main.</div>
            ${hookSelect}
            ${hookBox}
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Memory</div>
            <select class="settings-select" data-agent-memory="${esc(name)}">
              <option value="inherited" ${memory === "inherited" || memory === "" ? "selected" : ""}>Inherit from main</option>
              <option value="none" ${memory === "none" ? "selected" : ""}>None (stateless)</option>
            </select>
          </div>
        </div>`;
    }).join("");

    // Wire the dropdown + removable tag UX for a single agent card.
    function wireAgentSelections(card: HTMLElement, name: string) {
      const updateCount = (kind: string) => {
        const count = card.querySelectorAll(`.agent-selected-tag[data-kind="${kind}"]`).length;
        const box = card.querySelector(`[data-agent-box-${kind}="${name}"]`);
        const titleSpan = box?.parentElement?.querySelector(".settings-section-title span");
        if (titleSpan) titleSpan.textContent = `(${count} selected)`;
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
        select.innerHTML = `<option value="">Add ${kind}...</option>` + remaining.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
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
        if (!btn) return;
        const kind = btn.dataset.removeKind!;
        const value = btn.dataset.remove!;
        removeTag(kind, value);
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
      models: (p.models || []).map((m2: any) => ({ name: m2.name || "", supports_image: m2.supports_image ?? true })),
    }));
    for (let pi = 0; pi < normProviders.length; pi++) {
      const p = normProviders[pi];
      const isOpenAI = p.api_type !== "anthropic";
      let modelRows = "";
      for (const model of p.models) {
        const supportsImage = model.supports_image ?? true;  // default True = vision-capable
        modelRows += `
          <div class="settings-model-row">
            <input class="settings-input s-model-name" value="${esc(model.name)}" placeholder="Model name" style="flex:1" />
            <label class="settings-model-check" title="Can consume image_url blocks. Uncheck for text-only models — the runtime will then surface attachments as path text instead of base64."><input type="checkbox" class="s-model-image" ${supportsImage ? "checked" : ""} /> Vision</label>
            <label class="settings-model-check" title="Set as default model"><input type="radio" name="modelDefault" class="s-model-default" ${model.name === defaultModelName ? "checked" : ""} /> Default</label>
            <button class="settings-hook-remove s-model-remove" title="Remove">×</button>
          </div>`;
      }
      providersHtml += `
        <div class="settings-provider-card" data-provider-idx="${pi}">
          <div class="settings-provider-card-header">
            <input class="settings-input settings-provider-name" data-field="provider_name" value="${esc(p.name)}" placeholder="Provider name" />
            <button class="settings-hook-remove" data-provider-remove title="Remove provider">×</button>
          </div>
          <div class="settings-row"><label class="settings-label">API Type</label>
            <select class="settings-select" data-field="api_type">
              <option value="openai_compatible" ${isOpenAI ? "selected" : ""}>OpenAI Compatible</option>
              <option value="anthropic" ${!isOpenAI ? "selected" : ""}>Anthropic</option>
            </select>
          </div>
          <div class="settings-row"><label class="settings-label">API Key</label><input class="settings-input" type="password" data-field="api_key" value="${esc(p.api_key)}" /></div>
          <div class="settings-row"><label class="settings-label">Base URL</label><input class="settings-input" data-field="base_url" value="${esc(p.base_url)}" placeholder="e.g. https://api.openai.com/v1" /></div>
          <div class="settings-section-title" style="margin-top:8px">Models</div>
          <div class="settings-provider-models">${modelRows}</div>
          <button class="settings-add-btn s-add-model-btn">+ Add Model</button>
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
          <button class="settings-tab" data-tab="agents">${icons.agents}<span>Agents</span></button>
        </div>
        <div class="settings-content">
          <!-- Model -->
          <div class="settings-panel active" data-panel="model">
            <div class="settings-panel-inner">
              <div class="settings-section">
                <div class="settings-section-title">Thinking Mode</div>
                <div class="settings-desc">Configure reasoning effort for supported models (e.g., Claude 3.7 Sonnet).</div>
                <div class="settings-row"><label class="settings-label">Mode</label>
                  <select class="settings-select" id="s_thinking_mode">
                    <option value="disabled" ${(cfg.model?.thinking_mode || "disabled") === "disabled" ? "selected" : ""}>Disabled</option>
                    <option value="low" ${(cfg.model?.thinking_mode || "disabled") === "low" ? "selected" : ""}>Low</option>
                    <option value="medium" ${(cfg.model?.thinking_mode || "disabled") === "medium" ? "selected" : ""}>Medium</option>
                    <option value="high" ${(cfg.model?.thinking_mode || "disabled") === "high" ? "selected" : ""}>High</option>
                  </select>
                </div>
                <div class="settings-row"><label class="settings-label">Budget Tokens</label>
                  <input class="settings-input" id="s_thinking_budget" type="number" value="${cfg.model?.thinking_budget_tokens || 4000}" />
                </div>
              </div>
              <div class="settings-section-title" style="margin-top:16px;margin-bottom:8px;">Providers</div>
              <div id="sProvidersList">${providersHtml}</div>
              <button class="settings-add-btn" id="addProviderBtn">+ Add Provider</button>
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
          <!-- Agents -->
          <div class="settings-panel" data-panel="agents">
            <div class="settings-panel-inner settings-panel-wide">
              <div class="settings-section">
                <div class="settings-section-title">Sub-Agents</div>
                <div class="settings-desc">Predefined agent profiles the main agent can spawn via <code>spawn_agent(agent="name", task="...")</code>. Each agent has its own instructions, tool whitelist, skill set, and memory setting. The main agent may still pass <code>instructions</code> / <code>tools</code> / <code>background</code> at call time to override the defaults below.</div>
                <div id="agentsList">${agentsHtml || '<div style="color:var(--muted);font-size:12px;padding:12px 0">No agents defined yet. Click <strong>+ Add agent</strong> below to create one.</div>'}</div>
                <button class="settings-add-btn" id="addAgentBtn">+ Add agent</button>
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
            <input class="settings-input settings-agent-name" data-agent-rename="${esc(n)}" value="${esc(n)}" placeholder="agent name" style="font-weight:600;font-size:13px" />
            <label class="agent-bg-label"><input type="checkbox" data-agent-bg="${esc(n)}" /> background</label>
            <button class="settings-hook-remove" data-agent-remove="${esc(n)}" title="Remove agent">×</button>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Instructions</div>
            <textarea class="settings-input settings-agent-instructions" data-agent-instructions="${esc(n)}" rows="8" placeholder="System prompt for the sub-agent"></textarea>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Tools <span style="color:var(--muted);font-weight:400;font-size:11px">(0 selected)</span></div>
            <select class="settings-select agent-tools-select" data-agent-select-tools="${esc(n)}"><option value="">Add tools...</option>${toolOptions}</select>
            <div class="agent-selected-box" data-agent-box-tools="${esc(n)}"></div>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Skills <span style="color:var(--muted);font-weight:400;font-size:11px">(0 selected)</span></div>
            <select class="settings-select agent-skills-select" data-agent-select-skills="${esc(n)}"><option value="">Add skills...</option>${skillOptions}</select>
            <div class="agent-selected-box" data-agent-box-skills="${esc(n)}"></div>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Hooks <span style="color:var(--muted);font-weight:400;font-size:11px">(0 selected)</span></div>
            <select class="settings-select agent-hooks-select" data-agent-select-hooks="${esc(n)}"><option value="">Add hooks...</option>${hookOptions}</select>
            <div class="agent-selected-box" data-agent-box-hooks="${esc(n)}"></div>
          </div>
          <div class="settings-section">
            <div class="settings-section-title">Memory</div>
            <select class="settings-select" data-agent-memory="${esc(n)}">
              <option value="inherited" selected>Inherit from main</option>
              <option value="none">None (stateless)</option>
            </select>
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
            <input class="settings-input s-model-name" value="" placeholder="Model name" style="flex:1" />
            <label class="settings-model-check"><input type="checkbox" class="s-model-image" /> Image</label>
            <label class="settings-model-check"><input type="radio" name="modelDefault" class="s-model-default" /> Default</label>
            <button class="settings-hook-remove s-model-remove" title="Remove">×</button>`;
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
            <input class="settings-input settings-provider-name" data-field="provider_name" value="" placeholder="Provider name" />
            <button class="settings-hook-remove" data-provider-remove title="Remove provider">×</button>
          </div>
          <div class="settings-row"><label class="settings-label">API Type</label>
            <select class="settings-select" data-field="api_type">
              <option value="openai_compatible" selected>OpenAI Compatible</option>
              <option value="anthropic">Anthropic</option>
            </select>
          </div>
          <div class="settings-row"><label class="settings-label">API Key</label><input class="settings-input" type="password" data-field="api_key" value="" /></div>
          <div class="settings-row"><label class="settings-label">Base URL</label><input class="settings-input" data-field="base_url" value="" placeholder="e.g. https://api.openai.com/v1" /></div>
          <div class="settings-section-title" style="margin-top:8px">Models</div>
          <div class="settings-provider-models"></div>
          <button class="settings-add-btn s-add-model-btn">+ Add Model</button>`;
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
            <input class="settings-input settings-mcp-name" data-mcp-name="${esc(name)}" value="${esc(name)}" placeholder="Server name" style="font-weight:600;font-size:13px" />
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
        (card.querySelector(".settings-hook-remove") as HTMLElement).onclick = () => card.remove();
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

        // Model — collect from provider cards
        const newProviders: any[] = [];
        let defaultName = "";
        backdrop.querySelectorAll(".settings-provider-card").forEach(card => {
          const pName = (card.querySelector("[data-field='provider_name']") as HTMLInputElement)?.value.trim() || "";
          const apiType = (card.querySelector("[data-field='api_type']") as HTMLSelectElement)?.value || "openai_compatible";
          const apiKey = (card.querySelector("[data-field='api_key']") as HTMLInputElement)?.value || "";
          const baseUrl = (card.querySelector("[data-field='base_url']") as HTMLInputElement)?.value || "";
          const models: Array<{ name: string; supports_image: boolean }> = [];
          card.querySelectorAll(".settings-model-row").forEach(row => {
            const name = (row.querySelector(".s-model-name") as HTMLInputElement)?.value.trim() || "";
            if (!name) return;
            const supports_image = (row.querySelector(".s-model-image") as HTMLInputElement)?.checked ?? true;
            models.push({ name, supports_image });
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
        const tbt = parseInt((backdrop.querySelector("#s_thinking_budget") as HTMLInputElement)?.value || "4000", 10);
        updated.model = { name: defaultName || "", thinking_mode: tm, thinking_budget_tokens: tbt };

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
          const memVal = (card.querySelector(`[data-agent-memory="${origName}"]`) as HTMLSelectElement)?.value || "inherited";
          newAgents[newName] = {
            instructions: instr,
            tools,
            skills,
            hooks,
            background: bg,
            ...(memVal !== "inherited" ? { memory: memVal } : {}),
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
