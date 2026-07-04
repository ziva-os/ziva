/**
 * Browser shell — embeds a real Chromium browser in the desktop app.
 *
 * Two modes:
 *  • Electron: each web tab is a native WebContentsView owned by the MAIN
 *    process (a true embedded browser, not a DOM <webview>). This renderer is
 *    the host shell — a tab strip + omnibox that drives the main-process views
 *    over IPC, plus a "web area" div whose on-screen rectangle the main process
 *    reads (browser-set-area) to position the WebContentsView over.
 *  • Web/dev (no Electron): falls back to an <iframe> via /api/proxy so the UI
 *    is still drivable in a browser, though it's not a "real" embedded browser.
 *
 * Ziva is the leftmost pinned tab (the chat UI). Web tabs are real browser
 * panes. Links from chat / from web pages open as new web tabs.
 */

export interface BrowserTab {
  id: string;
  type: "ziva" | "web";
  url?: string;
  title: string;
  el?: HTMLElement; // the <iframe> for web-mode fallback (unused in Electron)
}

const ZIVA_TAB_ID = "ziva";
let tabs: BrowserTab[] = [{ id: ZIVA_TAB_ID, type: "ziva", title: "Ziva" }];
let activeTabId = ZIVA_TAB_ID;
let _seq = 0;

let strip: HTMLElement | null = null;
let omnibox: HTMLElement | null = null;
let zivaLayout: HTMLElement | null = null;
let webArea: HTMLElement | null = null;

const isElectron = !!(window as any).electronAPI;
const ea: any = (window as any).electronAPI;

function nextId(): string { _seq += 1; return "web_" + _seq; }

/** Build the shell: tab strip + omnibox + body (ziva-layout | web-area). */
export function initBrowserShell(): void {
  const app = document.getElementById("app");
  if (!app) return;
  const layout = app.querySelector(".ziva-layout") as HTMLElement | null;
  if (layout) layout.id = "zivaLayout";
  zivaLayout = layout;

  const shell = document.createElement("div");
  shell.className = "browser-shell";
  strip = document.createElement("div");
  strip.id = "browserTabstrip";
  strip.className = "browser-tabstrip";
  omnibox = document.createElement("div");
  omnibox.id = "browserOmnibox";
  omnibox.className = "browser-omnibox";
  const body = document.createElement("div");
  body.className = "browser-body";
  webArea = document.createElement("div");
  webArea.id = "browserWebArea";
  webArea.className = "browser-web-area";

  app.replaceChildren(shell);
  shell.append(strip, omnibox, body);
  if (layout) body.append(layout);
  body.append(webArea);

  renderTabstrip();
  renderOmnibox();
  applyActive();

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(reportBrowserArea);
    ro.observe(webArea);
  }
  window.addEventListener("resize", reportBrowserArea);
  setTimeout(reportBrowserArea, 100);

  // Main → renderer events: target=_blank in a page → new tab; nav/title sync.
  ea?.onBrowserNewTab?.((url: string) => openInBrowserTab(url));
  ea?.onBrowserNav?.((e: { id: string; url: string }) => {
    const t = tabs.find(x => x.id === e.id);
    if (t) { t.url = e.url; t.title = prettyHost(e.url); if (t.id === activeTabId) renderOmnibox(); renderTabstrip(); }
  });
  ea?.onBrowserTitle?.((e: { id: string; title: string }) => {
    const t = tabs.find(x => x.id === e.id);
    if (t && e.title) { t.title = e.title; renderTabstrip(); }
  });

  // Keyboard shortcuts: Cmd/Ctrl+T new tab, +W close, +L focus omnibox.
  document.addEventListener("keydown", (e: KeyboardEvent) => {
    const mod = e.metaKey || e.ctrlKey;
    if (!mod) return;
    const k = e.key.toLowerCase();
    if (k === "t") { e.preventDefault(); createWebTab(); }
    else if (k === "w") { e.preventDefault(); closeTab(activeTabId); }
    else if (k === "l") {
      e.preventDefault();
      const inp = omnibox?.querySelector(".bt-url") as HTMLInputElement | null;
      inp?.focus(); inp?.select();
    }
  });
}

/** Open a URL in a web tab: reuse a tab on that URL, else make a new one. */
export function openInBrowserTab(url: string): void {
  const existing = tabs.find(t => t.type === "web" && t.url === url);
  if (existing) { activateTab(existing.id); return; }
  createWebTab(url);
}

function createWebTab(url?: string): void {
  const tab: BrowserTab = { id: nextId(), type: "web", url, title: url ? prettyHost(url) : "New Tab" };
  if (!isElectron) {
    // Web/dev fallback: an <iframe> via the proxy. Not a real browser, just
    // keeps the UI drivable outside Electron.
    const f = document.createElement("iframe");
    f.className = "web-frame";
    f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups");
    f.src = url ? "/api/proxy?url=" + encodeURIComponent(url) : "about:blank";
    tab.el = f;
    if (webArea) webArea.appendChild(f);
  }
  tabs.push(tab);
  activateTab(tab.id);
  if (isElectron && ea?.browserCreateTab) {
    ea.browserCreateTab(url).then((id: string) => {
      // The main process allocated the real view; keep our id in sync by
      // reusing the returned id for subsequent navigate/show calls.
      const t = tabs.find(x => x === tab);
      if (t) { (t as any).mainId = id; }
    });
  }
}

function activateTab(id: string): void {
  activeTabId = id;
  if (isElectron) {
    const t = tabs.find(x => x.id === id);
    const mainId = t && (t as any).mainId;
    if (t?.type === "web" && mainId && ea?.browserShowTab) ea.browserShowTab(mainId);
  } else {
    // web fallback: show the active iframe, hide others
    tabs.forEach(t => { if (t.el) (t.el as HTMLElement).style.display = (t.id === id) ? "" : "none"; });
  }
  applyActive();
  renderTabstrip();
  renderOmnibox();
}

