# Changelog

All notable changes to Ziva will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Sub-agent permission UI persistence** — Switching an agent's tool/skill/
  hook selector between inherit / allow / deny now round-trips cleanly.
  Previously, switching from "allow specific" to "inherit all" would
  silently leave the old allow-list on disk, so re-opening the modal
  would show the selector snapping back to "allow". The deep-merge now
  treats an explicit `null` overlay value as a key deletion; the UI
  sends `null` for whichever side of the pair (`tools` ↔ `deny_tools`)
  is inactive for the current mode.
- **Hooks selector uses registered hook IDs, not event names** — The
  per-agent hooks dimension in Settings now lists each *registered* hook
  by its ID (e.g. `hook.image_guard`) with a label like
  `image_guard · before_tool`. The runtime filters sub-agent hooks by
  hook id first, falling back to event name for backwards compatibility
  with older configs. deny-mode now blocks specific hook ids instead of
  whole event phases.
- **Agent description input full width** — The "Description" field on
  the per-agent card was rendering at ~300px (the browser's default
  `<input>` width) because it lived inside a block-level
  `.settings-section` where `flex: 1` is a no-op. It now spans the full
  card width like the adjacent Instructions textarea.

## [1.1.5] - 2026-08-04

### Added
- **Sub-agent three-state permission control** — `tools` / `skills` /
  `hooks` now support inherit (omit key), allow (list), and deny (list) on
  the spawn-agent UI. Spawned agents translate the UI selection into
  `_allowed_tools` / `_allowed_skills` / `_allowed_hooks` automatically.
- **Custom sub-agents** — `config.agents.<name>` blocks now register as
  spawnable agents, and `spawn_agent` lists them in its dynamic enum.
- **Hook system overhaul** — `BaseHook` is the unified base for Python and
  shell hooks (`event_name` / `matcher` / `block` / `timeout` / `async_run`).
  A new `ShellHook` lets you add hooks without writing Python — drop a
  folder with `manifest.yaml` + `impl.sh`, the runtime loads it. The
  registry hot-reloads via `GET /api/hooks` + `POST /api/hooks/register`.
- **`before_tool` hooks can block** — non-zero exit (shell) / `_block=True`
  (Python) aborts the tool call and surfaces the reason to the model.
- **`Hooks` tab is now a real registration surface** — folder paths you
  register are loaded on next start; previously-registered hooks show as
  cards (event / matcher / blocking / timeout).
- **Agent description field** on the settings card; surfaced in the
  `spawn_agent` tool description so the parent model picks the right agent.
- **`image_guard` example shell hook** — blocks non-vision models that
  try to `read_file` an image (redirects them to specialized
  image-analysis tools). Lives at `~/.ziva/plugins/hooks/image_guard/`.
