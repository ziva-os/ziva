import { app, BrowserWindow, ipcMain, webContents, session, shell, WebContentsView, clipboard } from "electron";
import * as path from "path";
import { spawn, execFileSync, ChildProcess } from "child_process";
import * as http from "http";
import { CdpBridge } from "./cdp-bridge";

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
let cdpBridge: CdpBridge | null = null;
const PORT = 4097;
const CDP_PORT = Number(process.env.ZIVA_CDP_PORT || 9222);

// ---- Embedded Chromium browser (WebContentsView) ----
// Each web tab is a real native Chromium view (WebContentsView), managed by the
// main process and positioned over the "web area" the renderer reserves. This
// is a true embedded browser (not a <webview> DOM element) — the page renders
// in its own native web contents, like a real browser pane inside Ziva.
const browserViews = new Map<string, WebContentsView>();
let activeBrowserTab: string | null = null;
// Bounds of the web area in window coords (reported by the renderer). The
// renderer leaves this rectangle empty (its tab strip + Ziva panel sit around
// it) and the main process positions the WebContentsView exactly here.
let browserArea = { x: 0, y: 72, width: 1000, height: 700 };

function applyBrowserArea(): void {
  for (const [id, view] of browserViews) {
    if (id === activeBrowserTab) view.setBounds(browserArea);
  }
}
function showBrowserTab(id: string): void {
  if (!mainWindow) return;
  for (const [vid, view] of browserViews) {
    if (vid === id) {
      try { mainWindow.contentView.addChildView(view); } catch {}
      view.setBounds(browserArea);
    } else {
      try { mainWindow.contentView.removeChildView(view); } catch {}
    }
  }
  activeBrowserTab = id;
}
function destroyBrowserTab(id: string): void {
  console.log(`[main] destroyBrowserTab id=${id}`);
  const v = browserViews.get(id);
  if (!v) return;
  if ((v as any)._cdpTargetId && cdpBridge) cdpBridge.removePage((v as any)._cdpTargetId);
  try { mainWindow?.contentView.removeChildView(v); } catch { /* not attached */ }
  (v.webContents as any).destroy?.();
  browserViews.delete(id);
  if (activeBrowserTab === id) activeBrowserTab = null;
}
// Dedicated session partition for the Agent Browser <webview>. This keeps
// browser cookies/cache isolated from the main Ziva UI and lets us set a
// system proxy explicitly on the session used by the webview.
const BROWSER_PARTITION = "persist:ziva-browser";

