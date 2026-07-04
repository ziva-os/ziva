// electron/cdp-bridge.ts
//
// Chrome DevTools Protocol (CDP) bridge — exposes Electron's
// webContents to any CDP client (most importantly chrome-devtools-mcp
// running with --browser-url=http://127.0.0.1:<port>).
//
// Why a bridge instead of `--remote-debugging-port`?
//   1. The Chromium flag exposes the *main* BrowserWindow's webContents
//      only, including the Ziva UI itself. We want a dedicated page
//      (the "agent browser") that the agent can navigate without
//      breaking the chat surface.
//   2. Multiple page-level targets: main UI, agent browser, and any
//      future panes can each be a Target.attachedToTarget endpoint.
//   3. Permissionless: we own the webContents, so no
//      chrome://inspect/#remote-debugging dialog is shown — the user
//      never has to click "Allow".
//
// Protocol support is intentionally narrow:
//   - HTTP /json/version, /json/list, /json
//   - WS /devtools/browser — for Target.* commands (Puppeteer.connect
//     opens this endpoint first, then asks for targets)
//   - WS /devtools/page/<id> — direct page-level WS that bypasses the
//     Target domain (used by clients that just want to drive one page)
//   - Target.getTargets / attachToTarget / detachFromTarget /
//     sendMessageToTarget on the browser WS
//   - All other CDP methods (Page.*, Runtime.*, Network.*, …) are
//     forwarded straight to webContents.debugger.sendCommand
//   - Events from the page are dispatched to *all* connected clients
//     for that page (broadcast), so multiple clients can coexist
//
// Two response shapes are supported per session, matching Puppeteer's
// `flatten` flag:
//   - flatten: true  → responses to Target.sendMessageToTarget contain
//                       the inner command's result directly, and events
//                       arrive as { method, params, sessionId }
//   - flatten: false → responses are empty and events arrive wrapped
//                       in { method: "Target.receivedMessageFromTarget",
//                            params: { sessionId, message } }
// Modern Puppeteer (and therefore modern chrome-devtools-mcp) uses
// flatten: true; we still emit the wrapped form for older clients.

import * as http from "http";
import { AddressInfo } from "net";
import type { WebContents } from "electron";
import { WebSocket, WebSocketServer } from "ws";

const PROTOCOL_VERSION = "1.3";

export interface CdpBridgeOptions {
  port?: number;     // 0 = pick a free port (useful for tests)
  host?: string;     // defaults to 127.0.0.1 — local-only, no auth needed
}

interface PageTarget {
  id: string;
  title: string;
  url: string;
  webContents: WebContents;
  type: "page" | "background_page";
}

interface AttachedSession {
  sessionId: string;
  targetId: string;
  webContents: WebContents;
  ws: WebSocket;
  flatten: boolean;
}

export class CdpBridge {
  private server: http.Server | null = null;
  private wss: WebSocketServer | null = null;
  private readonly pages: PageTarget[] = [];
  // sessionId -> session
  private readonly sessions: Map<string, AttachedSession> = new Map();
  // webContents.id -> WebSocket (for the direct /devtools/page endpoint)
  private readonly directClient: Map<number, WebSocket> = new Map();
  // webContents -> true if we've already wc.debugger.attach()'d
  private readonly attachedPages: Set<WebContents> = new Set();
  private nextSessionId = 1;
  private readonly host: string;
  private requestedPort: number;
  private actualPort: number = 0;

  constructor(opts: CdpBridgeOptions = {}) {
    this.host = opts.host ?? "127.0.0.1";
    this.requestedPort = opts.port ?? 9222;
  }

  /** Port the server is listening on (only valid after start() resolves). */
  get port(): number {
    return this.actualPort;
  }

  /** Register a webContents as a Target. Returns the assigned targetId. */
  addPage(
    webContents: WebContents,
    opts: { type?: "page" | "background_page" } = {},
  ): string {
    const id = `ziva-page-${webContents.id}`;
    if (this.pages.find((p) => p.id === id)) return id;

    const target: PageTarget = {
      id,
      title: webContents.getTitle() || "",
      url: webContents.getURL() || "about:blank",
      webContents,
      type: opts.type || "page",
    };
    this.pages.push(target);

    const refreshMeta = () => {
      target.title = webContents.getTitle() || "";
      target.url = webContents.getURL() || "about:blank";
      this.broadcastTargetInfoChanged(target);
    };
    webContents.on("page-title-updated", refreshMeta);
    webContents.on("did-navigate", refreshMeta);
    webContents.on("did-navigate-in-page", refreshMeta);

    webContents.once("destroyed", () => this.removePage(id));

    // If the bridge is already up, announce the new target to live clients.
    if (this.server) this.broadcastTargetInfoChanged(target);
    return id;
  }

