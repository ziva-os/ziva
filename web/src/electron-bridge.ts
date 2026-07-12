/**
 * Thin wrapper around the Electron preload bridge (`window.electronAPI`).
 *
 * Centralises the two entry points the renderer actually needs:
 *   - isElectron() — true when running inside the packaged desktop app.
 *   - copyText()  — write text to the system clipboard.
 *
 * `copyText` has three fallback paths because clipboard API support is
 * inconsistent across runtimes:
 *
 *   1. Electron 加载前端页面用的是 http://127.0.0.1:4097，Chromium 视为
 *      non-secure context，`navigator.clipboard.writeText` 会直接被拒。
 *      这种情况下走 Electron 主进程的 IPC 通道，主进程用 native clipboard
 *      模块写剪贴板，没有 secure context 限制。
 *   2. secure context（https://、file://、app:// 等）+ 有原生 Clipboard
 *      API → 直接走浏览器 API。
 *   3. 都不行（例如纯 web dev fallback、极旧浏览器）→ 再用 document.execCommand
 *      + 临时 <textarea> 这种兼容老路径。
 *
 * 这层封装让上层（markdown 渲染、tool 卡片、消息区……）不用关心自己跑在
 * 哪里、要不要走 IPC，统统一行 `await copyText(text)`。
 */

declare global {
  interface Window {
    electronAPI?: {
      isElectron?: () => Promise<boolean>;
      copyText?: (text: string) => Promise<boolean>;
      [k: string]: any;
    };
  }
}

export function isElectron(): boolean {
  return typeof window !== "undefined" && !!window.electronAPI;
}

export async function copyText(text: string): Promise<boolean> {
  if (typeof text !== "string" || !text) return false;

  // Path 1 + 2: Electron preload bridge.
  // Electron 主进程的 native clipboard 始终可用，是最稳的路径。
  if (window.electronAPI?.copyText) {
    try {
      const ok = await window.electronAPI.copyText(text);
      if (ok) return true;
    } catch {
      // fall through to next path
    }
  }

  // Path 3: 浏览器原生 Clipboard API (需要 secure context)
  if (typeof window !== "undefined" && window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to legacy fallback
    }
  }

  // Path 4: 兜底 execCommand("copy") —— 仅在 textarea 选中文本时有效，
  // 已被 Chromium 标记 deprecated 但仍可工作（仅当焦点还在当前页面）。
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}