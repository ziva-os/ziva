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

import * as i18n from "./i18n";

const ZIVA_TAB_ID = "ziva";
// localStorage key for the active web tab's main-process id. Survives renderer
// reloads (Cmd+R) and window close→reopen on macOS, so recovery can restore the
// exact tab the user had focused — instead of relying on the main process's
// ``activeBrowserTab`` which may be stale after a close.
const ACTIVE_MAINID_KEY = "ziva:browserActiveMainId";
let tabs: BrowserTab[] = [{ id: ZIVA_TAB_ID, type: "ziva", title: "Ziva" }];
let activeTabId = ZIVA_TAB_ID;
let _seq = 0;
let pendingTabCreated: Array<{ id: string; url?: string; targetId?: string }> = [];

let strip: HTMLElement | null = null;
let omnibox: HTMLElement | null = null;
let zivaLayout: HTMLElement | null = null;
let webArea: HTMLElement | null = null;

const isElectron = !!(window as any).electronAPI;
const ea: any = (window as any).electronAPI;

function nextId(): string { _seq += 1; return "web_" + _seq; }

/** Add a tab created by the main process (CDP Target.createTarget) to the shell UI. */
function addMainProcessTab(e: { id: string; url?: string; targetId?: string; title?: string }) {
  if (!strip) {
    // Shell not initialized yet; buffer until initBrowserShell finishes.
    pendingTabCreated.push(e);
    return;
  }
  const tab: BrowserTab = {
    id: nextId(),
    type: "web",
    url: e.url,
    title: e.title || (e.url ? prettyHost(e.url) : i18n.t("browser.newTab")),
  };
  (tab as any).mainId = e.id;
  tabs.push(tab);
  if (activeTabId === ZIVA_TAB_ID) {
    activateTab(tab.id);
  } else {
    renderTabstrip();
    reportBrowserArea();
  }
}

