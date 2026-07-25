/** Pure formatting helpers shared across modules. */

/** Compact relative-time label ("now" / "12m" / "3h" / "2d" / "1w" / "3mo").
 *  ``tsSeconds`` is the backend's ``time.time()`` value (seconds since epoch).
 *  Returns "" for missing/zero input — callers decide whether to render
 *  a fallback like "never" / "just now". */
export function formatRelativeSeconds(tsSeconds?: number): string {
  if (!tsSeconds) return "";
  const now = Date.now();
  const diffMs = now - tsSeconds * 1000;
  const mins = Math.floor(diffMs / 60_000);
  const hours = Math.floor(diffMs / 3_600_000);
  const days = Math.floor(diffMs / 86_400_000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  if (hours < 24) return `${hours}h`;
  if (days < 7) return `${days}d`;
  if (days < 30) return `${Math.floor(days / 7)}w`;
  return `${Math.floor(days / 30)}mo`;
}

/** Same compact format, but accepts a JS millisecond timestamp (Date.now()-style). */
export function formatRelativeTime(tsMs?: number): string {
  if (!tsMs) return "";
  const now = Date.now();
  const diffMs = now - tsMs;
  const mins = Math.floor(diffMs / 60_000);
  const hours = Math.floor(diffMs / 3_600_000);
  const days = Math.floor(diffMs / 86_400_000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  if (hours < 24) return `${hours}h`;
  if (days < 7) return `${days}d`;
  if (days < 30) return `${Math.floor(days / 7)}w`;
  return `${Math.floor(days / 30)}mo`;
}
