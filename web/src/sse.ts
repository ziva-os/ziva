import type { Event } from "./api";

export type EventHandler = (event: Event) => void;
export type SidEventHandler = (sid: string, event: Event) => void;

/**
 * One SSE connection per session, kept alive independently of which
 * session is "active" in the UI. This lets the backend run turns in
 * parallel across sessions without losing events for background ones
 * when the user switches the active session.
 *
 * Connections are created lazily on first subscribe, retried with
 * exponential backoff on transient errors, and torn down when the
 * caller invokes `unsubscribe` and the refcount drops to zero.
 */
export class SSEPool {
  private handlers: Map<string, Set<SidEventHandler>> = new Map();
  private controllers: Map<string, AbortController> = new Map();
  private retryCounts: Map<string, number> = new Map();
  private connected: Set<string> = new Set();
  private readonly MAX_RETRIES = 3;
  private readonly BASE_DELAY = 1000;
  private readonly MAX_DELAY = 10000;

  isConnected(sid: string): boolean {
    return this.connected.has(sid);
  }

  subscribe(sid: string, handler: SidEventHandler): () => void {
    let set = this.handlers.get(sid);
    if (!set) {
      set = new Set();
      this.handlers.set(sid, set);
    }
    set.add(handler);
    this.ensureConnected(sid);
    return () => this.unsubscribe(sid, handler);
  }

  private unsubscribe(sid: string, handler: SidEventHandler): void {
    const set = this.handlers.get(sid);
    if (!set) return;
    set.delete(handler);
    if (set.size === 0) {
      this.handlers.delete(sid);
      this.disconnect(sid);
    }
  }

  disconnect(sid: string): void {
    this.retryCounts.delete(sid);
    this.connected.delete(sid);
    const ctrl = this.controllers.get(sid);
    if (ctrl) {
      ctrl.abort();
      this.controllers.delete(sid);
    }
  }

  disconnectAll(): void {
    for (const sid of [...this.controllers.keys()]) {
      this.disconnect(sid);
    }
  }

  private ensureConnected(sid: string): void {
    if (this.controllers.has(sid)) return;
    this.retryCounts.set(sid, 0);
    this._doConnect(sid);
  }

  private async _doConnect(sid: string): Promise<void> {
    if (!this.handlers.has(sid)) return; // no subscribers; aborted via disconnect
    const controller = new AbortController();
    this.controllers.set(sid, controller);
    const signal = controller.signal;

    try {
      const response = await fetch(`/sessions/${sid}/events`, { signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.connected.add(sid);
      this.retryCounts.set(sid, 0);

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (this.controllers.get(sid) === controller) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let dataBuffer = "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith("data:")) {
            dataBuffer += trimmed.slice(5).trim();
          } else if (trimmed === "" && dataBuffer) {
            try {
              const ev = JSON.parse(dataBuffer) as Event;
              this.dispatch(sid, ev);
            } catch { /* ignore parse errors */ }
            dataBuffer = "";
          }
        }
      }
    } catch (err) {
      this.connected.delete(sid);
      if (signal.aborted) return;
      if (!this.handlers.has(sid)) return;

      const retries = (this.retryCounts.get(sid) ?? 0) + 1;
      this.retryCounts.set(sid, retries);
      if (retries > this.MAX_RETRIES) {
        this.controllers.delete(sid);
        return;
      }
      const delay = Math.min(this.BASE_DELAY * Math.pow(2, retries - 1), this.MAX_DELAY);
      setTimeout(() => {
        if (this.handlers.has(sid)) this._doConnect(sid);
      }, delay);
    } finally {
      if (this.controllers.get(sid) === controller) {
        this.controllers.delete(sid);
      }
    }
  }

  private dispatch(sid: string, ev: Event): void {
    const set = this.handlers.get(sid);
    if (!set) return;
    for (const fn of set) {
      try { fn(sid, ev); } catch { /* ignore */ }
    }
  }
}
