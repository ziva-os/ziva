import type { Event } from "./api";

export type EventHandler = (event: Event) => void;

export class SSEConnection {
  private controller: AbortController | null = null;
  private handler: EventHandler;
  private sid: string | null = null;
  private _connected = false;
  private retryCount = 0;
  private readonly MAX_RETRIES = 3;
  private readonly BASE_DELAY = 1000;
  private readonly MAX_DELAY = 10000;
  private _generation = 0;

  get connected() { return this._connected; }

  constructor(handler: EventHandler) {
    this.handler = handler;
  }

  connect(sid: string): void {
    this._generation++;
    this.disconnect();
    this.sid = sid;
    this.retryCount = 0;
    this._doConnect(this._generation);
  }

  disconnect(): void {
    this._generation++;
    this.sid = null;
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    this._connected = false;
  }

  private async _doConnect(gen: number): Promise<void> {
    if (!this.sid || gen !== this._generation) return;

    this.controller = new AbortController();
    const signal = this.controller.signal;

    try {
      const response = await fetch(`/sessions/${this.sid}/events`, { signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this._connected = true;
      this.retryCount = 0;

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (gen === this._generation) {
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
              this.handler(JSON.parse(dataBuffer));
            } catch { /* ignore parse errors */ }
            dataBuffer = "";
          }
        }
      }
    } catch (err) {
      this._connected = false;
      if (signal.aborted || gen !== this._generation) return;

      if (this.retryCount < this.MAX_RETRIES) {
        const delay = Math.min(
          this.BASE_DELAY * Math.pow(2, this.retryCount),
          this.MAX_DELAY
        );
        this.retryCount++;
        setTimeout(() => this._doConnect(gen), delay);
      }
    }
  }
}
