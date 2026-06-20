import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("electronAPI", {
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),
  isElectron: () => ipcRenderer.invoke("is-electron"),
  // CDP bridge: the renderer registers the Agent Browser webview as a
  // target, and asks the main process for the port chrome-devtools-mcp
  // should be configured with.
  getCdpPort: () => ipcRenderer.invoke("get-cdp-port"),
  registerCdpPage: (wcId: number) =>
    ipcRenderer.invoke("register-cdp-page", wcId),
  unregisterCdpPage: (targetId: string) =>
    ipcRenderer.invoke("unregister-cdp-page", targetId),
});
