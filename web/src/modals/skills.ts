/**
 * Skills browser modal — extracted verbatim from main.ts.
 *
 * A full-page overlay listing installed skills (grouped/searchable by
 * category), with a viewer that renders a skill's markdown body and lets the
 * user follow relative doc links within the skill tree.
 */

import * as api from "../api";
import { esc } from "../dom";
import { renderMarkdown, addCopyButtons, highlightCode } from "../markdown";
import { closeAllFullpageOverlays } from "../modals";

let skillsCache: api.Skill[] | null = null;
let skillsBrowserState: { query: string; category: string | null } = { query: "", category: null };
// Navigation history for the skill viewer. Pushing a page adds to the
// stack; clicking back pops the top and renders the new top. The
// stack is cleared whenever the user opens a different skill from
// the list (so back from the first page of a skill returns to the
// list, not to a previously viewed skill).
let skillNavStack: { name: string; path: string }[] = [];

export async function openSkillsBrowser() {
  closeAllFullpageOverlays();
  skillNavStack = [];
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "skillsModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">📚 Skills</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-toolbar">
        <div class="skills-search-box">
          <span class="skills-search-icon">🔍</span>
          <input type="text" id="skillsSearchInput" placeholder="Search by name or description..." />
        </div>
        <div class="skills-category-tabs" id="skillsCategoryTabs"></div>
      </div>
      <div class="fullpage-body" id="skillsModalBody">
        <div class="skills-modal-loading">Loading skills...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  (backdrop.querySelector("#skillsSearchInput") as HTMLInputElement).oninput = (e) => {
    skillsBrowserState.query = (e.target as HTMLInputElement).value;
    renderSkillsBrowserBody();
  };

  try {
    if (!skillsCache) skillsCache = await api.listSkills();
    renderSkillsBrowser();
  } catch (e) {
    const body = backdrop.querySelector("#skillsModalBody") as HTMLElement;
    body.innerHTML = `<div class="skills-modal-error">Failed to load: ${esc((e as Error).message)}</div>`;
  }
}

function renderSkillsBrowser() {
  renderSkillsCategoryTabs();
  renderSkillsBrowserBody();
}

function renderSkillsCategoryTabs() {
  const tabs = document.getElementById("skillsCategoryTabs");
  if (!tabs || !skillsCache) return;
  const counts = new Map<string, number>();
  for (const s of skillsCache) {
    const c = s.category || "其他";
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  // Stable sort by name, then by count desc
  const sorted = Array.from(counts.entries()).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  const total = skillsCache.length;
  const active = skillsBrowserState.category;
  tabs.innerHTML = `
    <button class="skills-category-tab ${active === null ? "active" : ""}" data-cat="">
      全部 <span class="skills-cat-count">${total}</span>
    </button>` +
    sorted.map(([cat, n]) => `
      <button class="skills-category-tab ${active === cat ? "active" : ""}" data-cat="${esc(cat)}">
        ${esc(cat)} <span class="skills-cat-count">${n}</span>
      </button>`).join("");
  tabs.querySelectorAll<HTMLElement>(".skills-category-tab").forEach((btn) => {
    btn.onclick = () => {
      const cat = btn.dataset.cat || null;
      skillsBrowserState.category = cat;
      renderSkillsBrowser();
    };
  });
}

function renderSkillsBrowserBody() {
  const body = document.getElementById("skillsModalBody");
  if (!body || !skillsCache) return;
  const q = skillsBrowserState.query.trim().toLowerCase();
  const cat = skillsBrowserState.category;
  const filtered = skillsCache.filter((s) => {
    if (cat && (s.category || "其他") !== cat) return false;
    if (!q) return true;
    return s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q);
  });

  if (filtered.length === 0) {
    body.innerHTML = '<div class="skills-empty">No skills match your search.</div>';
    return;
  }

  // Group by category for the visual layout
  const groups = new Map<string, api.Skill[]>();
  for (const s of filtered) {
    const c = s.category || "其他";
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c)!.push(s);
  }
  // Use the same category order shown in the tabs
  const orderedCats = cat ? [cat] : Array.from(groups.keys()).sort((a, b) => a.localeCompare(b));

  let html = "";
  for (const c of orderedCats) {
    const items = groups.get(c) || [];
    html += `<div class="skills-group">`;
    html += `<div class="skills-group-header">${esc(c)} <span class="skills-group-count">${items.length}</span></div>`;
    html += `<div class="skills-grid">`;
    for (const s of items) {
      html += `
        <div class="skill-card" data-skill-path="${esc(s.path)}" data-skill-name="${esc(s.name)}">
          <div class="skill-card-name">${esc(s.name)}</div>
          <div class="skill-card-desc">${esc(s.description || "(no description)")}</div>
          <div class="skill-card-footer">
            <span class="skill-card-cat">${esc(s.category || "其他")}</span>
          </div>
        </div>`;
    }
    html += `</div></div>`;
  }
  body.innerHTML = html;
  body.querySelectorAll<HTMLElement>(".skill-card").forEach((el) => {
    el.onclick = () => {
      const path = el.dataset.skillPath!;
      const name = el.dataset.skillName!;
      // Clear any prior skill's history so back from the first page
      // goes to the list, not to a previously viewed skill.
      skillNavStack = [];
      openSkillViewer(name, path, /*pushToStack*/ true);
    };
  });
}