function closeTab(id: string): void {
  if (id === ZIVA_TAB_ID) return; // pinned
  const idx = tabs.findIndex(t => t.id === id);
  if (idx < 0) return;
  const tab = tabs[idx];
  if (isElectron && (tab as any).mainId && ea?.browserCloseTab) ea.browserCloseTab((tab as any).mainId);
  tab.el?.remove();
  tabs.splice(idx, 1);
  if (activeTabId === id) {
    const fallback = tabs[Math.min(idx, tabs.length - 1)] || tabs[0];
    activateTab(fallback.id);
  } else {
    renderTabstrip();
  }
}

/** Show/hide ziva-layout vs web-area based on the active tab. */
function applyActive(): void {
  const active = tabs.find(t => t.id === activeTabId);
  const isWeb = active?.type === "web";
  if (zivaLayout) zivaLayout.style.display = isWeb ? "none" : "";
  if (webArea) webArea.style.display = isWeb ? "" : "none";
  requestAnimationFrame(reportBrowserArea);
}

function navigateActive(url: string): void {
  const active = tabs.find(t => t.id === activeTabId);
  if (!active || active.type !== "web") return;
  active.url = url;
  active.title = prettyHost(url);
  if (isElectron) {
    const mainId = (active as any).mainId;
    if (mainId && ea?.browserNavigate) ea.browserNavigate(mainId, url);
  } else if (active.el) {
    (active.el as HTMLIFrameElement).src = "/api/proxy?url=" + encodeURIComponent(url);
  }
  renderTabstrip();
}

function navActive(kind: "back" | "forward" | "reload"): void {
  const active = tabs.find(t => t.id === activeTabId);
  if (!active || active.type !== "web") return;
  if (isElectron) {
    const mainId = (active as any).mainId;
    if (mainId && ea?.browserNav) ea.browserNav(mainId, kind);
  } else if (active.el) {
    try {
      switch (kind) {
        case "back": (active.el as HTMLIFrameElement).contentWindow?.history.back(); break;
        case "forward": (active.el as HTMLIFrameElement).contentWindow?.history.forward(); break;
        case "reload": if ((active.el as HTMLIFrameElement).contentWindow) (active.el as HTMLIFrameElement).src = (active.el as HTMLIFrameElement).src; break;
      }
    } catch {}
  }
}

function renderTabstrip(): void {
  if (!strip) return;
  strip.textContent = "";
  for (const t of tabs) {
    const el = document.createElement("div");
    el.className = "b-tab" + (t.id === activeTabId ? " active" : "");
    el.dataset.tab = t.id;
    if (t.type === "ziva") {
      el.classList.add("b-tab-pinned");
    } else {
      const fav = document.createElement("span"); fav.className = "b-tab-favicon"; fav.textContent = "●"; el.appendChild(fav);
    }
    const title = document.createElement("span"); title.className = "b-tab-title"; title.textContent = t.title; el.appendChild(title);
    if (t.type !== "ziva") {
      const close = document.createElement("button");
      close.className = "b-tab-close"; close.title = "Close tab"; close.textContent = "×";
      close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
      el.appendChild(close);
    }
    el.onclick = (e) => { if ((e.target as HTMLElement).classList.contains("b-tab-close")) return; activateTab(t.id); };
    strip.appendChild(el);
  }
  const add = document.createElement("button");
  add.className = "b-tab-new"; add.title = "New tab"; add.textContent = "+";
  add.onclick = () => createWebTab();
  strip.appendChild(add);
}

function renderOmnibox(): void {
  if (!omnibox) return;
  const active = tabs.find(t => t.id === activeTabId);
  const isWeb = active?.type === "web";
  
  if (!isWeb) {
    omnibox.style.display = "none";
    return;
  }
  omnibox.style.display = "flex";
  omnibox.textContent = "";

  const mkBtn = (label: string, kind: string, title: string, disabled = false): HTMLButtonElement => {
    const b = document.createElement("button");
    b.className = "bt-btn"; b.dataset.nav = kind; b.title = title; b.textContent = label;
    if (disabled) { b.disabled = true; b.style.opacity = "0.3"; }
    return b;
  };
  const back = mkBtn("◂", "back", "Back", !isWeb);
  const fwd = mkBtn("▸", "forward", "Forward", !isWeb);
  const reload = mkBtn("⟳", "reload", "Reload", !isWeb);
  const input = document.createElement("input");
  input.type = "text"; input.className = "bt-url";
  input.value = active!.url || "";
  input.placeholder = "Search or enter URL…";
  
  const goBtn = document.createElement("button"); goBtn.className = "bt-go"; goBtn.textContent = "Go";
  omnibox.append(back, fwd, reload, input, goBtn);

  const go = () => {
    let v = (input.value || "").trim();
    if (!v) return;
    if (!/^https?:\/\//i.test(v) && !/^[\w-]+(\.[\w-]+)+/.test(v)) v = "https://www.google.com/search?q=" + encodeURIComponent(v);
    else if (!/^https?:\/\//i.test(v)) v = "https://" + v;
    navigateActive(v);
  };
  goBtn.onclick = go;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  back.onclick = () => navActive("back");
  fwd.onclick = () => navActive("forward");
  reload.onclick = () => navActive("reload");
}

function prettyHost(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, "") || url; }
  catch { return url; }
}

export function reportBrowserArea() {
  if (!webArea || !ea?.browserSetArea) return;
  const r = webArea.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) ea.browserSetArea({ x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) });
}