function getBackendCommand(): { cmd: string; args: string[]; env: NodeJS.ProcessEnv } {
  const isDev = !app.isPackaged;
  const fs = require("fs");
  const projectRoot = (() => {
    // When compiled to dist/main.js, __dirname is electron/dist; the
    // project root is two levels up. When running from source it is one
    // level up. Detect by looking for pyproject.toml.
    const oneUp = path.resolve(__dirname, "..");
    const twoUp = path.resolve(__dirname, "..", "..");
    if (fs.existsSync(path.join(twoUp, "pyproject.toml"))) return twoUp;
    if (fs.existsSync(path.join(oneUp, "pyproject.toml"))) return oneUp;
    return twoUp;
  })();

  // Default workspace for the packaged app: the user's Documents folder, so
  // sessions / repos live somewhere the user can browse in Finder instead of
  // inside the read-only Ziva.app bundle. Falls back to HOME if Documents
  // isn't available (very unusual). Dev mode uses the repo root so that the
  // desktop app shares the same project_id (and therefore history) as a
  // `ziva desktop serve` started from the repo root.
  let workspaceArg: string | null = null;
  if (!isDev) {
    // Prefer the last-used workspace so reopening the app lands back in the
    // project the user was working in — otherwise every launch defaults to the
    // Documents folder, which shows up as a stray empty workspace in the sidebar.
    // First launch (no recent workspaces yet) falls back to Documents.
    try {
      const recentPath = path.join(app.getPath("home"), ".ziva", "recent_workspaces.json");
      const recent = JSON.parse(fs.readFileSync(recentPath, "utf8"));
      if (Array.isArray(recent) && recent.length > 0 && fs.existsSync(recent[0])) {
        workspaceArg = recent[0];
      }
    } catch {
      // no recent_workspaces.json yet — first launch
    }
    if (!workspaceArg) {
      try {
        workspaceArg = app.getPath("documents") || app.getPath("home");
      } catch {
        workspaceArg = app.getPath("home");
      }
    }
  } else {
    workspaceArg = projectRoot;
  }
  const baseArgs = ["desktop", "serve", "--port", String(PORT)];
  if (workspaceArg) baseArgs.push("--workspace", workspaceArg);

  // macOS GUI apps inherit a minimal PATH that omits shell-managed binaries
  // (nvm's node/npx, uv's uvx, ~/.local/bin, /opt/homebrew/bin). The backend
  // spawns MCP servers via `uvx`/`npx`, so merge in a login shell's PATH —
  // otherwise MCP connect fails with "No such file or directory: 'uvx'/'npx'".
  const env = { ...process.env };
  try {
    const cp = require("child_process");
    const r = cp.spawnSync("zsh", ["-lic", "echo $PATH"], {
      encoding: "utf8", timeout: 3000,
    });
    const shellPath = (r.stdout || "").trim();
    if (shellPath) env.PATH = shellPath + ":" + (env.PATH || "");
  } catch { /* zsh missing or slow — fall back to default PATH */ }

  if (isDev) {
    return {
      cmd: "python3",
      args: ["-m", "ziva.app.cli", ...baseArgs],
      env: { ...env, PYTHONPATH: path.join(projectRoot, "src") },
    };
  }

  // Packaged: use PyInstaller binary bundled in Resources
  const ext = process.platform === "win32" ? ".exe" : "";
  const backendPath = path.join(process.resourcesPath, `ziva-backend${ext}`);
  return {
    cmd: backendPath,
    args: baseArgs,
    env,
  };
}

// Note: the app no longer spawns a separate "debug" Chrome. The browser is
// fused into the app itself — web tabs are in-app <webview>s exposed to
// chrome-devtools-mcp via the CDP bridge on CDP_PORT (9222). Point
// chrome-devtools-mcp at --browser-url=http://127.0.0.1:9222.

