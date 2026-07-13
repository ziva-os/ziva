# Changelog

All notable changes to Ziva will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
