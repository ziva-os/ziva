# Changelog

All notable changes to Ziva will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