- **`/api/hooks` endpoints** — `GET` lists registered hooks with their
  effective capability flags (null for fields the manifest didn't set);
  `POST /api/hooks/register` adds a folder path and reloads.

### Changed
- **App icon now respects macOS HIG padding** — was 100% canvas fill, now
  80.5% (~10% transparent padding per side), matching Apple's
  `824/1024` convention. Other app icons no longer look bigger.
- **Loading screen infinity symbol** uses the icon's exact Arial Rounded
  Bold ∞ centerline (was a hand-drawn bezier approximation that didn't
  match); stroke-width and container size tuned to keep the proportions.
- **`ZivaOS` wordmark** is now 38 px (was 30 px); all other styling
  matches the original. Infinity container shrunk 5 % (92×38 → 87×36) to
  give the bigger wordmark room.
- **Multimodal prompt** — when `supports_image: true`, the system prompt
  tells the model to use `read_file` for image paths instead of reaching
  for external image-analysis tools. Plugin tools are no longer named in
  the base prompt (they belong in their own tool descriptions).
- **`_build_skill_index` runtime fallback removed** — `read_skill` errors
  if the runtime is missing the skill registry, instead of silently
  returning empty. Same lookup goes through `Runtime.get_skill()` (O(1)
  over a per-startup dict cache).
- **Skill list only injected when reachable** — `_allowed_tools` not
  containing `read_skill` skips the entire "Available Skills" block in
  the system prompt; `_allowed_skills` non-None filters to the allow-list.

### Removed
- **`_maybe_apply_skill` dead code** — `plugins/skills/` was never
  registered as a plugin kind, so the function was unreachable. Skills
  now flow through `plugins/tools/read_skill` only.

## [1.1.4] - 2026-08-02

### Added
- **Per-session reasoning effort** — `thinking_mode` is now per-session
  (persisted across restarts), exposed as a composer dropdown, a `/effort`
  slash command + structured picker (desktop), and `/effort` on IM/CLI. The
  backend maps it to OpenAI `reasoning_effort`, Anthropic adaptive thinking +
  `output_config.effort`, or MiniMax `reasoning_split`.
- **Per-model effort levels** — models can declare
  `capabilities.effort_levels`; the UI only offers supported levels and
  downgrades gracefully on model switch (max→xhigh→…→disabled). Default is
  the full `low…max` range.
- **Multi-provider same model** — same-named models across providers (e.g.
  `glm-5.2` under `glm` and `opencode`) are now distinguishable via a
  `provider_name` field; the composer groups by provider and `/model`
  (desktop + IM) uses `provider:model`.

### Changed
- **Dropped `thinking_budget_tokens`** — effort is adaptive+effort
  everywhere now; no more mode→budget mapping.
- **Default effort is the model's highest level** (max), not "off", so a new
  session reasons from the first turn and UI/backend agree.
- **Composer selects fit their label** (no widening to the longest option)
  and the native dropdown arrow is hidden — the control already opens a list
  on click.

### Fixed
- **Switching sessions no longer corrupts the model** — the composer's
  composite `provider|model` value was persisted as `model_name`, so
  switching back matched no model and the dropdown fell through to the first
  option with effort lookup failing. Now parsed into clean `model_name` +
  `provider_name`; legacy corrupted sessions are healed on read.
- **`/model` + `/effort` pickers render in empty sessions** without hiding
  the status bar / workspace selector.
- **IM `/model` shows the provider**; **IM `/new` echoes model + effort** and
  pins them on the fresh session.
- **IM reconnect no longer requires re-entering credentials** — a configured
  channel reconnects with saved secrets (one click), and start failures no
  longer flip `enabled:false`, so a transient drop auto-retries on the next
  restart instead of forcing a full re-setup.
- **MiniMax-M2.7** now gets `reasoning_split` (the minimax check only
  matched M3).
- **`esc()`** now escapes double quotes so HTML attribute values aren't
  truncated.
- **Effort dropdown no longer truncates "medium" to "m..."** — the select is
  sized to its selected label, but the width wasn't refit after changing
  effort, so switching to a longer label (e.g. max → medium) kept the old
  narrower width and `text-overflow: ellipsis` cut it.

## [1.1.3] - 2026-07-31

### Fixed
- **`send_file` no longer shown in non-IM sessions** — it was exposed in
  desktop sessions too, and the model often ignored the description's
  "only use on IM" hint. Now declarative: a tool's manifest can list
  `config.transports`, and `_build_tools_param` hides it when the session's
  transport (im vs desktop, from `session.im_channel`) doesn't match.
  `send_file` declares `transports:[im]`.
- **STT model config unified to a full HF repo id** —
  loader/server/stt_warmup/README all use `mlx-community/whisper-small-mlx`
  verbatim. server.py and stt_warmup.py no longer prepend `mlx-community/`,
  which double-prefixed when the config already carried the org (HF repo
  not found). Local pre-download at `~/.ziva/models/<repo-id>/` still works.
- **README**: dropped `skill` from the manifest-loaded list — skills load
  from the filesystem via `build_skill_index` + `skill.extra_paths`, not
  `manifest.yaml`.

## [1.1.2] - 2026-07-30

### Fixed
- **chrome-devtools-mcp tools on the embedded browser** — `close_page`,
  `select_page(bringToFront)` and `new_page(isolatedContext)` no longer
  fail with "Method not handled at browser level". The CDP bridge now
  handles `Target.closeTarget` / `activateTarget` / `createBrowserContext`
  / `disposeBrowserContext`, and a page closed via the MCP tool is also
  dropped from the tabstrip (previously left as a stale tab).
- **Phantom "Tool execution cancelled by user"** after a sub-agent
  returned — the frozen backend raised `ModuleNotFoundError:
  ziva.adapters._think_parser` (a lazy import PyInstaller's static
  analysis missed), which killed the parent turn mid-stream; the
  orphaned tool_calls were then sanitized next turn as a cancellation.
  Force-included the module in `ziva-backend.spec`.

## [1.1.1] - 2026-07-29

### Added
- **Restart Ziva from any of 4 entry points** — `Ziva → Restart Ziva` menu,
  `ziva desktop restart` CLI, chat `/restart` slash command, and
  IM `/restart` (Telegram / Feishu) all converge on the same
  `app.relaunch()` + `app.quit()` flow. CLI / chat / IM ack the same
  `✅ Restarted in Xs` string so an AI agent and the human agree on
  what happened. IM restart also reboots the IM bridge adapters, not
  just the Python backend.

### Changed
- **Skill viewer shows full frontmatter** above the markdown body in a
  dedicated meta block (name, description, category, version, tags,
  every other frontmatter field). Card preview keeps the 3-line clamp
  for scanability, but nothing is truncated server-side — the full
  description is one click away.

### Fixed
- `read_skill_file` endpoint now matches the symlink policy in
  `Runtime.build_skill_index`: a request whose raw path lives inside a configured
  `skill.extra_paths` root is accepted only when its symlink-resolved
  target also stays inside `$HOME`. Closes a hole where a stray URL
  like `?path=~/.ziva/skills/<symlink-to-ssh>` could otherwise read
  private files. The Claude-shared-tree pattern (symlink to
  `~/.claude/skills/`) still works because the target is in HOME.

## [1.1.0] - 2026-07-26

### Changed
- Adopted new tagline across the desktop composer placeholder:
  `无远弗届，所言即所达` / `Boundless in reach. Prompted, perfected.`

### Fixed
- Unified reasoning handling across Anthropic, OpenAI, and the runtime
  `chat_with_events` / `_run_model_tool_loop` paths. Reasoning content,
  signatures, and final-answer separation now flow through a single
  `reasoning_split` helper, removing duplicated parsing in each adapter.
- Web UI: queued (Codex-style) messages no longer sit un-flushed when the
  user switches away from a session whose turn is still running. The
  background-session SSE path now mirrors the active-session
  `flushComposerQueue` behavior on `turn_end` / `turn_cancelled` /
  `turn_failed`, with a `wasRunning` guard so a late duplicate
  `turn_cancelled` cannot clobber a fresh `turn_start`.

## [1.0.0] - 2026-07-13

### Added

- Initial open-source release.
- Core Python SDK with model-tool loop, context compaction, and MCP clients.
- Vite + TypeScript desktop web UI with SSE real-time streaming.
- Electron native shell with Chromium side-by-side browser automation.
- IM bridge for Feishu (Lark) and Telegram, supporting text and image input.
- Desktop slash commands: `/new`, `/model`, `/stop`, `/compact`, `/prune`.
- IM slash commands: `/new`, `/model`, `/stop`, `/compact`, `/prune`.
- Plugin system for tools, skills, memory backends, hooks, and prompts.
- On-device Apple Silicon STT via `mlx-whisper`.

### Fixed

- Feishu image download now uses the V3 `message_resource` API.
- IM-driven sessions now show running state and a working stop button in the desktop UI.
- `/model` model switches sync to the runtime `SessionState` and the desktop UI dropdown.
- `/stop` on IM is processed outside the per-conversation lock so it can cancel a running turn.

[Unreleased]: https://github.com/ziva-ai/ziva/compare/1.0.0...HEAD
[1.0.0]: https://github.com/ziva-ai/ziva/releases/tag/1.0.0