// Open the skill viewer modal on a specific file. `pushToStack` controls
// whether this navigation becomes a new history entry: true when the
// user clicked forward (skill card, reference link), false when
// restoring from the back stack.
function openSkillViewer(displayName: string, filePath: string, pushToStack: boolean = true) {
  if (pushToStack) {
    skillNavStack.push({ name: displayName, path: filePath });
  }
  closeSkillViewer();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "skillsModalBackdrop";
  const showBack = skillNavStack.length > 0;
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <button class="fullpage-back" id="skillsModalBack" style="display:${showBack ? "flex" : "none"}">
          <span class="back-arrow">←</span>
          <span>back</span>
        </button>
        <div class="fullpage-title" id="skillsModalTitle">${esc(displayName)}</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body fullpage-body-wide" id="skillsModalBody">
        <div class="skills-modal-loading">Loading ${esc(displayName)}...</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  (backdrop.querySelector("#skillsModalBack") as HTMLElement).onclick = () => {
    // Pop the current page; if there's still a previous page, render
    // it. If the stack is now empty, fall back to the skill list.
    skillNavStack.pop();
    const prev = skillNavStack[skillNavStack.length - 1];
    if (prev) {
      openSkillViewer(prev.name, prev.path, /*pushToStack*/ false);
    } else {
      openSkillsBrowser();
    }
  };

  loadSkillFileIntoModal(displayName, filePath);
}

export function closeSkillViewer() {
  document.getElementById("skillsModalBackdrop")?.remove();
}

// Fetch a skill file and render its markdown body into the modal.
// Relative `.md`/`.markdown`/text links in the rendered HTML are
// re-wired to a click handler that re-enters this function with the
// resolved absolute path, so users can navigate within a skill's
// reference tree without leaving the chat surface.
async function loadSkillFileIntoModal(displayName: string, filePath: string) {
  const body = document.getElementById("skillsModalBody");
  const title = document.getElementById("skillsModalTitle");
  if (!body || !title) return;
  body.innerHTML = '<div class="skills-modal-loading">Loading...</div>';
  if (title) title.textContent = displayName;
  try {
    const data = await api.readSkillFile(filePath);
    // Strip the YAML frontmatter for display — the sidebar list already
    // shows the description, and the raw frontmatter adds noise.
    const content = stripFrontmatter(data.content);
    body.innerHTML = `<div class="md">${renderMarkdown(content)}</div>`;
    addCopyButtons(body);
    highlightCode(body);
    interceptSkillLinks(body, data.path);
    // Scroll the modal to the top whenever a new file is loaded
    body.scrollTop = 0;
  } catch (e) {
    const msg = (e as any)?.error || (e as Error).message;
    body.innerHTML = `<div class="skills-modal-error">Failed to load: ${esc(msg)}</div>`;
  }
}

// Walk the rendered markdown container and turn any relative link
// pointing to a file under the same skill directory into a click
// handler that loads that file inline. External / absolute links
// remain normal `<a>` elements (still openable in a new tab, etc.).
function interceptSkillLinks(container: HTMLElement, currentFilePath: string) {
  const links = container.querySelectorAll<HTMLAnchorElement>("a[href]");
  const baseDir = currentFilePath.replace(/[^/]+$/, "");
  for (const a of links) {
    const href = a.getAttribute("href") || "";
    // Skip external links, anchors, mailto, etc.
    if (!href || href.startsWith("http") || href.startsWith("https") ||
        href.startsWith("mailto:") || href.startsWith("#") ||
        href.startsWith("/")) {
      // For absolute paths that point into the skill roots, still intercept
      if (href.startsWith("/") && isPathInSkillRoots(href)) {
        a.classList.add("skill-file-link");
        a.onclick = (e) => {
          e.preventDefault();
          const name = href.split("/").pop() || href;
          openSkillViewer(name, href, /*pushToStack*/ true);
        };
      }
      continue;
    }
    // Strip any anchor fragment for the file resolution
    const [rel] = href.split("#");
    if (!rel) continue;
    // Only intercept .md / .markdown / .txt / no-extension references —
    // everything else (images, binaries) is left as a plain link.
    const isLikelyDoc = /\.(md|markdown|txt)$/i.test(rel) || !/\.[a-z0-9]+$/i.test(rel);
    if (!isLikelyDoc) continue;
    const resolved = baseDir + rel;
    a.classList.add("skill-file-link");
    a.onclick = (e) => {
      e.preventDefault();
      const name = rel.split("/").pop() || rel;
      openSkillViewer(name, resolved, /*pushToStack*/ true);
    };
  }
}

function isPathInSkillRoots(p: string): boolean {
  // Best-effort client-side check — the server is the final authority.
  // We don't know the skill roots here, so allow anything that looks
  // like a markdown file and let the server reject if it's outside.
  return /\.(md|markdown|txt)$/i.test(p);
}

function stripFrontmatter(content: string): string {
  // YAML frontmatter is delimited by `---` lines at the very top of the
  // file. We strip it for display so the user sees only the body of
  // the skill, not its metadata block.
  if (!content.startsWith("---")) return content;
  const end = content.indexOf("\n---", 3);
  if (end < 0) return content;
  // Skip past the closing `---` and any trailing newline
  let rest = content.slice(end + 4);
  if (rest.startsWith("\n")) rest = rest.slice(1);
  return rest;
}
