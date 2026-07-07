import { contextBridge, ipcRenderer } from "electron";


// Embedded-browser event channels: the main process (which owns the native
// WebContentsView per web tab) pushes navigation/title/window-open events here
// so the renderer's tab strip + omnibox stay in sync with the real Chromium view.
let _browserNewTabHandler: any = null;
let _browserTabCreatedHandler: any = null;
let _browserNavHandler: any = null;
let _browserTitleHandler: any = null;
const _pendingNewTab: any[] = [];
const _pendingTabCreated: any[] = [];
const _pendingNav: any[] = [];
const _pendingTitle: any[] = [];
ipcRenderer.on("ziva:browser-new-tab", (_e, payload: any) => {
  if (_browserNewTabHandler) _browserNewTabHandler(payload);
  else _pendingNewTab.push(payload);
});
ipcRenderer.on("ziva:browser-tab-created", (_e, e: any) => {
  if (_browserTabCreatedHandler) _browserTabCreatedHandler(e);
  else _pendingTabCreated.push(e);
});
ipcRenderer.on("ziva:browser-nav", (_e, e: any) => {
  if (_browserNavHandler) _browserNavHandler(e);
  else _pendingNav.push(e);
});
ipcRenderer.on("ziva:browser-title", (_e, e: any) => {
  if (_browserTitleHandler) _browserTitleHandler(e);
  else _pendingTitle.push(e);
});

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
  browserHideTabs: () => ipcRenderer.invoke("browser-hide-tabs"),
  browserNavigate: (id: string, url: string) => ipcRenderer.invoke("browser-navigate", id, url),
  browserNav: (id: string, kind: "back" | "forward" | "reload") => ipcRenderer.invoke("browser-nav", id, kind),
  browserCloseTab: (id: string) => ipcRenderer.invoke("browser-close-tab", id),
  onBrowserNewTab: (cb: (payload: string | { url: string; force?: boolean }) => void) => {
    _browserNewTabHandler = cb;
    while (_pendingNewTab.length) cb(_pendingNewTab.shift());
  },
  onBrowserTabCreated: (cb: (e: { id: string; url?: string; targetId?: string }) => void) => {
    _browserTabCreatedHandler = cb;
    while (_pendingTabCreated.length) cb(_pendingTabCreated.shift());
  },
  onBrowserNav: (cb: (e: { id: string; url: string }) => void) => {
    _browserNavHandler = cb;
    while (_pendingNav.length) cb(_pendingNav.shift());
  },
  onBrowserTitle: (cb: (e: { id: string; title: string }) => void) => {
    _browserTitleHandler = cb;
    while (_pendingTitle.length) cb(_pendingTitle.shift());
  },
});
