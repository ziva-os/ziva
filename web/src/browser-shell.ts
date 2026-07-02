/**
 * Pinned-tab browser shell (Phase 4).
 *
 * Wraps the existing chat UI (`.ziva-layout`) in a Chromium-like shell:
 *
 *   ┌ tabstrip ──────────────────────────────────────────────┐
 *   │ 📌 Ziva   ● example.com   ● docs.org   [+]             │
 *   ├ toolbar (web tabs only) ───────────────────────────────┤
 *   │ ◂ ▸ ⟳  [url]                                           │
 *   ├ content ───────────────────────────────────────────────┤
 *   │   Ziva tab → the chat UI (.ziva-layout)                │
 *   │   Web tab  → an embedded <webview> filling the area    │
 *   └────────────────────────────────────────────────────────┘
 *
 * The Ziva tab is always index 0, pinned, and cannot be closed. Each web tab
 * is an Electron <webview> on the `persist:ziva-browser` partition; on
 * `did-attach` it registers with the CDP bridge (port 9223) so chrome-devtools-
 * mcp can drive it. The Ziva tab is plain renderer DOM, so it is never exposed
 * as a CDP target (the agent can't inspect the chat it lives in).
 *
 * Tabstrip/toolbar are built with the DOM API (no innerHTML) so page-derived
 * titles/URLs can't introduce markup.
 */

export interface BrowserTab {
  id: string;
  type: "ziva" | "web";
  url?: string;
  title: string;
  el?: HTMLElement; // the <webview>/<iframe> for web tabs
}

const ZIVA_TAB_ID = "ziva";
let tabs: BrowserTab[] = [{ id: ZIVA_TAB_ID, type: "ziva", title: "Ziva" }];
let activeTabId = ZIVA_TAB_ID;
let _seq = 0;

// DOM refs — populated in initBrowserShell().
let strip: HTMLElement | null = null;
let toolbar: HTMLElement | null = null;
let zivaLayout: HTMLElement | null = null;
let webViews: HTMLElement | null = null;

const isElectron = !!(window as any).electronAPI;

/**
 * Initialize the shell. The existing chat UI (`.ziva-layout`, already rendered
 * into #app by main.ts) is moved INSIDE the shell's content area; the
 * tabstrip, toolbar, and web-views container are built around it via the DOM
 * API (no template rewrite). The Ziva tab shows that layout; web tabs show
 * embedded <webview>s.
 */
export function initBrowserShell(): void {
  const app = document.getElementById("app");
  if (!app) return;
  const layout = app.querySelector(".ziva-layout") as HTMLElement | null;
  if (layout) layout.id = "zivaLayout";

  // Build the shell and relocate the existing chat layout into it.
  const shell = document.createElement("div");
  shell.className = "browser-shell";
  strip = document.createElement("div");
  strip.id = "browserTabstrip";
  strip.className = "browser-tabstrip";
  toolbar = document.createElement("div");
  toolbar.id = "browserToolbar";
  toolbar.className = "browser-toolbar";
  toolbar.style.display = "none";
  const content = document.createElement("div");
  content.className = "browser-content";
  webViews = document.createElement("div");
  webViews.id = "webViews";
  webViews.className = "web-views";

  app.replaceChildren(shell);
  shell.append(strip, toolbar, content);
  if (layout) content.append(layout);
  content.append(webViews);
  zivaLayout = layout;

  renderTabstrip();
  renderToolbar();
  applyActive();

  // target=_blank / window.open inside any webview → main process forwards
  // the URL here; open it as a new web tab (browser semantics).
  const ea = (window as any).electronAPI;
  if (ea?.setOpenLinkInPanelHandler) {
    ea.setOpenLinkInPanelHandler((url: string) => openInBrowserTab(url));
  }
}

/** Open a URL in a web tab: reuse a tab already on that URL, else make a new one. */
export function openInBrowserTab(url: string): void {
  const existing = tabs.find(t => t.type === "web" && t.url === url);
  if (existing) { activateTab(existing.id); return; }
  // If the active web tab is still on about:blank, navigate it instead of piling up tabs.
  const active = tabs.find(t => t.id === activeTabId);
  if (active && active.type === "web" && (!active.url || active.url === "about:blank")) {
    navigateWebTab(active, url);
    activateTab(active.id);
    return;
  }
  createWebTab(url);
}

