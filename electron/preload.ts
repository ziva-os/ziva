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

// Embedded-browser event channels: the main process (which owns the native
// WebContentsView per web tab) pushes navigation/title/window-open events here
// so the renderer's tab strip + omnibox stay in sync with the real Chromium view.
let _browserNewTabHandler: any = null;
let _browserNavHandler: any = null;
let _browserTitleHandler: any = null;
ipcRenderer.on("ziva:browser-new-tab", (_e, url: string) => { if (_browserNewTabHandler) _browserNewTabHandler(url); });
ipcRenderer.on("ziva:browser-nav", (_e, e: any) => { if (_browserNavHandler) _browserNavHandler(e); });
ipcRenderer.on("ziva:browser-title", (_e, e: any) => { if (_browserTitleHandler) _browserTitleHandler(e); });

contextBridge.exposeInMainWorld("electronAPI", {
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),
  isElectron: () => ipcRenderer.invoke("is-electron"),
  openExternal: (url: string) => ipcRenderer.invoke("open-external", url),
  getCdpPort: () => ipcRenderer.invoke("get-cdp-port"),
  // ---- Embedded Chromium browser (WebContentsView) ----
  // The renderer is the host shell; it asks the main process to create/show/
  // navigate/close native browser views and reports the rectangle where the
  // view should be positioned.
  browserSetArea: (b: { x: number; y: number; width: number; height: number }) =>
    ipcRenderer.invoke("browser-set-area", b),
  browserCreateTab: (url?: string) => ipcRenderer.invoke("browser-create-tab", url),
  browserShowTab: (id: string) => ipcRenderer.invoke("browser-show-tab", id),
  browserNavigate: (id: string, url: string) => ipcRenderer.invoke("browser-navigate", id, url),
  browserNav: (id: string, kind: "back" | "forward" | "reload") => ipcRenderer.invoke("browser-nav", id, kind),
  browserCloseTab: (id: string) => ipcRenderer.invoke("browser-close-tab", id),
  onBrowserNewTab: (cb: (url: string) => void) => { _browserNewTabHandler = cb; },
  onBrowserNav: (cb: (e: { id: string; url: string }) => void) => { _browserNavHandler = cb; },
  onBrowserTitle: (cb: (e: { id: string; title: string }) => void) => { _browserTitleHandler = cb; },
});
