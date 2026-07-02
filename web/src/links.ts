/**
 * Link handling — what happens when the user (or app) opens a URL.
 *
 * Links open as a new tab in the pinned-tab browser shell (see
 * browser-shell.ts): the Ziva tab is always pinned; web pages live in their
 * own <webview> tabs that the agent can drive via the CDP bridge.
 */

import { openInBrowserTab } from "./browser-shell";

/** Open a URL as a web tab in the browser shell. */
export function openLinkInBrowser(url: string): void {
  openInBrowserTab(url);
}

/**
 * Intercept clicks on links inside messages and open them in the external
 * browser instead of navigating the host window. Anchor-only / mailto / tel /
 * javascript links and skill-file links are left alone.
 */
export function initMessageLinkInterceptor(): void {
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const anchor = target.closest("a") as HTMLAnchorElement | null;
    if (!anchor) return;
    if (!anchor.closest(".msg-inner, .compact-dropped, .tool-card-body, .panel-content")) return;
    const href = anchor.getAttribute("href");
    if (!href) return;
    if (href.startsWith("#") || href.startsWith("mailto:") || href.startsWith("tel:") || href.startsWith("javascript:")) return;
    if (anchor.classList.contains("skill-file-link")) return;
    if (href.startsWith("http://") || href.startsWith("https://")) {
      e.preventDefault();
      e.stopPropagation();
      openLinkInBrowser(href);
      return;
    }
    if (!href.startsWith("/")) {
      e.preventDefault();
      e.stopPropagation();
      console.log("Relative link clicked:", href);
    }
  });
}
