import { marked, type TokenizerExtension, type RendererExtension, type Tokens } from "marked";
import Prism from "prismjs";
import { copyText } from "./electron-bridge";
import katex from "katex";
import "katex/dist/katex.min.css";
import "prismjs/components/prism-python";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-json";
import "prismjs/components/prism-yaml";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-javascript";
import "prismjs/components/prism-css";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-diff";
import "prismjs/components/prism-markdown";

// KaTeX math extensions for marked
interface MathToken {
  type: string;
  raw: string;
  text: string;
}

const inlineMath: TokenizerExtension & RendererExtension = {
  name: "inlineMath",
  level: "inline",
  start(src: string) {
    return src.match(/\$/)?.index;
  },
  tokenizer(src: string) {
    const match = src.match(/^\$([^\$]+?)\$/);
    if (match) {
      return { type: "inlineMath", raw: match[0], text: match[1].trim() };
    }
  },
  renderer(token: Tokens.Generic) {
    return renderKatex(token.text as string, false);
  },
};

const blockMath: TokenizerExtension & RendererExtension = {
  name: "blockMath",
  level: "block",
  start(src: string) {
    return src.match(/\$\$/)?.index;
  },
  tokenizer(src: string) {
    const match = src.match(/^\$\$([\s\S]+?)\$\$/);
    if (match) {
      return { type: "blockMath", raw: match[0], text: match[1].trim() };
    }
  },
  renderer(token: Tokens.Generic) {
    return `<div class="katex-display">${renderKatex(token.text as string, true)}</div>`;
  },
};

function renderKatex(expr: string, displayMode: boolean): string {
  try {
    return katex.renderToString(expr, { displayMode, throwOnError: true });
  } catch {
    return `<span class="katex-error" title="LaTeX syntax error">${escapeHtml(expr)}</span>`;
  }
}

const renderer = new marked.Renderer();
marked.setOptions({
  renderer,
  breaks: true,
  gfm: true,
});
marked.use({ extensions: [blockMath, inlineMath] });

// Marked's autolinker includes trailing punctuation such as ')' as part of the
// URL, so text like "（https://weibo.com/hot/mine）" renders the link as
// "https://weibo.com/hot/mine)". For autolinks (href === text), strip trailing
// punctuation from the URL and place it back after the link.
const TRAILING_PUNCT_RE = /[.,;:!?\)\]\}>]+$/;
renderer.link = function (href: string, title: string | null | undefined, text: string): string {
  let cleanHref = href;
  let cleanText = text;
  let suffix = "";
  if (href === text) {
    const m = text.match(TRAILING_PUNCT_RE);
    if (m) {
      suffix = m[0];
      cleanHref = href.slice(0, -suffix.length);
      cleanText = text.slice(0, -suffix.length);
    }
  }
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `<a href="${escapeHtml(cleanHref)}"${titleAttr}>${escapeHtml(cleanText)}</a>${suffix ? escapeHtml(suffix) : ""}`;
};

// Custom code block rendering
renderer.code = function (code: string, lang?: string): string {
  const language = lang || "";
  return `<pre><div class="code-header"><span class="lang-label">${language || "text"}</span><button class="copy-btn" title="Copy">Copy</button></div><code class="language-${language}">${escapeHtml(code)}</code></pre>`;
};

// Markdown images in assistant messages often reference local filesystem
// paths (e.g. /tmp/vvg_star/starry_night.jpg from image-generation tools).
// The browser can't load these directly, so proxy them through the
// server's /attachments endpoint — same rule as attachmentUrl() in main.ts.
//
// IM-bridge sessions embed images as `attachment://filename` — resolved
// via `_attachmentDataMap` (set by renderMessages before calling
// renderMarkdown) which maps filename → data:URL from hidden messages.
let _attachmentDataMap: Map<string, string> | null = null;

export function setAttachmentDataMap(map: Map<string, string> | null): void {
  _attachmentDataMap = map;
}

renderer.image = function (href: string, title: string | null | undefined, text: string): string {
  let src = href;
  if (href && href.startsWith("attachment://")) {
    const filename = href.slice("attachment://".length);
    const dataUrl = _attachmentDataMap?.get(filename);
    if (dataUrl) {
      src = dataUrl;
    }
    // If no dataUrl found, fall through to the broken-image path so the
    // user at least sees the alt text rather than a silent empty box.
  } else if (href && href.startsWith("/") && !href.startsWith("/attachments")) {
    src = `/attachments?path=${encodeURIComponent(href)}`;
  }
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `<img src="${escapeHtml(src)}" alt="${escapeHtml(text)}"${titleAttr}>`;
};

export function renderMarkdown(text: string): string {
  if (!text) return "";
  try {
    return marked.parse(text) as string;
  } catch {
    return escapeHtml(text).replace(/\n/g, "<br/>");
  }
}

export function highlightCode(container: HTMLElement): void {
  setTimeout(() => {
    container.querySelectorAll("pre code").forEach((block) => {
      const el = block as HTMLElement;
      const lang = el.className.match(/language-(\w+)/)?.[1];
      if (lang && Prism.languages[lang]) {
        try {
          Prism.highlightElement(el);
        } catch { /* ignore */ }
      }
    });
  }, 0);
}

export function addCopyButtons(container: HTMLElement): void {
  container.querySelectorAll(".code-header .copy-btn").forEach((btn) => {
    const btnEl = btn as HTMLElement;
    if (btnEl.dataset.bound) return;
    btnEl.dataset.bound = "true";
    btnEl.onclick = async () => {
      const pre = btnEl.closest("pre");
      const code = pre?.querySelector("code");
      const text = code?.textContent || "";
      const ok = await copyText(text);
      btnEl.textContent = ok ? "Copied!" : "Copy failed";
      setTimeout(() => { btnEl.textContent = "Copy"; }, 1500);
      if (!ok) {
        // 不要静默吞错：让用户/开发能看到
        // eslint-disable-next-line no-console
        console.warn("[ziva] copy failed for code block");
      }
    };
  });
}

// Extract thinking content between <think tags
export function extractThinking(content: string): { thinking: string; main: string } {
  const thinkRegex = /<think[^>]*>([\s\S]*?)<\/think[^>]*>/gi;
  const thinkMatches = [...content.matchAll(thinkRegex)];
  const thinking = thinkMatches.map(m => m[1].trim()).join("\n\n---\n\n");
  const main = content.replace(thinkRegex, "").trim();
  return { thinking, main };
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
