export interface Session {
  id: string;
  preview?: string;
  turnCount?: number;
  status?: "idle" | "running" | "done" | "failed";
  time?: { created: number; updated: number };
}

export interface Turn {
  id: string;
  status: "running" | "done" | "failed" | "cancelled";
  events?: Event[];
  result?: { role: string; content: string; finish_reason?: string };
  error?: { message: string; class: string };
}

export interface Event {
  type: string;
  [key: string]: unknown;
}

export interface Automation {
  id: string;
  name: string;
  interval_seconds: number;
  enabled: boolean;
  last_run?: number;
  last_result?: string;
}

export interface Status {
  model: string;
  workspace: string;
  tools: string[];
  approval_policy: string;
}

export interface Config {
  model: { current: string; available: string[] };
  approval: { current: string; options: string[] };
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method, headers: JSON_HEADERS };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const data = (await r.json().catch(() => ({}))) as Record<string, unknown> | null;
  if (!r.ok) {
    const errCode = typeof data?.error === "string" ? data.error : `http_${r.status}`;
    const errMsg = typeof data?.message === "string" ? data.message : r.statusText || `HTTP ${r.status}`;
    const err = new Error(errMsg) as Error & { error?: string; status?: number };
    err.error = errCode;
    err.status = r.status;
    throw err;
  }
  return data as T;
}

export async function listSessions(): Promise<Session[]> {
  const data = await api<{ sessions: { id: string }[] }>("GET", "/sessions");
  return data.sessions || [];
}

export async function createSession(): Promise<string> {
  const data = await api<{ id: string }>("POST", "/sessions", {});
  return data.id;
}

export async function deleteSession(sid: string): Promise<void> {
  await api("DELETE", `/sessions/${sid}`);
}

export async function getMessages(sid: string, opts?: { includeDropped?: boolean }): Promise<{ messages: Record<string, any>[]; last_usage?: { prompt_tokens?: number; completion_tokens?: number } }> {
  const qs = opts?.includeDropped ? "?include_dropped=true" : "";
  return api("GET", `/sessions/${sid}/messages${qs}`);
}

export async function getTurns(sid: string): Promise<Turn[]> {
  const data = await api<{ turns: Turn[] }>("GET", `/sessions/${sid}/turns`);
  return data.turns || [];
}

export async function createTurn(sid: string, content: string): Promise<{ accepted: boolean; turn_id: string }> {
  return api("POST", `/sessions/${sid}/turns`, { messages: [{ role: "user", content }] });
}

export async function compactSession(sid: string): Promise<{ success: boolean; message_count?: number; last_usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } }> {
  const r = await fetch(`/sessions/${sid}/compact`, { method: "POST", headers: JSON_HEADERS });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(text || `HTTP ${r.status}`);
  }
  return r.json();
}

export async function pruneSession(sid: string): Promise<{ success: boolean; message_count?: number; last_usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } }> {
  const r = await fetch(`/sessions/${sid}/prune`, { method: "POST", headers: JSON_HEADERS });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(text || `HTTP ${r.status}`);
  }
  return r.json();
}

export async function cancelTurn(sid: string): Promise<void> {
  await api("POST", `/sessions/${sid}/cancel`);
}

export async function getPlan(sid: string): Promise<unknown[]> {
  const data = await api<{ plan: unknown[] }>("GET", `/sessions/${sid}/plan`);
  return data.plan || [];
}

export async function getDiff(sid: string): Promise<string> {
  const data = await api<{ diff: string }>("GET", `/sessions/${sid}/diff`);
  return data.diff || "";
}

export async function getStatus(): Promise<Status> {
  return api<Status>("GET", "/status");
}

export async function getConfig(): Promise<Config> {
  return api<Config>("GET", "/config");
}

export async function updateConfig(updates: { model?: { name: string }; approval?: { policy: string } }): Promise<void> {
  await api("PATCH", "/config", updates);
}

export async function getConfigYaml(): Promise<string> {
  const res = await api<{ yaml: string }>("GET", "/config/yaml");
  return res.yaml;
}

export async function saveConfigYaml(yaml: string): Promise<void> {
  await api("PUT", "/config/yaml", { yaml });
}

export async function getConfigJson(): Promise<Record<string, any>> {
  return api<Record<string, any>>("GET", "/config/json");
}

export async function saveConfigJson(config: Record<string, any>): Promise<void> {
  await api("PUT", "/config/json", config);
}

export async function updateSession(sid: string, updates: { name?: string }): Promise<void> {
  await api("PATCH", `/sessions/${sid}`, updates);
}

export interface MCPServerStatus {
  name: string;
  status: string;
  tool_count: number;
}

export interface MCPStatus {
  servers: MCPServerStatus[];
  connected: boolean;
  tools: { name: string; description: string }[];
}

export async function getMCPStatus(): Promise<MCPStatus> {
  return api<MCPStatus>("GET", "/mcp-status");
}

export async function revertFiles(sid: string, files: string[]): Promise<{ reverted: string[] }> {
  return api("POST", `/sessions/${sid}/revert`, { files });
}

export async function replyPermission(requestId: string, action: string, message?: string): Promise<void> {
  await api("POST", `/api/permissions/${requestId}/reply`, { action, message });
}

export async function replyQuestion(sid: string, answer: string): Promise<{ ok: boolean }> {
  return api("POST", `/sessions/${sid}/questions/reply`, { answer });
}

export async function listAutomations(): Promise<Automation[]> {
  const data = await api<{ automations: Automation[] }>("GET", "/automations");
  return data.automations || [];
}

export interface Skill {
  name: string;
  description: string;
  path: string;
  category: string;
}

export interface SkillFile {
  path: string;
  content: string;
  name: string;
  size: number;
}

export async function listSkills(): Promise<Skill[]> {
  const data = await api<{ skills: Skill[] }>("GET", "/skills");
  return data.skills || [];
}

export async function readSkillFile(path: string): Promise<SkillFile> {
  return api<SkillFile>("GET", `/skills/file?path=${encodeURIComponent(path)}`);
}

export async function createAutomation(name: string, prompt: string, intervalSeconds: number): Promise<{ id: string }> {
  return api("POST", "/automations", { name, prompt, interval_seconds: intervalSeconds });
}

export async function deleteAutomation(aid: string): Promise<void> {
  await api("DELETE", `/automations/${aid}`);
}
