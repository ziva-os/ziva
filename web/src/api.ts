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
  return r.json();
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

export async function getMessages(sid: string): Promise<{ messages: Record<string, any>[]; last_usage?: { prompt_tokens?: number; completion_tokens?: number } }> {
  return api("GET", `/sessions/${sid}/messages`);
}

export async function getTurns(sid: string): Promise<Turn[]> {
  const data = await api<{ turns: Turn[] }>("GET", `/sessions/${sid}/turns`);
  return data.turns || [];
}

export async function createTurn(sid: string, content: string): Promise<{ accepted: boolean; turn_id: string }> {
  return api("POST", `/sessions/${sid}/turns`, { messages: [{ role: "user", content }] });
}

export async function compactSession(sid: string): Promise<{ success: boolean; message_count?: number }> {
  const r = await fetch(`/sessions/${sid}/compact`, { method: "POST", headers: JSON_HEADERS });
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

export async function listAutomations(): Promise<Automation[]> {
  const data = await api<{ automations: Automation[] }>("GET", "/automations");
  return data.automations || [];
}

export async function createAutomation(name: string, prompt: string, intervalSeconds: number): Promise<{ id: string }> {
  return api("POST", "/automations", { name, prompt, interval_seconds: intervalSeconds });
}

export async function deleteAutomation(aid: string): Promise<void> {
  await api("DELETE", `/automations/${aid}`);
}
