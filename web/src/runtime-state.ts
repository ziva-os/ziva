/** Per-session runtime state — streaming/pending/draft accessors.
 *  Extracted from main.ts as the shared foundation for the chat-engine modules
 *  (messages/composer/sse-router). Operates on the singleton store + a few
 *  module-local maps (stream contexts, live-stream target). */

import { store } from "./state";
import type { PendingAttachment, PendingItem } from "./state";

interface RuntimeStateDeps { updateSendStopButton: () => void; }
let _deps: RuntimeStateDeps;
export function setRuntimeStateDeps(deps: RuntimeStateDeps): void { _deps = deps; }

export function isActiveRunning(): boolean {
  return isSessionRunning(store.get().activeSid || "");
}

export function getActivePending(): string | null {
  return getSessionPending(store.get().activeSid || "");
}

export function setActivePending(text: string | null, retries: number = 0) {
  setSessionPending(store.get().activeSid || "", text, retries);
}

export function setActiveRunning(running: boolean) {
  setSessionRunning(store.get().activeSid || "", running);
  // Keep the global send/stop button in sync.
  _deps.updateSendStopButton();
}

// Per-session state helpers (sid-keyed; not "active" only).
// Other sessions' values are kept in the map but only matter for
// background turns (e.g. when a question card is answered in a
// non-active session — handled in the SSE event path).
export function setSessionRunning(sid: string, running: boolean) {
  if (!sid) return;
  const { runningSessions } = store.get();
  const next = { ...runningSessions };
  if (running) next[sid] = true;
  else delete next[sid];
  store.set({ runningSessions: next });
}

export function isSessionRunning(sid: string): boolean {
  if (!sid) return false;
  return !!store.get().runningSessions[sid];
}

export function getSessionPending(sid: string): string | null {
  // Legacy compatibility: return the first item's text if any exist
  if (!sid) return null;
  const queue = store.get().pendingMessages[sid];
  return (queue && queue.length > 0) ? queue[0].text : null;
}

export function setSessionPending(sid: string, text: string | null, retries: number = 0) {
  // Legacy compatibility: replace the entire queue with a single item
  if (!sid) return;
  const { pendingMessages } = store.get();
  const next = { ...pendingMessages };
  if (text == null) {
    delete next[sid];
  } else {
    const prev = pendingMessages[sid];
    const images = (prev && prev.length > 0) ? prev[0].images : undefined;
    next[sid] = [{ id: generatePendingId(), text, retries, images }];
  }
  store.set({ pendingMessages: next });
}

// Generate a stable ID for a pending item
export function generatePendingId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

// Enqueue a new pending message to the end of the queue
export function enqueuePending(sid: string, text: string, retries: number = 0, images?: PendingAttachment[]): string {
  if (!sid) return "";
  const { pendingMessages } = store.get();
  const queue = pendingMessages[sid] || [];
  const item: PendingItem = { id: generatePendingId(), text, retries, images };
  const next = { ...pendingMessages, [sid]: [...queue, item] };
  store.set({ pendingMessages: next });
  return item.id;
}

// Get the current queue for a session
export function getPendingQueue(sid: string): PendingItem[] {
  if (!sid) return [];
  return store.get().pendingMessages[sid] || [];
}

// Update a specific pending item by ID
export function updatePendingItem(sid: string, id: string, patch: Partial<PendingItem>): void {
  if (!sid) return;
  const { pendingMessages } = store.get();
  const queue = pendingMessages[sid];
  if (!queue) return;
  const next = { ...pendingMessages };
  next[sid] = queue.map(item => item.id === id ? { ...item, ...patch } : item);
  store.set({ pendingMessages: next });
}

// Remove a specific pending item by ID
export function removePendingItem(sid: string, id: string): void {
  if (!sid) return;
  const { pendingMessages } = store.get();
  const queue = pendingMessages[sid];
  if (!queue) return;
  const next = { ...pendingMessages };
  const filtered = queue.filter(item => item.id !== id);
  if (filtered.length === 0) {
    delete next[sid];
  } else {
    next[sid] = filtered;
  }
  store.set({ pendingMessages: next });
}

// Clear all pending items for a session
export function clearAllPending(sid: string): void {
  if (!sid) return;
  const { pendingMessages } = store.get();
  const next = { ...pendingMessages };
  delete next[sid];
  store.set({ pendingMessages: next });
}