  removePage(id: string): void {
    const idx = this.pages.findIndex((p) => p.id === id);
    if (idx === -1) return;
    const [removed] = this.pages.splice(idx, 1);
    // Detach sessions for this target
    for (const [sessionId, sess] of this.sessions) {
      if (sess.targetId === id) this.sessions.delete(sessionId);
    }
    this.directClient.delete(removed.webContents.id);
    // Notify browser WS clients
    for (const sess of this.sessions.values()) {
      if (sess.ws.readyState === WebSocket.OPEN) {
        sess.ws.send(
          JSON.stringify({
            method: "Target.targetGone",
            params: { targetId: id },
          }),
        );
      }
    }
  }

  start(): Promise<number> {
    if (this.server) return Promise.resolve(this.actualPort);

    this.server = http.createServer((req, res) => this.handleHttp(req, res));
    this.wss = new WebSocketServer({ noServer: true });

    this.server.on("upgrade", (req, socket, head) => {
      const url = req.url || "/";
      this.wss!.handleUpgrade(req, socket as any, head, (ws) => {
        this.handleWsConnection(ws, url);
      });
    });

    return new Promise((resolve, reject) => {
      const onError = (err: Error) => reject(err);
      this.server!.once("error", onError);
      this.server!.listen(this.requestedPort, this.host, () => {
        this.server!.off("error", onError);
        const addr = this.server!.address() as AddressInfo | null;
        this.actualPort = addr?.port ?? this.requestedPort;
        // eslint-disable-next-line no-console
        console.log(
          `[cdp-bridge] http://${this.host}:${this.actualPort} (${this.pages.length} page target(s))`,
        );
        resolve(this.actualPort);
      });
    });
  }

  stop(): void {
    for (const p of this.pages) {
      try {
        if (this.attachedPages.has(p.webContents)) {
          p.webContents.debugger.detach();
        }
      } catch {
        /* ignore */
      }
    }
    this.attachedPages.clear();
    this.directClient.clear();
    this.sessions.clear();
    try {
      this.wss?.close();
    } catch {
      /* ignore */
    }
    try {
      this.server?.close();
    } catch {
      /* ignore */
    }
    this.wss = null;
    this.server = null;
  }

  // ---- HTTP ----

  private handleHttp(req: http.IncomingMessage, res: http.ServerResponse) {
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Cache-Control", "no-cache");

    const url = (req.url || "/").split("?")[0];

    if (url === "/json/version") {
      res.end(
        JSON.stringify({
          // chrome-devtools-mcp / Puppeteer check this to decide if the
          // endpoint is a real Chrome. Electron IS Chromium, so report the
          // real Chrome version — otherwise they ignore --browserUrl and
          // launch an external Chrome.
          Browser: `Chrome/${process.versions.chrome}`,
          "Protocol-Version": PROTOCOL_VERSION,
          "User-Agent": `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${process.versions.chrome} Safari/537.36`,
          V8: process.versions.v8,
          "WebKit-Version": "537.36",
          webSocketDebuggerUrl: `ws://${this.host}:${this.actualPort}/devtools/browser`,
        }),
      );
      return;
    }

    if (url === "/json/list" || url === "/json") {
      // If no Agent Browser tab is open, the list is empty and the agent
      // gets a clear "no target" error from chrome-devtools-mcp. That's
      // the intended contract: the user must open the Browser tab before
      // the agent can drive it. (See registerWebviewWithCdp in
      // web/src/main.ts for the webview-side wiring.)
      res.end(JSON.stringify(this.pages.map((p) => this.toPageInfo(p))));
      return;
    }

    if (url === "/json/protocol") {
      // We don't expose a full protocol descriptor — clients that need
      // it can use the upstream Chrome one. Empty object is valid.
      res.end("{}");
      return;
    }

    res.writeHead(404);
    res.end(JSON.stringify({ error: "not found" }));
  }