/** Build the shell: tab strip + omnibox + body (ziva-layout | web-area). */
export function initBrowserShell(): void {
  const app = document.getElementById("app");
  if (!app) return;
  document.body.classList.add("browser-shell-active");
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

  // Reload recovery: the main process keeps every WebContentsView alive across
  // renderer reloads (so chrome-devtools-mcp still sees them over CDP), but the
  // renderer's `tabs` array is reset and the tab strip would otherwise be
  // empty. Pull the live tab list from the main process and re-register them.
  //
  // Done after applyActive() so the shell DOM is ready, and after the event
  // listeners are wired so subsequent nav/title events still bind to the
  // rebuilt tabs.
  //
  // We bypass ``addMainProcessTab`` here on purpose. That function auto-
  // focuses a newly-added tab when the user is on the Ziva tab — fine for a
  // live createTarget event, but during recovery it would land focus on the
  // first web tab in the list even if the user was on the Ziva tab when they
  // reloaded. Inlining here lets us honour the last active tab (persisted in
  // localStorage, which survives reloads and window close/reopen) and leave
  // the Ziva tab alone when that's what the user had focused.
  ea?.browserListTabs?.().then((existing: Array<{ id: string; url?: string; title?: string; active?: boolean }>) => {
    if (!existing?.length) return;
    const lastActiveMainId = localStorage.getItem(ACTIVE_MAINID_KEY);
    let activated = false;
    for (const t of existing) {
      const tab: BrowserTab = {
        id: nextId(),
        type: "web",
        url: t.url,
        title: t.title || (t.url ? prettyHost(t.url) : i18n.t("browser.newTab")),
      };
      (tab as any).mainId = t.id;
      tabs.push(tab);
      if (t.id === lastActiveMainId && !activated) {
        activateTab(tab.id);
        activated = true;
      }
    }
    renderTabstrip();
    reportBrowserArea();
  });

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(reportBrowserArea);
    ro.observe(webArea);
  }
  window.addEventListener("resize", reportBrowserArea);
  setTimeout(reportBrowserArea, 100);

  // Main → renderer events: target=_blank in a page → new tab; nav/title sync;
  // main-process-created CDP tabs (from Target.createTarget) → add shell entry.
  ea?.onBrowserNewTab?.((payload: string | { url: string; force?: boolean }) => {
    const url = typeof payload === "string" ? payload : payload.url;
    if (typeof payload === "object" && payload.force) {
      createWebTab(url);
    } else {
      openInBrowserTab(url);
    }
  });
  ea?.onBrowserTabCreated?.((e: { id: string; url?: string; targetId?: string }) => {
    addMainProcessTab(e);
  });
  // Main closed a tab itself (CDP close_page) — remove the matching shell tab.
  // closeTab is idempotent here: its browserCloseTab IPC no-ops on a view the
  // main process already destroyed (destroyBrowserTab returns early).
  ea?.onBrowserTabClosed?.((e: { id: string }) => {
    const t = tabs.find(x => (x as any).mainId === e.id);
    if (t) closeTab(t.id);
  });
  ea?.onBrowserNav?.((e: { id: string; url: string }) => {
    const t = tabs.find(x => (x as any).mainId === e.id);
    if (t) { t.url = e.url; t.title = prettyHost(e.url); if (t.id === activeTabId) renderOmnibox(); renderTabstrip(); }
  });
  ea?.onBrowserTitle?.((e: { id: string; title: string }) => {
    const t = tabs.find(x => (x as any).mainId === e.id);
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

  // Flush any main-process tabs that arrived before the shell was ready.
  while (pendingTabCreated.length) {
    addMainProcessTab(pendingTabCreated.shift()!);
  }
}

/** Open a URL in a web tab: reuse a tab on that URL, else make a new one. */
export function openInBrowserTab(url: string): void {
  const existing = tabs.find(t => t.type === "web" && t.url === url);
  if (existing) { activateTab(existing.id); return; }
  createWebTab(url);
}

function createWebTab(url?: string): void {
  const tab: BrowserTab = { id: nextId(), type: "web", url, title: url ? prettyHost(url) : i18n.t("browser.newTab") };
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
  const t = tabs.find(x => x.id === id);
  // Persist the active tab so reload/close-reopen recovery can restore it.
  // Store the main-process id for web tabs; clear it for the Ziva tab.
  const mainId = t && (t as any).mainId;
  if (t?.type === "web" && mainId) localStorage.setItem(ACTIVE_MAINID_KEY, mainId);
  else localStorage.removeItem(ACTIVE_MAINID_KEY);
  if (isElectron) {
    if (t?.type === "web" && mainId && ea?.browserShowTab) ea.browserShowTab(mainId);
    else if (t?.type === "ziva" && ea?.browserHideTabs) ea.browserHideTabs();
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
  if (webArea) webArea.style.display = isWeb ? "block" : "none";
  // Full-page overlays (Skills, Settings, etc.) are appended directly to
  // <body>, not inside zivaLayout. Hide them while a browser tab is active
  // so they don't cover the native WebContentsView.
  document.querySelectorAll(".fullpage-overlay").forEach((el) => {
    (el as HTMLElement).style.display = isWeb ? "none" : "";
  });
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
      close.className = "b-tab-close"; close.title = i18n.t("browser.closeTab"); close.textContent = "×";
      close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
      el.appendChild(close);
    }
    el.onclick = (e) => { if ((e.target as HTMLElement).classList.contains("b-tab-close")) return; activateTab(t.id); };
    strip.appendChild(el);
  }
  const add = document.createElement("button");
  add.className = "b-tab-new"; add.title = i18n.t("browser.newTabTitle"); add.textContent = "+";
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
  const back = mkBtn("◂", "back", i18n.t("browser.back"), !isWeb);
  const fwd = mkBtn("▸", "forward", i18n.t("browser.forward"), !isWeb);
  const reload = mkBtn("⟳", "reload", i18n.t("browser.reload"), !isWeb);
  const input = document.createElement("input");
  input.type = "text"; input.className = "bt-url";
  input.value = active!.url || "";
  input.placeholder = i18n.t("browser.urlPlaceholder");

  const goBtn = document.createElement("button"); goBtn.className = "bt-go"; goBtn.textContent = i18n.t("browser.go");
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
