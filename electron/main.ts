import { app, BrowserWindow, ipcMain, webContents } from "electron";
import * as path from "path";
import { spawn, ChildProcess } from "child_process";
import { CdpBridge } from "./cdp-bridge";

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
let cdpBridge: CdpBridge | null = null;
const PORT = 4097;
// chrome-devtools-mcp's --browser-url points here. Override with
// ZIVA_CDP_PORT=<n> if 9222 is taken.
const CDP_PORT = Number(process.env.ZIVA_CDP_PORT || 9222);

function getBackendCommand(): { cmd: string; args: string[]; env: NodeJS.ProcessEnv } {
  const isDev = !app.isPackaged;
  const baseArgs = ["desktop", "serve", "--port", String(PORT)];

  if (isDev) {
    const projectRoot = path.resolve(__dirname, "..");
    return {
      cmd: "python3",
      args: ["-m", "ziva_runtime", ...baseArgs],
      env: { ...process.env, PYTHONPATH: projectRoot },
    };
  }

  // Packaged: use PyInstaller binary bundled in Resources
  const ext = process.platform === "win32" ? ".exe" : "";
  const backendPath = path.join(process.resourcesPath, `ziva-backend${ext}`);
  return {
    cmd: backendPath,
    args: baseArgs,
    env: { ...process.env },
  };
}

function startPythonBackend(): Promise<void> {
  return new Promise((resolve, reject) => {
    const { cmd, args, env } = getBackendCommand();

    pythonProcess = spawn(cmd, args, {
      cwd: path.resolve(app.isPackaged ? process.resourcesPath : __dirname, ".."),
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let started = false;

    pythonProcess.stdout?.on("data", (data: Buffer) => {
      const msg = data.toString();
      console.log("[ziva-backend]", msg.trim());
      if (!started && (msg.includes("Running on") || msg.includes("started"))) {
        started = true;
        resolve();
      }
    });

    pythonProcess.stderr?.on("data", (data: Buffer) => {
      const msg = data.toString();
      console.log("[ziva-backend:err]", msg.trim());
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

    // Fallback timeout: assume backend started after 5s
    setTimeout(() => {
      if (!started) {
        started = true;
        resolve();
      }
    }, 5000);
  });
}

function createMainWindow() {
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

  mainWindow.loadURL(`http://127.0.0.1:${PORT}`);

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

// Register a webview's webContents as a CDP bridge target. The
// renderer calls this from the Agent Browser tab's `did-attach`
// handler. Returns the targetId the renderer should remember for
// the matching unregister call (and to display in the panel header
// so the user knows the exact WebSocket URL to point chrome-devtools-mcp at).
ipcMain.handle("register-cdp-page", (_event, wcId: number): string | null => {
  const wc = webContents.fromId(wcId);
  if (!wc || !cdpBridge) return null;
  return cdpBridge.addPage(wc, { type: "page" });
});

ipcMain.handle("unregister-cdp-page", (_event, targetId: string): boolean => {
  if (!cdpBridge) return false;
  cdpBridge.removePage(targetId);
  return true;
});

// App lifecycle
app.whenReady().then(async () => {
  try {
    await startPythonBackend();
  } catch (err) {
    console.error("Failed to start backend, launching window anyway:", err);
  }
  createWindow();

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
  if (cdpBridge) {
    cdpBridge.stop();
    cdpBridge = null;
  }
});
