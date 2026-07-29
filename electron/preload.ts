import { contextBridge, ipcRenderer, webUtils } from "electron";


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
// Web-tab "send selection to Ziva": main process forwards {text, url,
// screenshotDataUrl} from a web page's selection.
let _browserSelectionHandler: any = null;
const _pendingSelection: any[] = [];
ipcRenderer.on("ziva:browser-selection", (_e, payload: any) => {
  if (_browserSelectionHandler) _browserSelectionHandler(payload);
  else _pendingSelection.push(payload);
});

contextBridge.exposeInMainWorld("electronAPI", {
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),
  isElectron: () => ipcRenderer.invoke("is-electron"),
  openExternal: (url: string) => ipcRenderer.invoke("open-external", url),
  getCdpPort: () => ipcRenderer.invoke("get-cdp-port"),
  setTheme: (theme: string) => ipcRenderer.invoke("set-theme", theme),
  // Resolve the absolute path of a file chosen via <input type="file">.
  // Electron 32 deprecated and 35 removed File.path; webUtils.getPathForFile
  // is the supported replacement. Lets the composer skip copying a local file
  // into the attachments dir and just hand the runtime its real path.
  getPathForFile: (file: File): string => webUtils.getPathForFile(file),
  // ---- Clipboard ----
  // Renderer 加载在 http://127.0.0.1:4097，被 Chromium 视作 non-secure context，
  // `navigator.clipboard.writeText` 在那里会被拒。提供一个统一入口：
  //   1. secure context 且有原生 Clipboard API → 直接走浏览器
  //   2. 否则走 IPC 落到主进程 Electron.clipboard 模块
  //   3. 都没拿到（web dev / 旧版 Chromium）→ 再 fallback execCommand("copy")
  copyText: async (text: string): Promise<boolean> => {
    if (typeof text !== "string") return false;
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        // fall through to IPC
      }
    }
    try {
      const ok: boolean = await ipcRenderer.invoke("clipboard:writeText", text);
      return ok;
    } catch {
      return false;
    }
  },
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
  // Reload the desktop (Electron + python backend) so newly added plugins
  // or skill changes take effect. Triggered by the renderer's `/restart`
  // slash command — same UX as the IM bridge's `/restart`.
  restartZiva: () => ipcRenderer.invoke("restart-ziva"),
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
  onBrowserSelection: (cb: (payload: { text: string; url: string; screenshotDataUrl: string }) => void) => {
    _browserSelectionHandler = cb;
    while (_pendingSelection.length) cb(_pendingSelection.shift());
  },
});
