/** Shared modal/overlay helpers extracted from main.ts. */

/**
 * Remove every fullpage overlay currently on screen. Called when the user
 * picks a destination from the sidebar (a session, a nav item other than the
 * current one) so the chat surface is restored. Every modal's backdrop uses
 * the `.fullpage-overlay` class, so this dismisses them all uniformly.
 */
export function closeAllFullpageOverlays(): void {
  document.querySelectorAll(".fullpage-overlay").forEach((el) => el.remove());
}
