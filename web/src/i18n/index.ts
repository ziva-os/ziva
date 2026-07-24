/**
 * Lightweight i18n — flat-key dictionaries, zero dependencies.
 *
 * Modeled on opencode's desktop i18n (packages/desktop/src/renderer/i18n):
 * each locale is a flat `dict` of dotted keys + `{{var}}` interpolation,
 * English is the base and the active locale overlays it (missing keys fall
 * back to English so partial translations still render). Plurals use the
 * browser-native `Intl.PluralRules`, which supports every language's rules
 * (one/other for en/zh, few/many for ru/pl, …) with no library.
 *
 * The UI is imperative DOM (innerHTML/textContent), so switching language
 * re-renders via `location.reload()` — drafts are persisted in localStorage
 * (state.ts) so the user loses no in-flight input.
 */
import { dict as en, type Dict } from "./en";
import { dict as zh } from "./zh";

export type Lang = "zh" | "en";

const tables: Record<Lang, Partial<Dict>> = { en, zh };

function detect(): Lang {
  // 1. An explicit user choice always wins.
  try {
    const saved = localStorage.getItem("ziva-lang");
    if (saved === "zh" || saved === "en") return saved;
  } catch {
    /* localStorage unavailable */
  }
  // 2. Only switch to English for an unambiguous English environment;
  //    everything else (incl. undeterminable) defaults to Chinese.
  const langs =
    typeof navigator !== "undefined" && navigator.languages && navigator.languages.length
      ? navigator.languages
      : typeof navigator !== "undefined" && navigator.language
        ? [navigator.language]
        : [];
  for (const l of langs) {
    if (!l) continue;
    const low = l.toLowerCase();
    if (low.startsWith("en")) return "en";
    if (low.startsWith("zh")) return "zh";
  }
  return "zh";
}

let lang: Lang = detect();

// Merged table: English base overlaid with the active locale. Recomputed on
// setLang (followed by a reload), so reads during a session are a cheap lookup.
let table: Dict = merge(lang);
function merge(l: Lang): Dict {
  return l === "en" ? en : { ...en, ...zh };
}

if (typeof document !== "undefined") {
  document.documentElement.setAttribute("lang", lang);
}

/** Resolve `{{var}}` placeholders against `params`. */
function resolveTemplate(s: string, p?: Record<string, string | number>): string {
  if (!p) return s;
  return s.replace(/\{\{(\w+)\}\}/g, (_, k) => (p[k] != null ? String(p[k]) : ""));
}

/**
 * Translate a key. `key` is constrained to the English dictionary's keys, so a
 * typo or stale key is a compile error.
 */
export function t<K extends keyof Dict>(key: K, p?: Record<string, string | number>): string {
  const s = (table[key] ?? en[key] ?? (key as string)) as string;
  return resolveTemplate(s, p);
}

type PluralForm = {
  zero?: string;
  one?: string;
  two?: string;
  few?: string;
  many?: string;
  other?: string;
};

/**
 * Pick a plural form via the browser-native plural rules for the active
 * locale. e.g. `plural(n, { one: "1 session", other: "{{n}} sessions" })`.
 * Use `{{n}}` inside the form strings and pass `{ n }` — but note plural
 * forms are NOT auto-templated, so prefer pre-inlined counts.
 */
export function plural(n: number, forms: PluralForm): string {
  const cat = new Intl.PluralRules(lang).select(n);
  return forms[cat as keyof PluralForm] ?? forms.other ?? forms.one ?? String(n);
}

export function getLang(): Lang {
  return lang;
}

/** Persist the choice and reload so every imperative template re-runs `t()`. */
export function setLang(l: Lang): void {
  lang = l;
  table = merge(l);
  try {
    localStorage.setItem("ziva-lang", l);
  } catch {
    /* ignore */
  }
  if (typeof document !== "undefined") document.documentElement.setAttribute("lang", l);
  if (typeof location !== "undefined") location.reload();
}
