import type { Event } from "./api";

export type EventHandler = (event: Event) => void;

/**
 * Single global SSE connection that receives events from ALL sessions.
 * Each event carries a `session_id` field (set by the server's
 * runtime._emit). The frontend's main.ts routes by session_id to the
 * per-session handler.
 *
 * Why one connection: the previous per-session SSEPool kept N
 * long-lived connections open (one per session), each running its own
 * reader loop. With 10+ sessions this created N parallel loops and N
 * reader promises, which the browser tab struggled to load — the page
 * would spin forever on hard refresh. One connection scales to N
 * sessions without growing the connection count.
 *
 * Connections are created lazily on first subscribe, retried with
 * exponential backoff on transient errors, and torn down when the last
 * subscriber invokes its returned unsubscribe.
 */
export class SSEPool {
  private handlers: Set<EventHandler> = new Set();
  private controller: AbortController | null = null;
  private retryCount = 0;
  private connected = false;
  private readonly MAX_RETRIES = 3;
  private readonly BASE_DELAY = 1000;
  private readonly MAX_DELAY = 10000;

  isConnected(): boolean {
    return this.connected;
  }

  subscribe(handler: EventHandler): () => void {
    this.handlers.add(handler);
    this.ensureConnected();
    return () => this.unsubscribe(handler);
  }

  private unsubscribe(handler: EventHandler): void {
    this.handlers.delete(handler);
    if (this.handlers.size === 0) {
      this.disconnect();
    }
  }

  disconnect(): void {
    this.retryCount = 0;
    this.connected = false;
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
  }

  private ensureConnected(): void {
    if (this.controller) return;
    this.retryCount = 0;
    this._doConnect();
  }

  private async _doConnect(): Promise<void> {
    if (this.handlers.size === 0) return;
    const controller = new AbortController();
    this.controller = controller;
    const signal = controller.signal;

    try {
      const response = await fetch(`/events`, { signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.connected = true;
      this.retryCount = 0;

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (this.controller === controller) {
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
              this.dispatch(ev);
            } catch { /* ignore parse errors */ }
            dataBuffer = "";
          }
        }
      }
    } catch (err) {
      this.connected = false;
      if (signal.aborted) return;
      if (this.handlers.size === 0) return;

      const retries = this.retryCount + 1;
      this.retryCount = retries;
      if (retries > this.MAX_RETRIES) {
        this.controller = null;
        return;
      }
      const delay = Math.min(this.BASE_DELAY * Math.pow(2, retries - 1), this.MAX_DELAY);
      setTimeout(() => {
        if (this.handlers.size > 0) this._doConnect();
      }, delay);
    } finally {
      if (this.controller === controller) {
        this.controller = null;
      }
    }
  }

  private dispatch(ev: Event): void {
    for (const fn of this.handlers) {
      try { fn(ev); } catch { /* ignore */ }
    }
  }
}
