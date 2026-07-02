/**DOM + small pure helpers shared across the frontend.

Extracted from main.ts as the first step of the modularization (Phase 3).
These are leaf utilities — they depend on nothing else in the app, so every
other module can safely import them.
*/

/** HTML-escape a string for safe interpolation into innerHTML templates. */
export function esc(s: string): string {
  const d = document.createElement("span");
  d.textContent = s;
  return d.innerHTML;
}

/** getElementById shorthand with a typed cast (the app's ubiquitous `$()`). */
export const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

/**
 * Drag a vertical resizer to resize a pane's width. `side` = which side the
 * target pane sits on relative to the handle: a "left" pane grows when the
 * handle is dragged right, a "right" pane grows when dragged left. Width is
 * not persisted (per product decision — resets on reopen).
 */
export function bindResizer(handle: HTMLElement, target: HTMLElement, side: "left" | "right", min = 120, max = 600): void {
  handle.addEventListener("mousedown", (e: MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = target.offsetWidth;
    const dir = side === "left" ? 1 : -1;
    target.style.maxWidth = "none"; // clear any CSS max-width cap so drag wins
    const onMove = (ev: MouseEvent) => {
      const w = Math.max(min, Math.min(max, startW + dir * (ev.clientX - startX)));
      target.style.width = w + "px";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
