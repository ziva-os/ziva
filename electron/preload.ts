import { contextBridge, ipcRenderer } from "electron";

// Webview → renderer link bridge: the main process forwards
// target=_blank / window.open events here (via webContents.send).
// The webview's own <a> click interception uses sendToHost, which is
// delivered through the webview element's `ipc-message` event in the
// renderer — that path is wired in renderBrowserTab(). Both paths end
// up calling the same handler.
let _openLinkInPanelHandler: ((url: string) => void) | null = null;
ipcRenderer.on("ziva:open-link-in-panel", (_event, url: string) => {
  if (_openLinkInPanelHandler) _openLinkInPanelHandler(url);
});

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
  // Webview preload path, async via main process. renderBrowserTab caches
  // this once at init and uses it to set <webview>.preload.
  getBrowserPreloadPath: () => ipcRenderer.invoke("get-browser-preload-path"),
  // Partition to use for the Agent Browser webview so it gets the dedicated
  // session with explicit system proxy settings.
  browserPartition: "persist:ziva-browser",
  // Register a single renderer-side callback that handles every link
  // forwarded by the main process (target=_blank / window.open).
  setOpenLinkInPanelHandler: (cb: (url: string) => void) => {
    _openLinkInPanelHandler = cb;
  },
});
