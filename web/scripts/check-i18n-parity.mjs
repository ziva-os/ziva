#!/usr/bin/env node
/**
 * Assert zh.ts covers every key in en.ts (and has no extra keys), so the two
 * locales stay in sync. Mirrors opencode's i18n parity.test.ts, but needs no
 * test runner — esbuild (already a transitive dep via vite) strips types so we
 * can import the .ts dictionaries directly.
 *
 * Run: `node scripts/check-i18n-parity.mjs`  (or `npm run check:i18n`)
 */
import * as esbuild from "esbuild";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const dir = fileURLToPath(new URL("../src/i18n/", import.meta.url));

async function loadKeys(file) {
  const src = readFileSync(dir + file, "utf8");
  // `import type` (zh.ts's only import) is erased by esbuild, so no resolution needed.
  const { code } = await esbuild.transform(src, { loader: "ts", format: "esm" });
  const mod = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
  if (!mod.dict) throw new Error(`${file} has no \`dict\` export`);
  return new Set(Object.keys(mod.dict));
}

const en = await loadKeys("en.ts");
const zh = await loadKeys("zh.ts");

const missingInZh = [...en].filter((k) => !zh.has(k));
const extraInZh = [...zh].filter((k) => !en.has(k));

if (missingInZh.length || extraInZh.length) {
  let msg = "i18n parity check FAILED:\n";
  if (missingInZh.length) msg += `  Missing in zh.ts (${missingInZh.length}): ${missingInZh.join(", ")}\n`;
  if (extraInZh.length) msg += `  Present in zh.ts but not en.ts (${extraInZh.length}): ${extraInZh.join(", ")}\n`;
  console.error(msg);
  process.exit(1);
}
console.log(`i18n parity OK — ${en.size} keys matched in en.ts and zh.ts.`);
