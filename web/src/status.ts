/** Status-bar refresh — extracted from main.ts. */

import * as api from "./api";
import * as i18n from "./i18n";
import { $ } from "./dom";
import { store } from "./state";

// refreshStatus wires status-bar click handlers that live in main.ts
// (project picker, git-branch picker) and syncs the composer's approval
// dropdown (a composer helper). Injected at init to avoid a circular import.
interface StatusDeps {
  composerApprovalSelect: (sid: string) => HTMLSelectElement | null;
  openProjectPicker: (e?: MouseEvent | Event) => Promise<void>;
  openGitBranchPicker: (e?: MouseEvent | Event) => Promise<void>;
}
let _deps: StatusDeps;
export function setStatusDeps(deps: StatusDeps): void { _deps = deps; }

/** Toggle the "connected" indicator in app state. */
export function updateConnStatus(connected: boolean): void {
  store.set({ connected });
}

/** Refresh the MCP status badge in the sidebar from the server. */
export async function refreshMCPStatus(): Promise<void> {
  try {
    const status = await api.getMCPStatus();
    if (status.servers.length > 0) {
      ($("mcpStatus") as HTMLElement).style.display = "flex";
      const servers = status.servers.length;
      const tools = status.tools.length;
      const key = servers > 1 ? "status.mcpBadge" : "status.mcpBadgeOne";
      $("mcpDetail").textContent = i18n.t(key, { servers, tools });
    } else {
      ($("mcpStatus") as HTMLElement).style.display = "none";
    }
  } catch {
    ($("mcpStatus") as HTMLElement).style.display = "none";
  }
}

/** Pull workspace/model/approval status from the server into the store + UI. */
export async function refreshStatus(): Promise<void> {
  let workspace = "";
  try {
    const s = await api.getStatus();
    workspace = s.workspace || "";
    store.set({ config: { ...store.get().config, model: s.model, workspace: s.workspace, tools: s.tools, approval: s.approval_policy, contextWindow: s.context_window || 200000 } });
    // Sync the active composer's approval dropdown to the session's policy.
    const approvalSel = _deps.composerApprovalSelect(store.get().activeSid || "");
    if (approvalSel) approvalSel.value = s.approval_policy;
    const wn = (s.workspace || "ziva").split("/").pop() || "Project";
    const workspaceNameEl = $("workspaceName") as HTMLElement;
    if (workspaceNameEl) workspaceNameEl.textContent = wn;
  } catch { /* server not running */ }
  // Always attach click handlers so they work even when getStatus fails (e.g. new session)
  const contextWorkspaceEl = $("contextWorkspace") as HTMLElement;
  if (contextWorkspaceEl) {
    contextWorkspaceEl.style.cursor = "pointer";
    contextWorkspaceEl.title = workspace;
    contextWorkspaceEl.onclick = _deps.openProjectPicker;
  }
  const gitBranchContextEl = $("gitBranchContext") as HTMLElement;
  if (gitBranchContextEl) {
    gitBranchContextEl.onclick = _deps.openGitBranchPicker;
  }
}
