/**
 * Preload injected into every web tab (WebContentsView). Runs in the page's
 * isolated world (contextIsolation is on), so it can touch the DOM but the
 * page's own scripts can't reach it. Watches for a text selection and floats
 * a "发送到 Ziva" button next to it; clicking sends the selected text +
 * its viewport rect to the main process, which screenshots the rect and
 * forwards {text, url, screenshot} to the Ziva renderer.
 *
 * sandbox is on, so only ipcRenderer/contextBridge are available (Electron
 * preloads them for sandboxed preloads). That's all we need here: read the
 * selection and fire one IPC. No Node required.
 */
import { ipcRenderer } from "electron";

let btn: HTMLDivElement | null = null;

interface SelInfo {
  text: string;
  rect: { x: number; y: number; width: number; height: number };
}

function selectionInfo(): SelInfo | null {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const text = sel.toString().trim();
  if (!text) return null;
  const r = sel.getRangeAt(0).getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  return { text, rect: { x: r.left, y: r.top, width: r.width, height: r.height } };
}

function hideButton(): void {
  if (btn) btn.style.display = "none";
}

function showButtonAt(x: number, y: number): void {
  if (!btn) {
    btn = document.createElement("div");
    btn.textContent = "发送到 Ziva";
    // All-inline styles: inline style assignments bypass most site CSP
    // rules (which target <style>/<link>/inline style attributes via header
    // policies). z-index max so it floats above page chrome.
    Object.assign(btn.style, {
      position: "fixed",
      zIndex: "2147483647",
      padding: "4px 10px",
      fontSize: "12px",
      lineHeight: "1",
      color: "#fff",
      background: "rgba(60, 100, 230, 0.95)",
      borderRadius: "6px",
      cursor: "pointer",
      fontFamily: "-apple-system, system-ui, sans-serif",
      boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
      userSelect: "none",
    });
    // Prevent the mousedown on the button from collapsing the selection
    // (which would empty getSelection() before the click handler reads it).
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", () => {
      const info = selectionInfo();
      hideButton();
      if (info) ipcRenderer.send("ziva:page-selection", { text: info.text, rect: info.rect });
    });
    document.documentElement.appendChild(btn);
  }
  btn.style.display = "block";
  // Clamp so the button never spills off-viewport.
  const maxX = window.innerWidth - 100;
  const maxY = window.innerHeight - 30;
  btn.style.left = `${Math.max(8, Math.min(x, maxX))}px`;
  btn.style.top = `${Math.max(8, Math.min(y, maxY))}px`;
}

// Wait a tick after mouseup for the browser to finalize the selection range.
document.addEventListener("mouseup", () => {
  setTimeout(() => {
    const info = selectionInfo();
    if (!info) {
      hideButton();
      return;
    }
    // Float just above the selection's top-left corner.
    showButtonAt(info.rect.x, info.rect.y - 30);
  }, 10);
});

// Any click elsewhere (including in the page) collapses the selection → hide.
// The button stops its own mousedown so this never fires from the button.
document.addEventListener("mousedown", (e) => {
  if (btn && e.target !== btn) hideButton();
});

window.addEventListener("blur", hideButton);
window.addEventListener("scroll", hideButton, true);