  private toPageInfo(p: PageTarget) {
    return {
      id: p.id,
      title: p.title,
      type: p.type,
      url: p.url,
      webSocketDebuggerUrl: `ws://${this.host}:${this.actualPort}/devtools/page/${p.id}`,
      devtoolsFrontendUrl: `/devtools/inspector.html?ws=${this.host}:${this.actualPort}/devtools/page/${p.id}`,
    };
  }

  // ---- WebSocket ----

  private handleWsConnection(ws: WebSocket, urlPath: string) {
    // Strip query string
    const path = urlPath.split("?")[0];

    const pageMatch = path.match(/^\/devtools\/page\/(.+)$/);
    if (pageMatch) {
      const targetId = pageMatch[1];
      const page = this.pages.find((p) => p.id === targetId);
      if (!page) {
        ws.close(1008, "unknown target");
        return;
      }
      this.attachDirectPageSession(ws, page);
      return;
    }

    if (path === "/devtools/browser" || path.startsWith("/devtools/browser/")) {
      this.attachBrowserSession(ws);
      return;
    }

    ws.close(1008, "unknown endpoint");
  }

  // Direct page session: commands come without sessionId, go straight
  // to the page's webContents.debugger. Simpler path used by clients
  // that just want to drive one page.
  private attachDirectPageSession(ws: WebSocket, page: PageTarget) {
    this.attachDebugger(page.webContents);
    this.directClient.set(page.webContents.id, ws);

    ws.on("message", async (data) => {
      const msg = this.parseMessage(data);
      if (!msg) return;
      if (msg.id === undefined) return; // ignore events from client
      await this.forwardCommand(ws, msg, page.webContents);
    });

    const cleanup = () => {
      if (this.directClient.get(page.webContents.id) === ws) {
        this.directClient.delete(page.webContents.id);
        if (!this.hasAnyClientFor(page.webContents)) {
          this.detachDebugger(page.webContents);
        }
      }
    };
    ws.on("close", cleanup);
    ws.on("error", cleanup);
  }

  // Browser session: hosts Target.* commands. After attachToTarget, the
  // client uses Target.sendMessageToTarget to talk to the page.
  private attachBrowserSession(ws: WebSocket) {
    // Pre-attach to all pages so Puppeteer's connect() can attach to
    // them immediately. Idempotent — no-op if already attached.
    for (const p of this.pages) this.attachDebugger(p.webContents);

    ws.on("message", async (data) => {
      const msg = this.parseMessage(data);
      if (!msg) return;
      await this.handleBrowserMessage(ws, msg);
    });

    const cleanup = () => {
      for (const [sessionId, sess] of this.sessions) {
        if (sess.ws === ws) this.sessions.delete(sessionId);
      }
      for (const p of this.pages) {
        if (!this.hasAnyClientFor(p.webContents)) {
          this.detachDebugger(p.webContents);
        }
      }
    };
    ws.on("close", cleanup);
    ws.on("error", cleanup);
  }

  private async handleBrowserMessage(ws: WebSocket, msg: any) {
    // ---- Target domain ----
    // chrome-devtools-mcp / Puppeteer call this right after connect. Electron
    // has no browser contexts (no incognito), so report none — returning
    // "Method not handled at browser level" breaks the whole handshake.
    if (msg.method === "Target.getBrowserContexts") {
      this.respond(ws, msg.id, { browserContextIds: [] });
      return;
    }
    if (msg.method === "Target.getTargets") {
      // Puppeteer / chrome-devtools-mcp ask for targets over the WS
      // (not the /json/list HTTP endpoint). If the Agent Browser tab
      // isn't open, this returns an empty list — the client gets a
      // clear "no target" error rather than silently switching to a
      // hidden window. See the /json/list handler above for the
      // matching contract.
      this.respond(ws, msg.id, {
        targetInfos: this.pages.map((p) => ({
          targetId: p.id,
          type: p.type,
          title: p.title,
          url: p.url,
          attached: this.hasSessionFor(p.id),
        })),
      });
      return;
    }
    // Puppeteer optionally enables auto-attach / target discovery. We don't
    // auto-attach (the client attaches explicitly via attachToTarget), but
    // must respond so the method isn't "not handled".
    if (msg.method === "Target.setAutoAttach") {
      this.respond(ws, msg.id, {});
      return;
    }
    if (msg.method === "Target.setDiscoverTargets") {
      this.respond(ws, msg.id, {});
      if (msg.params?.discover) {
        for (const p of this.pages) {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(
              JSON.stringify({
                method: "Target.targetCreated",
                params: {
                  targetInfo: {
                    targetId: p.id,
                    type: p.type,
                    title: p.title,
                    url: p.url,
                    attached: this.hasSessionFor(p.id),
                  },
                },
              })
            );
          }
        }
      }
      return;
    }
    // chrome-devtools-mcp's new_page / navigate calls Target.createTarget to
    // open a fresh page. We return the most recently registered page's
    // targetId so the client can attach to it. If no page is registered
    // (Browser tab never opened), we return an empty targetId and the
    // client fails with a clear error — no silent fallback to a hidden
    // window. To open a fresh page the user must first open the Agent
    // Browser tab so a webview gets registered via
    // registerWebviewWithCdp in the renderer.
    if (msg.method === "Target.createTarget") {
      const page = this.pages[this.pages.length - 1];
      if (page && msg.params?.url && msg.params.url !== "about:blank") {
        page.webContents.loadURL(msg.params.url).catch(() => {});
      }
      this.respond(ws, msg.id, { targetId: page?.id || "" });
      return;
    }