// Detect a leftover backend from a previous crashed session and reuse it if
// it is still healthy. This prevents the "address already in use" startup
// failure and also makes second-instance launches attach to the running one.
function checkBackendHealth(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PORT}/status`, { timeout: 2000 }, (res) => {
      let body = "";
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => {
        try {
          const data = JSON.parse(body);
          // /status returns { model, workspace, tools, approval_policy, context_window }
          resolve(data && typeof data.workspace === "string");
        } catch {
          resolve(false);
        }
      });
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

// Tear down the whole backend process tree on quit. The backend is spawned
// detached (its own process group) so a PyInstaller build is actually three
// processes: the bootloader we spawned, the real `desktop serve` server that
// holds :4097, and a multiprocessing resource_tracker child. SIGTERM-ing only
// our direct child (the bootloader) leaves the server orphaned on :4097, and
// the reuse logic then silently attaches the next launch to that stale
// backend. Killing the process group reaches all three at once.
function killBackendTree(): void {
  // 1. Group-kill the backend we spawned (detached → pgid == its pid, so
  //    -pid broadcasts SIGTERM to the whole tree).
  if (pythonProcess && pythonProcess.pid) {
    try { process.kill(-pythonProcess.pid, "SIGTERM"); } catch { /* already gone */ }
  }
  // 2. Fallback for the reused-backend case (pythonProcess is null because we
  //    attached to an existing :4097 instead of spawning): kill whoever
  //    actually owns the port. execFileSync with an arg list — no shell, no
  //    injection. The server's SIGTERM handler then shuts it down gracefully.
  try {
    const out = execFileSync("lsof", ["-tiTCP:" + PORT, "-sTCP:LISTEN"], {
      stdio: ["ignore", "pipe", "ignore"],
    }).toString().trim();
    for (const pidStr of out.split(/\s+/)) {
      const pid = Number(pidStr);
      if (pid) { try { process.kill(pid, "SIGTERM"); } catch { /* gone */ } }
    }
  } catch { /* lsof unavailable or nothing on the port */ }
  pythonProcess = null;
}

// Wait (bounded) for :4097 to go down so an immediate relaunch doesn't reuse
// a backend that's still dying. The port frees early in graceful shutdown,
// well before MCP teardown finishes, so this returns quickly in practice.
async function waitForBackendGone(timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await checkBackendHealth())) return;
    await new Promise((r) => setTimeout(r, 100));
  }
}

function startPythonBackend(): Promise<void> {
  return new Promise((resolve, reject) => {
    const { cmd, args, env } = getBackendCommand();

    // Packaged: spawn from the user's home dir. `process.resourcesPath` (and
    // therefore `Ziva.app/Contents`) is inside the read-only app bundle on
    // macOS — many tools refuse to write there and the resulting CWD shows
    // up in the UI as "Contents", which looks broken. Dev keeps the original
    // behavior of running from the project root.
    const cwd = app.isPackaged
      ? (() => {
          try { return app.getPath("home"); } catch { return app.getPath("temp"); }
        })()
      : path.resolve(__dirname, "..");

    pythonProcess = spawn(cmd, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      // Own process group/session so before-quit can kill the whole tree
      // (bootloader + server + resource_tracker) with one group signal,
      // instead of orphaning the server that actually holds :4097.
      detached: true,
    });

    let started = false;

    // Persist backend stdout/stderr to ~/.ziva/backend.log so issues like
    // "could not get source code" (which masks the real traceback inside the
    // frozen binary) can be diagnosed without a terminal attached.
    const fs = require("fs");
    const logPath = path.join(app.getPath("home"), ".ziva", "backend.log");
    const appendLog = (line: string) => {
      try { fs.appendFileSync(logPath, line); } catch { /* best-effort */ }
    };

    pythonProcess.stdout?.on("data", (data: Buffer) => {
      const msg = data.toString();
      console.log("[ziva-backend]", msg.trim());
      appendLog(msg);
      if (!started && (msg.includes("Running on") || msg.includes("started"))) {
        started = true;
        resolve();
      }
    });

    pythonProcess.stderr?.on("data", (data: Buffer) => {
      const msg = data.toString();
      console.log("[ziva-backend:err]", msg.trim());
      appendLog("[err] " + msg);
      if (!started && (msg.includes("Running on") || msg.includes("started"))) {
        started = true;
        resolve();
      }
    });

    pythonProcess.on("error", (err) => {
      console.error("Failed to start Python backend:", err);
      reject(err);
    });

    pythonProcess.on("exit", (code) => {
      console.log(`Python backend exited with code ${code}`);
      pythonProcess = null;
    });

    // Poll the backend /status endpoint until it is healthy. This is more
    // reliable than parsing stdout markers, and resolves as soon as the HTTP
    // server is accepting requests. The old 60s stdout-marker fallback
    // waited needlessly when the marker was missing.
    const startTime = Date.now();
    const timeout = 60000;
    const pollInterval = 200;
    const poll = async () => {
      if (started) return;
      if (Date.now() - startTime > timeout) {
        console.warn("[backend] timed out waiting for /status; loading UI anyway");
        started = true;
        resolve();
        return;
      }
      try {
        const healthy = await checkBackendHealth();
        if (healthy) {
          started = true;
          resolve();
          return;
        }
      } catch { /* not ready yet */ }
      setTimeout(poll, pollInterval);
    };
    poll();
  });
}

async function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "Ziva",
    backgroundColor: "#141414",
    titleBarStyle: process.platform === "darwin" ? "hidden" : "default",
    trafficLightPosition: { x: 16, y: 16 },
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
    },
  });

  // Honour the OS proxy for the main window AND every <webview> (they share
  // the default session), so the Agent Browser can reach sites behind a
  // system proxy. MUST be awaited before loadURL, otherwise the first
  // navigation (and any webview created right after) misses the proxy.
  const envProxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
  if (envProxy) {
    await mainWindow.webContents.session.setProxy({ proxyRules: envProxy });
  } else {
    await mainWindow.webContents.session.setProxy({ mode: "system" });
  }

  // Auto-grant the few permissions the renderer actually needs. Without
  // this, navigator.mediaDevices.getUserMedia({ audio: true }) is denied
  // silently on packaged builds and the mic button does nothing. We
  // restrict the allowlist to "media" so the renderer can't quietly gain
  // geolocation / notifications / midi / etc. — each of those is a
  // separate prompt that should still surface to the user.
  mainWindow.webContents.session.setPermissionRequestHandler(
    (_webContents, permission, callback) => {
      if (permission === "media") return callback(true);
      return callback(false);
    },
  );

  // Show a loading screen immediately (data: URL needs no backend) so the
  // window isn't blank while the PyInstaller backend cold-starts. The real
  // UI is swapped in from app.whenReady once the backend is up.
  const loadingHtml = "data:text/html;charset=utf-8," + encodeURIComponent(
    `<html><head><meta charset='utf-8'><style>
      * { margin:0; padding:0; box-sizing:border-box; }
      html,body { height:100%; }
      body {
        display:flex; flex-direction:column; align-items:center; justify-content:center;
        background:#0f0f0f;
        font-family:-apple-system,'SF Pro Display',system-ui,sans-serif;
        color:#e8e8f0; overflow:hidden;
      }
      .wordmark { font-size:30px; font-weight:700; letter-spacing:1px; color:#e8e8f0; margin-bottom:26px; }
      .wordmark .os { color:#8ab4f8; font-weight:500; margin-left:1px; }
      .mark { width:124px; height:52px; animation: breathe 2.8s ease-in-out infinite; }
      @keyframes breathe { 0%,100%{ transform:scale(1); opacity:.92; } 50%{ transform:scale(1.03); opacity:1; } }
      .mark svg { width:100%; height:100%; overflow:visible; }
      .inf { fill:none; stroke-linecap:round; stroke-linejoin:round; stroke-width:5; }
      .track { stroke:rgba(138,180,248,0.12); }
      .base { stroke:url(#g); opacity:0.28; }
      .comet { stroke:url(#g); stroke-dasharray:13 87; animation: trace 1.9s linear infinite; filter:url(#glow); }
      @keyframes trace { to { stroke-dashoffset:-100; } }
      .head { fill:#dbeaff; filter:url(#glow); }
    </style></head><body>
      <div class='wordmark'>Ziva<span class='os'>OS</span></div>
      <div class='mark'>
        <svg viewBox='0 0 120 50'>
          <defs>
            <linearGradient id='g' x1='0' y1='0' x2='1' y2='0'>
              <stop offset='0%' stop-color='#8ab4f8'/>
              <stop offset='100%' stop-color='#cfe0ff'/>
            </linearGradient>
            <filter id='glow' x='-60%' y='-60%' width='220%' height='220%'>
              <feGaussianBlur stdDeviation='2.2' result='b'/>
              <feMerge><feMergeNode in='b'/><feMergeNode in='SourceGraphic'/></feMerge>
            </filter>
          </defs>
          <path class='inf track' pathLength='100' d='M60,25 C60,5 25,5 25,25 C25,45 60,45 60,25 C60,5 95,5 95,25 C95,45 60,45 60,25 Z'/>
          <path class='inf base' pathLength='100' d='M60,25 C60,5 25,5 25,25 C25,45 60,45 60,25 C60,5 95,5 95,25 C95,45 60,45 60,25 Z'/>
          <path class='inf comet' pathLength='100' d='M60,25 C60,5 25,5 25,25 C25,45 60,45 60,25 C60,5 95,5 95,25 C95,45 60,45 60,25 Z'/>
          <circle class='head' r='2.6'>
            <animateMotion dur='1.9s' repeatCount='indefinite' path='M60,25 C60,5 25,5 25,25 C25,45 60,45 60,25 C60,5 95,5 95,25 C95,45 60,45 60,25 Z'/>
          </circle>
        </svg>
      </div>
    </body></html>`
  );
  mainWindow.loadURL(loadingHtml);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // Re-position the embedded browser view when the window is resized — the
  // renderer re-reports the web-area bounds, but this keeps the view pinned
  // during the resize gesture itself.
  mainWindow.on("resize", () => { applyBrowserArea(); });

  return mainWindow;
}

function createWindow() {
  createMainWindow();
  // The CDP bridge is started once in app.whenReady so it survives window
  // recreation on macOS (close window → click dock icon → new window).
}

// ---- IPC ----
ipcMain.handle("get-backend-url", () => `http://127.0.0.1:${PORT}`);

ipcMain.handle("is-electron", () => true);

// Open a URL in the user's external (system default) browser. The in-app
// embedded browser panel was removed, so links the user clicks now delegate
// to the OS browser via shell.openExternal rather than an in-app <webview>.
ipcMain.handle("open-external", (_event, url: string) => {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return;
  shell.openExternal(url);
});

ipcMain.handle("get-cdp-port", () => cdpBridge?.port ?? null);

// ---- Clipboard ----
// Electron 加载的后端是 http://127.0.0.1:4097，浏览器把它视为 non-secure context，
// `navigator.clipboard.writeText` 在那里会被直接拒绝（不是 focus 不够，是
// secure context 这道硬墙）。让渲染器走 IPC 落到主进程，主进程用 native
// `clipboard` 模块写剪贴板，没有 secure context 限制。
ipcMain.handle("clipboard:writeText", (_event, text: string) => {
  if (typeof text !== "string") return false;
  clipboard.writeText(text);
  return true;
});

ipcMain.handle("set-theme", (_event, theme: string) => {
  const color = theme === "light" ? "#f5f5f5" : "#141414";
  mainWindow?.setBackgroundColor(color);
  return true;
});

// Absolute path of the webview preload script. The renderer sets this
// as <webview>.preload so the webview's pages can intercept <a> clicks
// and forward them to the host (see electron/browser-preload.ts).
// `__dirname` is the dist/ folder at runtime (built by tsc).
ipcMain.handle("get-browser-preload-path", () =>
  path.join(__dirname, "browser-preload.js")
);

// Register a webview's webContents as a CDP bridge target. The
// renderer calls this from the Agent Browser tab's `did-attach`
// handler. Returns the targetId the renderer should remember for
// the matching unregister call (and to display in the panel header
// so the user knows the exact WebSocket URL to point chrome-devtools-mcp at).
ipcMain.handle("register-cdp-page", (_event, wcId: number): string | null => {
  const wc = webContents.fromId(wcId);
  if (!wc || !cdpBridge) return null;
  // Keep target=_blank / window.open links inside the agent browser
  // webview instead of handing them to the OS browser. The main
  // process is the reliable path (the webview 'new-window' DOM event
  // fires inconsistently across Electron versions), so we forward the
  // URL to the main window renderer via IPC and let it call
  // openLinkInBrowser(url) — the same entry point the webview preload
  // uses for normal <a> clicks. One handler, two paths.
  wc.setWindowOpenHandler(({ url }) => {
    if (url) mainWindow?.webContents.send("ziva:open-link-in-panel", url);
    return { action: "deny" };
  });
  return cdpBridge.addPage(wc, { type: "page" });
});

ipcMain.handle("unregister-cdp-page", (_event, targetId: string): boolean => {
  if (!cdpBridge) return false;
  cdpBridge.removePage(targetId);
  return true;
});

// ---- Embedded browser (WebContentsView) IPC ----
// Renderer drives the native Chromium views: create/navigate/switch/close +
// back/forward/reload, and reports the web-area rectangle so the main process
// can position the active view over it.
ipcMain.handle("browser-set-area", (_e, b: { x: number; y: number; width: number; height: number }) => {
  browserArea = b;
  applyBrowserArea();
  return true;
});
// ---- Embedded browser helpers ----
function createBrowserTab(url?: string): string {
  if (!mainWindow) return "";
  const id = "bv_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 6);
  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      backgroundThrottling: false,
      // Inject the selection-to-Ziva preload into every web tab so the user
      // can select text and send it (+ URL + screenshot) to the chat.
      preload: path.join(__dirname, "browser-page-preload.js"),
    },
  });
  // Honour the system/env proxy on the embedded browser too.
  const envProxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
  view.webContents.session.setProxy(envProxy ? { proxyRules: envProxy } : { mode: "system" }).catch(() => {});
  // target=_blank / window.open inside the page → open as a new embedded tab
  // (renderer handles it via the event below), never the OS browser.
  view.webContents.setWindowOpenHandler(({ url: u }) => {
    mainWindow?.webContents.send("ziva:browser-new-tab", u);
    return { action: "deny" };
  });
  // Keep the renderer's omnibox/tab title in sync with real navigation.
  view.webContents.on("did-navigate", (_ev, u) => mainWindow?.webContents.send("ziva:browser-nav", { id, url: u }));
  view.webContents.on("did-navigate-in-page", (_ev, u) => mainWindow?.webContents.send("ziva:browser-nav", { id, url: u }));
  view.webContents.on("page-title-updated", (_ev, title) => mainWindow?.webContents.send("ziva:browser-title", { id, title }));
  (view.webContents as any).on("crashed", (_ev: any, killed: boolean) => {
    console.log(`[browser] webContents crashed id=${id} killed=${killed}`);
  });
  (view.webContents as any).on("unresponsive", () => {
    console.log(`[browser] webContents unresponsive id=${id}`);
  });
  (view.webContents as any).on("destroyed", () => {
    console.log(`[browser] webContents destroyed id=${id}`);
  });
  browserViews.set(id, view);
  // Attach the view to the window *before* loading the URL and before
  // registering it with the CDP bridge. A WebContentsView whose webContents
  // has not been added to a BrowserWindow may silently fail to start
  // navigation / CDP domain setup, which is why chrome-devtools-mcp's new_page
  // often ended up stuck on about:blank while manually clicked links worked.
  showBrowserTab(id);
  if (cdpBridge) {
    const tid = cdpBridge.addPage(view.webContents, { type: "page", url });
    (view as any)._cdpTargetId = tid;
  }
  if (url) {
    console.log(`[browser] createBrowserTab calling loadURL=${url}`);
    view.webContents.loadURL(url).then(() => {
      console.log(`[browser] loadURL resolved for ${url}, currentURL=${view.webContents.getURL()}`);
    }).catch((err: any) => {
      console.error(`[browser] loadURL failed for ${url}:`, err?.message || err);
    });
  } else {
    console.log(`[browser] createBrowserTab no url, staying on about:blank`);
  }
  return id;
}

ipcMain.handle("browser-create-tab", (_e, url?: string): string => {
  return createBrowserTab(url);
});
ipcMain.handle("browser-show-tab", (_e, id: string) => { showBrowserTab(id); return true; });
ipcMain.handle("browser-hide-tabs", () => {
  if (!mainWindow) return false;
  for (const view of browserViews.values()) {
    try { mainWindow.contentView.removeChildView(view); } catch {}
  }
  activeBrowserTab = null;
  return true;
});
ipcMain.handle("browser-navigate", (_e, id: string, url: string) => {
  browserViews.get(id)?.webContents.loadURL(url);
  return true;
});
ipcMain.handle("browser-nav", (_e, id: string, kind: "back" | "forward" | "reload") => {
  const wc = browserViews.get(id)?.webContents;
  if (!wc) return false;
  if (kind === "back") wc.goBack();
  else if (kind === "forward") wc.goForward();
  else wc.reload();
  return true;
});
ipcMain.handle("browser-close-tab", (_e, id: string) => {
  const view = browserViews.get(id);
  if (view && (view as any)._cdpTargetId && cdpBridge) cdpBridge.removePage((view as any)._cdpTargetId);
  destroyBrowserTab(id);
  return true;
});

// A web tab's selection-to-Ziva button fired. Find which tab the message
// came from (by webContents), grab the real URL server-side (don't trust
// the page), screenshot the selection rect, and forward {text,url,screenshot}
// to the renderer so it lands in the composer.
ipcMain.on("ziva:page-selection", async (_event, payload: { text: string; rect: { x: number; y: number; width: number; height: number } }) => {
  const wc = _event.sender;
  let id: string | null = null;
  for (const [tid, view] of browserViews) {
    if ((view as any).webContents === wc) { id = tid; break; }
  }
  if (!id) return;
  const url = wc.getURL();
  let screenshotDataUrl = "";
  try {
    const view = browserViews.get(id);
    if (view) {
      const img = await (view as any).webContents.capturePage(payload.rect);
      screenshotDataUrl = img.toDataURL();
    }
  } catch (err) {
    console.error("[browser] selection capturePage failed:", err);
  }
  mainWindow?.webContents.send("ziva:browser-selection", {
    text: payload.text,
    url,
    screenshotDataUrl,
  });
});

let backendStarting = false;

async function ensureBackendReady(): Promise<void> {
  const alreadyRunning = await checkBackendHealth();
  if (alreadyRunning) {
    console.log("[backend] reusing existing backend on port", PORT);
    return;
  }
  if (backendStarting) {
    // Wait for the in-flight startup to finish instead of spawning a second process.
    const waitStart = Date.now();
    while (backendStarting && Date.now() - waitStart < 60000) {
      await new Promise((r) => setTimeout(r, 200));
    }
    if (await checkBackendHealth()) return;
    throw new Error("Backend startup failed");
  }
  backendStarting = true;
  try {
    await startPythonBackend();
  } finally {
    backendStarting = false;
  }
}

// Wait for the STT (voice input) model to finish warming up before swapping
// the loading screen for the real UI, so the user never lands on a chat
// window whose mic button doesn't work yet. The mlx-whisper cold start
// (load 459MB weights + JIT-compile Metal kernels) takes ~10–15s on every
// launch; the model is already on disk, so there's no download — just load
// + compile. We poll /api/stt/status and proceed once it reaches a terminal
// state. A 120s ceiling ensures a stuck warmup never leaves the app stuck
// on the loading screen forever.
async function waitForSttReady(): Promise<void> {
  const start = Date.now();
  const timeout = 120000;
  const interval = 500;
  const query = (): Promise<string> => new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PORT}/api/stt/status`, { timeout: 2000 }, (res) => {
      let body = "";
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => {
        try { resolve((JSON.parse(body).status as string) || ""); } catch { resolve(""); }
      });
    });
    req.on("error", () => resolve(""));
    req.on("timeout", () => { req.destroy(); resolve(""); });
  });
  while (Date.now() - start < timeout) {
    let status = "";
    try { status = await query(); } catch { /* keep polling */ }
    // ready = success; error / needs_download won't improve by waiting.
    // idle / warming / "" → keep polling.
    if (status === "ready") return;
    if (status === "error" || status === "needs_download") {
      console.warn(`[stt] warmup ended in non-ready state: ${status}; loading UI anyway`);
      return;
    }
    await new Promise((r) => setTimeout(r, interval));
  }
  console.warn("[stt] timed out waiting for warmup; loading UI anyway");
}

function loadBackendUrlInto(window: BrowserWindow | null): void {
  if (!window) return;
  const backendUrl = `http://127.0.0.1:${PORT}`;
  window.webContents.on("did-fail-load", () => {
    setTimeout(() => window.loadURL(backendUrl), 1000);
  });
  window.loadURL(backendUrl);
}

// App lifecycle
app.whenReady().then(async () => {
  // Set up a dedicated session for the Agent Browser webview and honour the
  // system proxy there too. Some Electron builds don't propagate the default
  // session's proxy settings to webviews reliably; setting it explicitly on
  // the partition's session avoids that.
  try {
    const browserSession = session.fromPartition(BROWSER_PARTITION);
    const envProxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
    if (envProxy) {
      await browserSession.setProxy({ proxyRules: envProxy });
    } else {
      await browserSession.setProxy({ mode: "system" });
    }
    console.log("[proxy] system proxy enabled for browser session");
  } catch (err) {
    console.error("[proxy] failed to set system proxy on browser session:", err);
  }

  createWindow(); // show the loading window immediately (not a blank screen)
  try {
    await ensureBackendReady();
    // Hold the loading screen until voice input is usable — see waitForSttReady.
    await waitForSttReady();
    loadBackendUrlInto(mainWindow);
  } catch (err) {
    console.error("Failed to start backend:", err);
  }

  // CDP bridge starts once; it must outlive individual BrowserWindows so that
  // closing and reopening the window on macOS doesn't break the agent browser.
  cdpBridge = new CdpBridge({ port: CDP_PORT, host: "127.0.0.1" });
  cdpBridge.onEnsurePage = async (url?: string) => {
    if (!mainWindow || !cdpBridge) return;
    console.log(`[main] onEnsurePage url=${JSON.stringify(url)}`);
    // Create the native view directly in the main process instead of asking
    // the renderer to do it asynchronously. This removes the race that caused
    // CDP Target.createTarget to return the wrong/stale targetId or
    // about:blank when the URL was lost in the IPC round-trip.
    const id = createBrowserTab(url || "about:blank?t=" + Date.now());
    const view = browserViews.get(id);
    const targetId = (view as any)?._cdpTargetId;
    // Notify the renderer about the tab so the shell UI stays in sync.
    // The renderer may not be ready yet; its preload buffers the event.
    mainWindow.webContents.send("ziva:browser-tab-created", { id, url, targetId });
    return targetId;
  };
  cdpBridge.start().then(() => {
    const port = cdpBridge!.port;
    console.log(
      `[cdp-bridge] To connect chrome-devtools-mcp, add to your MCP config:\n` +
      `  "chrome-devtools": {\n` +
      `    "command": "npx",\n` +
      `    "args": ["-y", "chrome-devtools-mcp@latest", "--browser-url=http://127.0.0.1:${port}"]\n` +
      `  }`,
    );
  }).catch((err) => {
    console.error("[cdp-bridge] failed to start:", err);
  });

  app.on("activate", async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      try {
        await ensureBackendReady();
        loadBackendUrlInto(mainWindow);
      } catch (err) {
        console.error("Failed to connect to backend on reactivate:", err);
      }
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

let isQuitting = false;
app.on("before-quit", (event) => {
  if (isQuitting) return;
  // preventDefault synchronously, then do the async backend teardown before
  // actually quitting — otherwise the relaunched/next instance would race the
  // dying backend for :4097 and silently reuse it.
  event.preventDefault();
  isQuitting = true;
  (async () => {
    for (const id of Array.from(browserViews.keys())) destroyBrowserTab(id);
    killBackendTree();
    await waitForBackendGone(2000);
    if (cdpBridge) {
      cdpBridge.stop();
      cdpBridge = null;
    }
    app.quit(); // re-fires before-quit; isQuitting short-circuits the guard
  })();
});

// Full restart: relaunch the app and quit. before-quit (above) kills the whole
// backend tree and waits for :4097 to free, so the relaunched instance spawns
// a fresh backend instead of reusing the dying one. Renderer invokes this
// (e.g. a "Restart" menu item) via ipcRenderer.invoke("restart-app").
ipcMain.handle("restart-app", () => {
  app.relaunch();
  app.quit();
});
