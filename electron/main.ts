import { app, BrowserWindow, ipcMain, webContents } from "electron";
import * as path from "path";
import { spawn, ChildProcess } from "child_process";
import { CdpBridge } from "./cdp-bridge";

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
let cdpBridge: CdpBridge | null = null;
let chromeProcess: ChildProcess | null = null;
const PORT = 4097;
// CDP bridge (for the Agent Browser <webview>) — moved off 9222 so the real
// Chrome below can use 9222. Real Chrome = native speed, no Electron
// debugger overhead.
const CDP_PORT = Number(process.env.ZIVA_CDP_PORT || 9223);
const CHROME_DEBUG_PORT = 9222;

function getBackendCommand(): { cmd: string; args: string[]; env: NodeJS.ProcessEnv } {
  const isDev = !app.isPackaged;
  // Default workspace for the packaged app: the user's Documents folder, so
  // sessions / repos live somewhere the user can browse in Finder instead of
  // inside the read-only Ziva.app bundle. Falls back to HOME if Documents
  // isn't available (very unusual). Dev mode keeps the old behavior of
  // cwd-relative ".", which on `electron .` resolves to the ziva repo root
  // and is what local development expects.
  let workspaceArg: string | null = null;
  if (!isDev) {
    // Prefer the last-used workspace so reopening the app lands back in the
    // project the user was working in — otherwise every launch defaults to
    // the Documents folder, which shows up as a stray empty workspace in the
    // sidebar. First launch (no recent workspaces yet) falls back to
    // Documents, which is Finder-browsable and writable (unlike the app bundle).
    try {
      const fs = require("fs");
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
    const projectRoot = path.resolve(__dirname, "..");
    return {
      cmd: "python3",
      args: ["-m", "ziva_runtime", ...baseArgs],
      env: { ...env, PYTHONPATH: projectRoot },
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

function spawnDebugChrome(): void {
  const fs = require("fs");
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  const exe = candidates.find((p) => { try { return fs.existsSync(p); } catch { return false; } });
  if (!exe) {
    console.error("[chrome] Google Chrome not found — chrome-devtools-mcp will start its own");
    return;
  }
  const profileDir = path.join(app.getPath("home"), ".ziva", "chrome-debug-profile");
  chromeProcess = spawn(exe, [
    `--remote-debugging-port=${CHROME_DEBUG_PORT}`,
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
  ], { detached: true, stdio: "ignore" });
  chromeProcess.unref();
  console.log(`[chrome] real Chrome started on port ${CHROME_DEBUG_PORT}`);
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

    // Fallback: wait up to 60s for the "Running on" marker. The PyInstaller
    // backend can take 10-20s to import on first launch (cold start); the
    // old 5s fallback resolved before the server was up, so loadURL hit a
    // dead port → the white screen on first open. 60s only fires if the
    // backend truly hung.
    setTimeout(() => {
      if (!started) {
        started = true;
        resolve();
      }
    }, 60000);
  });
}

async function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "Ziva",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
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
  await mainWindow.webContents.session.setProxy({ mode: "system" });

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
    "<html><body style='margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#1e1e1e;color:#aaa;font-family:-apple-system,system-ui,sans-serif'><div style='font-size:28px;font-weight:600'>Ziva</div><div style='color:#666;margin-top:10px;font-size:13px'>启动中…</div></body></html>"
  );
  mainWindow.loadURL(loadingHtml);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  return mainWindow;
}

function createWindow() {
  createMainWindow();

  // CDP bridge starts now; pages will be registered lazily by the
  // renderer as webviews come online (e.g. the Agent Browser tab).
  // The main Ziva UI's webContents is intentionally NOT exposed —
  // the agent shouldn't be able to navigate or inspect the chat
  // surface it's embedded in.
  cdpBridge = new CdpBridge({ port: CDP_PORT, host: "127.0.0.1" });
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
}

// ---- IPC ----
ipcMain.handle("get-backend-url", () => `http://127.0.0.1:${PORT}`);

ipcMain.handle("is-electron", () => true);

ipcMain.handle("get-cdp-port", () => cdpBridge?.port ?? null);

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

// App lifecycle
app.whenReady().then(async () => {
  spawnDebugChrome();
  createWindow();  // show the loading window immediately (not a blank screen)
  try {
    await startPythonBackend();
    // Backend is up — swap the loading screen for the real UI.
    const backendUrl = `http://127.0.0.1:${PORT}`;
    mainWindow?.webContents.on("did-fail-load", () => {
      setTimeout(() => mainWindow?.loadURL(backendUrl), 1000);
    });
    mainWindow?.loadURL(backendUrl);
  } catch (err) {
    console.error("Failed to start backend:", err);
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  if (pythonProcess) {
    pythonProcess.kill("SIGTERM");
    pythonProcess = null;
  }
  if (chromeProcess) {
    try { chromeProcess.kill("SIGTERM"); } catch {}
    chromeProcess = null;
  }
  if (cdpBridge) {
    cdpBridge.stop();
    cdpBridge = null;
  }
});
