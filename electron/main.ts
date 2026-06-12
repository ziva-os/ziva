import { app, BrowserWindow, ipcMain } from "electron";
import * as path from "path";
import { spawn, ChildProcess } from "child_process";

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
const PORT = 4097;

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

function createWindow() {
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
}

// IPC handlers
ipcMain.handle("get-backend-url", () => `http://127.0.0.1:${PORT}`);

ipcMain.handle("is-electron", () => true);

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
});