function nextId(): string { _seq += 1; return "web_" + _seq; }

function createWebTab(url?: string): void {
  const tab: BrowserTab = { id: nextId(), type: "web", url, title: url ? prettyHost(url) : "New Tab" };
  tab.el = createFrame(url);
  if (webViews) webViews.appendChild(tab.el);
  tabs.push(tab);
  activateTab(tab.id);
}

function navigateWebTab(tab: BrowserTab, url: string): void {
  tab.url = url;
  tab.title = prettyHost(url);
  const frame: any = tab.el;
  if (!frame) return;
  if (isElectron) { try { frame.loadURL(url); } catch {} }
  else { frame.src = "/api/proxy?url=" + encodeURIComponent(url); }
}

function createFrame(url?: string): HTMLElement {
  let frame: any;
  if (isElectron) {
    frame = document.createElement("webview");
    frame.className = "web-frame";
    frame.setAttribute("allowpopups", "");
    const partition = (window as any).electronAPI?.browserPartition || "persist:ziva-browser";
    frame.setAttribute("partition", partition);
    // Preload must be set BEFORE attach (Electron captures it at attach time).
    const preload = (window as any).electronAPI?.browserPreloadPath;
    if (typeof preload === "string") frame.setAttribute("preload", preload);
    frame.setAttribute("src", "about:blank");
    registerCdp(frame);
    // <a> clicks inside the webview are forwarded by browser-preload.ts via
    // sendToHost → ipc-message; open them as new web tabs.
    frame.addEventListener("ipc-message", (e: any) => {
      if (e.channel === "ziva:open-link-in-panel" && e.args?.[0]) openInBrowserTab(e.args[0]);
    });
    if (url) {
      frame.addEventListener("did-attach", () => { try { frame.loadURL(url); } catch {} }, { once: true });
    }
    // Keep the URL bar + tab title in sync as the user navigates.
    frame.addEventListener("did-navigate", (e: any) => onNav(frame, e.url));
    frame.addEventListener("did-navigate-in-page", (e: any) => onNav(frame, e.url));
    frame.addEventListener("page-title-updated", (e: any) => {
      const t = tabs.find(x => x.el === frame);
      if (t && e.title) { t.title = e.title; renderTabstrip(); }
    });
  } else {
    frame = document.createElement("iframe");
    frame.className = "web-frame";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups");
    frame.src = url ? "/api/proxy?url=" + encodeURIComponent(url) : "about:blank";
  }
  return frame;
}

function onNav(frame: any, url: string): void {
  const t = tabs.find(x => x.el === frame);
  if (!t) return;
  t.url = url;
  t.title = prettyHost(url);
  renderTabstrip();
  if (t.id === activeTabId) renderToolbar();
}

/** Register a webview with the CDP bridge so chrome-devtools-mcp can drive it. */
function registerCdp(frame: any): void {
  frame.addEventListener("did-attach", async () => {
    try {
      const wcId = frame.getWebContentsId?.();
      if (typeof wcId !== "number") return;
      const targetId: string | null = await (window as any).electronAPI.registerCdpPage(wcId);
      if (!targetId) return;
      frame._cdpTargetId = targetId;
      frame.addEventListener("destroyed", () => {
        (window as any).electronAPI.unregisterCdpPage(targetId).catch(() => {});
      }, { once: true });
    } catch (err) {
      console.error("[browser-shell] CDP register failed:", err);
    }
  }, { once: true });
}

function activateTab(id: string): void {
  activeTabId = id;
  applyActive();
  renderTabstrip();
  renderToolbar();
}

function closeTab(id: string): void {
  if (id === ZIVA_TAB_ID) return; // pinned
  const idx = tabs.findIndex(t => t.id === id);
  if (idx < 0) return;
  const tab = tabs[idx];
  tab.el?.remove();
  tabs.splice(idx, 1);
  if (activeTabId === id) {
    const fallback = tabs[Math.min(idx, tabs.length - 1)] || tabs[0];
    activateTab(fallback.id);
  } else {
    renderTabstrip();
  }
}

