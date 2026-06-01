import { marked } from "marked";
import Prism from "prismjs";
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

const renderer = new marked.Renderer();
marked.setOptions({
  renderer,
  breaks: true,
  gfm: true,
});

// Custom code block rendering
renderer.code = function (code: string, lang?: string): string {
  const language = lang || "";
  return `<pre><div class="code-header"><span class="lang-label">${language || "text"}</span><button class="copy-btn" title="Copy">Copy</button></div><code class="language-${language}">${escapeHtml(code)}</code></pre>`;
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
    btnEl.onclick = () => {
      const pre = btnEl.closest("pre");
      const code = pre?.querySelector("code");
      const text = code?.textContent || "";
      navigator.clipboard.writeText(text);
      btnEl.textContent = "Copied!";
      setTimeout(() => { btnEl.textContent = "Copy"; }, 1500);
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
