"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const path = __importStar(require("path"));
const child_process_1 = require("child_process");
const cdp_bridge_1 = require("./cdp-bridge");
let mainWindow = null;
let pythonProcess = null;
let cdpBridge = null;
const PORT = 4097;
// chrome-devtools-mcp's --browser-url points here. Override with
// ZIVA_CDP_PORT=<n> if 9222 is taken.
const CDP_PORT = Number(process.env.ZIVA_CDP_PORT || 9222);
function getBackendCommand() {
    const isDev = !electron_1.app.isPackaged;
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
function startPythonBackend() {
    return new Promise((resolve, reject) => {
        const { cmd, args, env } = getBackendCommand();
        pythonProcess = (0, child_process_1.spawn)(cmd, args, {
            cwd: path.resolve(electron_1.app.isPackaged ? process.resourcesPath : __dirname, ".."),
            env,
            stdio: ["pipe", "pipe", "pipe"],
        });
        let started = false;
        pythonProcess.stdout?.on("data", (data) => {
            const msg = data.toString();
            console.log("[ziva-backend]", msg.trim());
            if (!started && (msg.includes("Running on") || msg.includes("started"))) {
                started = true;
                resolve();
            }
        });
        pythonProcess.stderr?.on("data", (data) => {
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
async function createMainWindow() {
    mainWindow = new electron_1.BrowserWindow({
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
    cdpBridge = new cdp_bridge_1.CdpBridge({ port: CDP_PORT, host: "127.0.0.1" });
    // When a CDP client (chrome-devtools-mcp) asks for a page but the Agent
    // Browser tab isn't open, lazily create a standalone browser window so
    // the agent has a page to drive instead of failing with "no page target".
    cdpBridge.onEnsurePage = () => {
        const win = new electron_1.BrowserWindow({
            width: 1000,
            height: 700,
            title: "Ziva Browser",
            webPreferences: { contextIsolation: true, nodeIntegration: false },
        });
        win.loadURL("about:blank");
        // Keep links inside this window (same behaviour as the Agent Browser
        // webview). Proxy is already handled via the default session setProxy.
        win.webContents.setWindowOpenHandler(({ url }) => {
            if (url)
                win.webContents.loadURL(url);
            return { action: "deny" };
        });
        cdpBridge.addPage(win.webContents);
    };
    cdpBridge.start().then(() => {
        const port = cdpBridge.port;
        console.log(`[cdp-bridge] To connect chrome-devtools-mcp, add to your MCP config:\n` +
            `  "chrome-devtools": {\n` +
            `    "command": "npx",\n` +
            `    "args": ["-y", "chrome-devtools-mcp@latest", "--browser-url=http://127.0.0.1:${port}"]\n` +
            `  }`);
    }).catch((err) => {
        console.error("[cdp-bridge] failed to start:", err);
    });
}
// ---- IPC ----
electron_1.ipcMain.handle("get-backend-url", () => `http://127.0.0.1:${PORT}`);
electron_1.ipcMain.handle("is-electron", () => true);
electron_1.ipcMain.handle("get-cdp-port", () => cdpBridge?.port ?? null);
// Register a webview's webContents as a CDP bridge target. The
// renderer calls this from the Agent Browser tab's `did-attach`
// handler. Returns the targetId the renderer should remember for
// the matching unregister call (and to display in the panel header
// so the user knows the exact WebSocket URL to point chrome-devtools-mcp at).
electron_1.ipcMain.handle("register-cdp-page", (_event, wcId) => {
    const wc = electron_1.webContents.fromId(wcId);
    if (!wc || !cdpBridge)
        return null;
    // Keep target=_blank / window.open links INSIDE the agent browser webview
    // instead of handing them to the OS browser. (The renderer also attaches
    // a 'new-window' listener, but this main-process handler is the reliable
    // path — the webview 'new-window' DOM event fires inconsistently across
    // Electron versions.)
    wc.setWindowOpenHandler(({ url }) => {
        if (url)
            wc.loadURL(url);
        return { action: "deny" };
    });
    return cdpBridge.addPage(wc, { type: "page" });
});
electron_1.ipcMain.handle("unregister-cdp-page", (_event, targetId) => {
    if (!cdpBridge)
        return false;
    cdpBridge.removePage(targetId);
    return true;
});
// App lifecycle
electron_1.app.whenReady().then(async () => {
    try {
        await startPythonBackend();
    }
    catch (err) {
        console.error("Failed to start backend, launching window anyway:", err);
    }
    createWindow();
    electron_1.app.on("activate", () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});
electron_1.app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
        electron_1.app.quit();
    }
});
electron_1.app.on("before-quit", () => {
    if (pythonProcess) {
        pythonProcess.kill("SIGTERM");
        pythonProcess = null;
    }
    if (cdpBridge) {
        cdpBridge.stop();
        cdpBridge = null;
    }
});