/** Show/hide the ziva view vs webviews + toolbar based on the active tab. */
function applyActive(): void {
  const active = tabs.find(t => t.id === activeTabId);
  const isWeb = active?.type === "web";
  if (zivaLayout) zivaLayout.style.display = isWeb ? "none" : "";
  if (toolbar) toolbar.style.display = isWeb ? "" : "none";
  if (webViews) webViews.style.display = isWeb ? "" : "none";
  tabs.forEach(t => {
    if (t.el) (t.el as HTMLElement).style.display = (t.id === activeTabId) ? "" : "none";
  });
}

function renderTabstrip(): void {
  if (!strip) return;
  strip.textContent = ""; // clear safely
  for (const t of tabs) {
    const el = document.createElement("div");
    el.className = "b-tab" + (t.id === activeTabId ? " active" : "") + (t.type === "ziva" ? " b-tab-pinned" : "");
    el.dataset.tab = t.id;
    if (t.type === "ziva") {
      el.title = "Ziva (pinned)";
      const pin = document.createElement("span");
      pin.className = "b-tab-pin";
      pin.textContent = "📌";
      el.appendChild(pin);
    } else {
      const fav = document.createElement("span");
      fav.className = "b-tab-favicon";
      fav.textContent = "●";
      el.appendChild(fav);
    }
    const title = document.createElement("span");
    title.className = "b-tab-title";
    title.textContent = t.title;
    el.appendChild(title);
    if (t.type !== "ziva") {
      const close = document.createElement("button");
      close.className = "b-tab-close";
      close.dataset.close = t.id;
      close.title = "Close tab";
      close.textContent = "×";
      close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
      el.appendChild(close);
    }
    el.onclick = (e) => {
      if ((e.target as HTMLElement).classList.contains("b-tab-close")) return;
      activateTab(t.id);
    };
    strip.appendChild(el);
  }
  const add = document.createElement("button");
  add.className = "b-tab-new";
  add.id = "btnNewWebTab";
  add.title = "New tab";
  add.textContent = "+";
  add.onclick = () => createWebTab();
  strip.appendChild(add);
}

function renderToolbar(): void {
  if (!toolbar) return;
  const active = tabs.find(t => t.id === activeTabId);
  if (!active || active.type !== "web") { toolbar.textContent = ""; return; }
  toolbar.textContent = "";
  const url = active.url && active.url !== "about:blank" ? active.url : "";
  const mkBtn = (label: string, kind: string, title: string): HTMLButtonElement => {
    const b = document.createElement("button");
    b.className = "bt-btn";
    b.dataset.nav = kind;
    b.title = title;
    b.textContent = label;
    return b;
  };
  const back = mkBtn("◂", "back", "Back");
  const fwd = mkBtn("▸", "forward", "Forward");
  const reload = mkBtn("⟳", "reload", "Reload");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "bt-url";
  input.value = url;
  input.placeholder = "Search or enter URL…";
  const goBtn = document.createElement("button");
  goBtn.className = "bt-go";
  goBtn.textContent = "Go";
  toolbar.append(back, fwd, reload, input, goBtn);

  const frame: any = active.el;
  const go = () => {
    let v = (input.value || "").trim();
    if (!v) return;
    if (!/^https?:\/\//i.test(v) && !/^[\w-]+(\.[\w-]+)+/.test(v)) {
      v = "https://www.google.com/search?q=" + encodeURIComponent(v);
    } else if (!/^https?:\/\//i.test(v)) {
      v = "https://" + v;
    }
    navigateWebTab(active, v);
  };
  goBtn.onclick = go;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  back.onclick = () => nav(frame, "back");
  fwd.onclick = () => nav(frame, "forward");
  reload.onclick = () => nav(frame, "reload");
}

function nav(frame: any, kind: "back" | "forward" | "reload"): void {
  if (!frame) return;
  try {
    if (isElectron) {
      if (kind === "back") frame.goBack();
      else if (kind === "forward") frame.goForward();
      else frame.reload();
    } else {
      if (kind === "back") frame.contentWindow?.history.back();
      else if (kind === "forward") frame.contentWindow?.history.forward();
      else { try { frame.contentWindow?.location.reload(); } catch { frame.src = frame.src; } }
    }
  } catch {}
}

function prettyHost(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, "") || url; }
  catch { return url; }
}