    if (msg.method === "Target.attachToTarget") {
      const targetId = msg.params?.targetId;
      const flatten = !!msg.params?.flatten;
      const page = this.pages.find((p) => p.id === targetId);
      if (!page) {
        this.respondError(ws, msg.id, -32000, "No target found");
        return;
      }
      this.attachDebugger(page.webContents);
      const sessionId = `s-${this.nextSessionId++}`;
      this.sessions.set(sessionId, {
        sessionId,
        targetId: page.id,
        webContents: page.webContents,
        ws,
        flatten,
      });
      this.respond(ws, msg.id, { sessionId });
      // Notify other listeners on the same ws (Puppeteer also expects this)
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            method: "Target.attachedToTarget",
            params: {
              sessionId,
              targetInfo: {
                targetId: page.id,
                type: page.type,
                title: page.title,
                url: page.url,
              },
            },
          }),
        );
      }
      return;
    }

    if (msg.method === "Target.detachFromTarget") {
      const sessionId = msg.params?.sessionId;
      if (typeof sessionId === "string") {
        const sess = this.sessions.get(sessionId);
        this.sessions.delete(sessionId);
        if (sess && !this.hasAnyClientFor(sess.webContents)) {
          this.detachDebugger(sess.webContents);
        }
      }
      if (msg.id !== undefined) this.respond(ws, msg.id, {});
      return;
    }

    if (msg.method === "Target.sendMessageToTarget") {
      const sessionId = msg.params?.sessionId;
      const innerRaw = msg.params?.message;
      let innerMsg: any;
      try {
        innerMsg = typeof innerRaw === "string" ? JSON.parse(innerRaw) : innerRaw;
      } catch {
        this.respondError(ws, msg.id, -32700, "Invalid inner message JSON");
        return;
      }
      const sess = this.sessions.get(sessionId);
      if (!sess) {
        this.respondError(ws, msg.id, -32000, "No session");
        return;
      }
      if (innerMsg.id === undefined) {
        // Not a request, ignore
        return;
      }

      try {
        const result = await sess.webContents.debugger.sendCommand(
          innerMsg.method,
          innerMsg.params || {},
        );
        if (sess.flatten) {
          // Modern (Puppeteer) shape: embed inner result in the outer's
          // response. The outer's id and the inner's id are linked 1:1
          // by the client, so we just spread the inner result.
          this.respond(ws, msg.id, { ...result, sessionId });
        } else {
          // Legacy shape: empty outer response, inner response is
          // delivered as a Target.receivedMessageFromTarget event.
          this.respond(ws, msg.id, {});
          this.sendInnerResponseAsEvent(sess, innerMsg.id, result, undefined);
        }
      } catch (err: any) {
        const message = err?.message || String(err);
        if (sess.flatten) {
          this.respondError(ws, msg.id, -32000, message);
        } else {
          this.respond(ws, msg.id, {});
          this.sendInnerResponseAsEvent(sess, innerMsg.id, undefined, message);
        }
      }
      return;
    }

    // ---- Browser domain (minimal — Electron's "browser" is mostly
    // invisible to CDP clients, so we synthesize enough for Puppeteer's
    // connect() to succeed). ----
    if (msg.method === "Browser.getVersion") {
      this.respond(ws, msg.id, {
        protocolVersion: PROTOCOL_VERSION,
        product: `Ziva/Electron (${process.versions.electron})`,
        revision: process.versions.chrome || "0",
        userAgent: `Ziva/Electron (${process.versions.electron})`,
        jsVersion: process.versions.v8,
      });
      return;
    }

    if (msg.method === "Browser.getWindowForTarget") {
      if (msg.id !== undefined) {
        this.respond(ws, msg.id, {
          windowId: -1,
          bounds: { x: 0, y: 0, width: 0, height: 0 },
        });
      }
      return;
    }

    // Anything else with an id: tell the client we don't know it. This
    // matches Chrome's behavior and lets clients fall back gracefully.
    if (msg.id !== undefined) {
      this.respondError(ws, msg.id, -32601, `Method not handled at browser level: ${msg.method}`);
    }
  }

  // ---- Debugger plumbing ----

  private attachDebugger(wc: WebContents) {
    if (this.attachedPages.has(wc)) return;
    try {
      wc.debugger.attach(PROTOCOL_VERSION);
      this.attachedPages.add(wc);
      wc.debugger.on("message", (_event, method, params) => {
        this.dispatchEvent(wc, method, params);
      });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[cdp-bridge] failed to attach debugger:", err);
    }
  }

  private detachDebugger(wc: WebContents) {
    if (!this.attachedPages.has(wc)) return;
    try {
      wc.debugger.detach();
    } catch {
      /* ignore */
    }
    this.attachedPages.delete(wc);
  }

  private async forwardCommand(ws: WebSocket, msg: any, wc: WebContents) {
    try {
      const result = await wc.debugger.sendCommand(msg.method, msg.params || {});
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ id: msg.id, result }));
      }
    } catch (err: any) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            id: msg.id,
            error: { code: -32000, message: err?.message || String(err) },
          }),
        );
      }
    }
  }

  private dispatchEvent(wc: WebContents, method: string, params: any) {
    // Direct page client
    const direct = this.directClient.get(wc.id);
    if (direct && direct.readyState === WebSocket.OPEN) {
      direct.send(JSON.stringify({ method, params }));
    }
    // Browser-session clients: route to the sessions that own this WC
    for (const sess of this.sessions.values()) {
      if (sess.webContents !== wc) continue;
      if (sess.ws.readyState !== WebSocket.OPEN) continue;
      if (sess.flatten) {
        sess.ws.send(JSON.stringify({ method, params, sessionId: sess.sessionId }));
      } else {
        sess.ws.send(
          JSON.stringify({
            method: "Target.receivedMessageFromTarget",
            params: {
              sessionId: sess.sessionId,
              message: JSON.stringify({ method, params }),
            },
          }),
        );
      }
    }
  }

  private sendInnerResponseAsEvent(
    sess: AttachedSession,
    innerId: number,
    result: any,
    errorMessage: string | undefined,
  ) {
    if (sess.ws.readyState !== WebSocket.OPEN) return;
    const inner = errorMessage
      ? { id: innerId, error: { code: -32000, message: errorMessage } }
      : { id: innerId, result };
    sess.ws.send(
      JSON.stringify({
        method: "Target.receivedMessageFromTarget",
        params: {
          sessionId: sess.sessionId,
          message: JSON.stringify(inner),
        },
      }),
    );
  }

  private broadcastTargetInfoChanged(p: PageTarget) {
    for (const sess of this.sessions.values()) {
      if (sess.ws.readyState !== WebSocket.OPEN) continue;
      sess.ws.send(
        JSON.stringify({
          method: "Target.targetInfoChanged",
          params: {
            targetInfo: {
              targetId: p.id,
              type: p.type,
              title: p.title,
              url: p.url,
            },
          },
        }),
      );
    }
  }

  // ---- helpers ----

  private parseMessage(data: any): any | null {
    try {
      return JSON.parse(data.toString());
    } catch {
      return null;
    }
  }

  private respond(ws: WebSocket, id: number, result: any) {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ id, result }));
  }

  private respondError(ws: WebSocket, id: number, code: number, message: string) {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ id, error: { code, message } }));
  }

  private hasAnyClientFor(wc: WebContents): boolean {
    if (this.directClient.has(wc.id)) return true;
    for (const sess of this.sessions.values()) {
      if (sess.webContents === wc) return true;
    }
    return false;
  }

  private hasSessionFor(targetId: string): boolean {
    for (const sess of this.sessions.values()) {
      if (sess.targetId === targetId) return true;
    }
    return false;
  }
}