// Per-session streaming context. Keyed by sid so two panes can stream
// concurrently without their in-progress assistant element / pending tool
// cards colliding. The streaming text buffers (_main / _reasoning) live on
// the assistant DOM element itself, so isolating `assistantEl` +
// `pendingTools` per sid is enough for correct concurrent streaming.
interface StreamCtx { assistantEl: HTMLElement | null; pendingTools: Map<string, HTMLElement>; }
const _streamCtx = new Map<string, StreamCtx>();
export function streamCtx(sid: string): StreamCtx {
  let c = _streamCtx.get(sid);
  if (!c) { c = { assistantEl: null, pendingTools: new Map() }; _streamCtx.set(sid, c); }
  return c;
}
export function clearStreamCtx(sid: string): void {
  const c = _streamCtx.get(sid);
  if (!c) return;
  if (c.assistantEl) c.assistantEl.remove();
  c.pendingTools.forEach((el) => el.remove());
  _streamCtx.delete(sid);
}
// The sid whose turn is currently being processed by handleSessionEvent.
// Set only while a streaming event is being handled (null during history
// rendering), so the append* helpers' "next assistant segment starts
// fresh" invalidation hits the right session without clobbering others.
let liveStreamSid: string | null = null;
// The messages container for that session. While a streaming event is
// being handled, the no-arg scrollBottom()/removeTyping()/appendTyping()/
// append* helpers resolve to this target so the same code streams into a
// split pane as into #messages. Null outside event handling → defaults to
// #messages (the active container), which is correct for history rendering.
let liveStreamTarget: HTMLElement | null = null;
// liveStreamSid/liveStreamTarget are module-local but read+reassigned by
// main.ts (append* defaults, SSE handler), so they're exposed as accessors —
// ES import bindings are read-only and can't be reassigned from the importer.
export function getLiveStreamSid(): string | null { return liveStreamSid; }
export function setLiveStreamSid(s: string | null): void { liveStreamSid = s; }
export function getLiveStreamTarget(): HTMLElement | null { return liveStreamTarget; }
export function setLiveStreamTarget(t: HTMLElement | null): void { liveStreamTarget = t; }
export function invalidateLiveStreamEl(): void {
  if (liveStreamSid) streamCtx(liveStreamSid).assistantEl = null;
}

// --- Per-session image attachments (single source of truth) ---
// Live (in-composer, editable) attachments ride on the prompt draft;
// queued-message attachments (frozen, waiting to flush on turn_end)
// ride on the pending message. Both are per-sid in the store. This
// replaces the former `pendingImages` module array (an active-session
// mirror) and the `pendingSessionImages` map, so a split-pane composer
// can attach/send images for its own session with no active/background
// special-casing.
export function draftImages(sid: string): PendingAttachment[] {
  if (!sid) return [];
  return store.get().promptDrafts[sid]?.images || [];
}
export function setDraftImages(sid: string, images: PendingAttachment[]): void {
  if (!sid) return;
  const { promptDrafts } = store.get();
  const prev = promptDrafts[sid] || { text: "", images: [] as PendingAttachment[] };
  store.set({ promptDrafts: { ...promptDrafts, [sid]: { text: prev.text || "", images } } });
}
export function draftText(sid: string): string {
  if (!sid) return "";
  return store.get().promptDrafts[sid]?.text || "";
}
export function setDraftText(sid: string, text: string): void {
  if (!sid) return;
  const { promptDrafts } = store.get();
  const prev = promptDrafts[sid] || { text: "", images: [] as PendingAttachment[] };
  store.set({ promptDrafts: { ...promptDrafts, [sid]: { text, images: prev.images || [] } } });
}
export function queuedImages(sid: string): PendingAttachment[] {
  // Legacy compatibility: return images from the first item
  if (!sid) return [];
  const queue = store.get().pendingMessages[sid];
  return (queue && queue.length > 0) ? (queue[0].images || []) : [];
}
export function setQueuedImages(sid: string, images: PendingAttachment[]): void {
  // Legacy compatibility: update images on the first item
  if (!sid) return;
  const { pendingMessages } = store.get();
  const queue = pendingMessages[sid];
  if (!queue || queue.length === 0) return;
  const next = { ...pendingMessages };
  next[sid] = [{ ...queue[0], images }, ...queue.slice(1)];
  store.set({ pendingMessages: next });
}
export function clearQueuedImages(sid: string): void {
  // Legacy compatibility: clear images from the first item
  if (!sid) return;
  const { pendingMessages } = store.get();
  const queue = pendingMessages[sid];
  if (!queue || queue.length === 0) return;
  const next = { ...pendingMessages };
  const { images: _drop, ...rest } = queue[0];
  next[sid] = [{ ...rest, images: undefined }, ...queue.slice(1)];
  store.set({ pendingMessages: next });
}
