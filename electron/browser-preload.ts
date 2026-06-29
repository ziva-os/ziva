// electron/browser-preload.ts
//
// Runs inside the Agent Browser <webview>'s guest pages. Forwards
// link clicks and window.open calls back to the host renderer via
// ipcRenderer.sendToHost so they funnel through the same
// `openLinkInBrowser(url)` entry point that the main process uses for
// target=_blank / window.open (see electron/main.ts setWindowOpenHandler).
//
// Why funnel everything through the host instead of letting the
// webview navigate on its own?
//   - Single place to update the URL bar / agent context after each
//     navigation. The webview's did-navigate also fires after loadURL,
//     so the URL bar still gets every page.
//   - Lets the host log or filter links before the webview commits to
//     a navigation (e.g. CSP, allowed-domains for the agent browser).
//   - Keeps target=_blank, window.open, and normal clicks on a single
//     code path in the renderer.
//
// Note: this preload only runs inside the <webview>, not in the main
// Ziva window. The renderer-side preload (electron/preload.ts) handles
// the main window.

import { ipcRenderer } from "electron";

// Catch every link click in the capture phase so we see them before
// site scripts can preventDefault. Only intercept clicks that would
// otherwise navigate; ignore clicks on non-anchor elements.
document.addEventListener(
  "click",
  (e) => {
    const target = e.target as Element | null;
    const anchor = target?.closest?.("a") as HTMLAnchorElement | null;
    if (!anchor) return;

    // Let clicks on anchors without an href fall through (e.g. <a> used
    // as a JS-only button — but those usually have href="#").
    const rawHref = anchor.getAttribute("href");
    if (!rawHref) return;

    // Skip in-page anchors, mailto, tel, javascript — let the page handle.
    if (
      rawHref.startsWith("#") ||
      rawHref.startsWith("mailto:") ||
      rawHref.startsWith("tel:") ||
      rawHref.startsWith("javascript:")
    ) {
      return;
    }

    // Resolve relative URLs against the page's base URL so the host
    // gets an absolute URL it can hand to frame.loadURL().
    let href: string;
    try {
      href = new URL(rawHref, document.baseURI).toString();
    } catch {
      return;
    }

    // Prevent the webview from navigating on its own — the host will
    // call frame.loadURL(href) and the webview navigates from there.
    e.preventDefault();
    e.stopPropagation();
    ipcRenderer.sendToHost("ziva:open-link-in-panel", href);
  },
  true,
);

// Override window.open so any window.open(url) call (including the
// one synthesised by target="_blank" anchors) goes through the host
// instead of opening a new Electron window. The main process's
// setWindowOpenHandler does the same thing at the OS level, but
// site scripts that use window.open directly during page load (before
// any user click) bypass that path on some Electron versions — this
// preload-side override is the reliable catch-all.
const originalOpen = window.open;
window.open = function (
  url?: string | URL,
  _target?: string,
  _features?: string,
): Window | null {
  if (url != null) {
    const href = typeof url === "string" ? url : url.toString();
    if (href) ipcRenderer.sendToHost("ziva:open-link-in-panel", href);
  }
  // Drop the original return value — callers expect a window handle,
  // but we're sending them to the host which doesn't expose one.
  return null as unknown as Window;
};
// Preserve the original signature for any code that checks
// window.open.toString() etc.
try {
  Object.defineProperty(window.open, "name", { value: "open" });
} catch {
  /* ignore — non-essential */
}
