# Ziva Architecture Design (from scratch, informed by Codex CLI and Pi Agent)

Date: 2026-05-22
Status: Draft v2 (implementation-ready)
Scope: Rebuild in a new folder, using OpenAI Agents SDK only, with ACP protocol support, and clear extensible architecture.

## 0. External Source Review (before design)

This design is updated after reviewing:
- OpenAI Codex CLI repository (`openai/codex`): multi-crate runtime, CLI/TUI split, MCP client+server support, config discipline.
- Pi coding-agent (`earendil-works/pi`, `packages/coding-agent`): extension-first customization, skills packaging, event-driven extension API.
- pi-agent Python runtime (`aniketmaurya/pi-agent`): small typed runtime with explicit event stream and provider abstraction.

Design implications adopted here:
- Keep a strict runtime core separated from transport/UI.
- Treat events as a first-class contract (for CLI, ACP, desktop streaming).
- Build plugin/skill/hook system as explicit package contracts, not ad-hoc imports.
- Keep config strongly validated, with layered overrides and deterministic resolution.

## 1. Goals and Non-Goals

### Goals
- Build a clean architecture from scratch under `codex-rebuild/`.
- Use **OpenAI Agents SDK** as the only model orchestration SDK.
- Support **ACP protocol** as first-class integration surface.
- Make configuration explicit and modular for:
  - `model`
  - `prompt`
  - `tool`
  - `skill`
  - `memory`
  - `hooks`
  - `plugin`
- Ensure UI can be smooth by consuming unified streaming events.

### Non-Goals (Phase 1)
- Multi-SDK support (Google/Claude SDK adapters).
- Complex distributed deployment.
- Full enterprise policy engine.

## 2. High-Level Architecture

Use layered architecture with strict dependency direction:

1. `kernel/`
- Session lifecycle and turn state machine.
- Event bus and typed events.
- Permission gate and execution policy.

2. `capabilities/`
- Registries and interfaces for `tool/skill/hook/memory`.
- Plugin loader and capability resolution.

3. `adapters/`
- `openai_agents/` adapter to OpenAI Agents SDK.
- `acp/` transport adapter (JSON-RPC over stdio or HTTP upgrade as later option).
- `storage/` adapter (sqlite/file in Phase 1).

4. `transports/`
- `cli/` transport (interactive + run mode).
- `desktop_api/` transport (SSE/WebSocket friendly API for desktop UI).

5. `apps/`
- `codex-cli` executable.
- `codex-desktop` backend entrypoint (frontend separated).

Dependency rule:
- outer layers depend on inner layers only.
- `kernel` never imports concrete plugin implementations.

Reference alignment:
- Codex-style separation of core logic and interface layers is mirrored by `kernel` + `transports`.
- Pi-style customization is mirrored by `plugins/` + capability registries.
- pi-agent style typed event/runtime loop is mirrored by `events.py` + `turn_engine.py`.

## 3. Directory Layout

```text
codex-rebuild/
  pyproject.toml
  README.md
  docs/
    plans/
  src/
    codex_runtime/
      kernel/
        session.py
        turn_engine.py
        events.py
        permissions.py
      capabilities/
        interfaces.py
        registries.py
        resolver.py
      plugins/
        loader.py
        manifest.py
        sandbox.py
      adapters/
        openai_agents/
          provider.py
          mapper.py
        acp/
          server.py
          protocol.py
        storage/
          sqlite_store.py
      transports/
        cli/
          app.py
        desktop_api/
          server.py
      config/
        schema/
          root.schema.json
          model.schema.json
          tool.schema.json
          skill.schema.json
          hook.schema.json
          memory.schema.json
          plugin.schema.json
        loader.py
        merger.py
        validator.py
      shared/
        types.py
        errors.py
  plugins/
    tools/
    skills/
    hooks/
    memory/
  tests/
```

## 4. Configuration Design (YAML + JSON Schema)

Three-level override:
- Global: `~/.codex-rebuild/config.yaml`
- Workspace: `<repo>/.codex/config.yaml`
- Session override: in-memory patch

Merge order:
`global <- workspace <- session`

Validation:
- Load YAML first.
- Validate each section using JSON Schema.
- Refuse boot if schema invalid (with exact path errors).

Example:
```yaml
model:
  provider: openai_agents
  name: gpt-5-codex
  options:
    reasoning: medium
prompt:
  profile: codex_default
  variables:
    project_name: demo
tool:
  allow: [read, write, grep, bash]
  deny: [rm_rf]
skill:
  enabled: [brainstorming]
memory:
  backend: sqlite
  context_window_tokens: 120000
hooks:
  before_turn: [audit_input]
  after_tool: [usage_log]
plugin:
  paths:
    - ./plugins
  trust:
    unsigned: low
```

## 5. Plugin System Design

### 5.1 Plugin Packaging
Each plugin folder contains:
- `manifest.yaml`
- implementation module
- optional assets/templates

Example:
```yaml
id: tool.read_file
type: tool
version: 0.1.0
entry: impl:ReadFileTool
config_schema: schema.json
permissions:
  fs: [read]
enabled_by_default: true
```

### 5.2 Plugin Types
- `tool`: executable capability exposed to model/runtime.
- `skill`: higher-level orchestrator for multi-step behaviors.
- `hook`: lifecycle extension.
- `memory`: retrieval/store provider.

### 5.3 Runtime Contracts
- Tool: `spec()`, `run(input, ctx)`
- Skill: `match(ctx)`, `execute(input, ctx)`
- Hook: `event_name`, `handle(event, ctx)`
- Memory: `put()`, `search()`, `summarize()`

### 5.4 Safety
- Permissions declared in manifest and enforced centrally.
- Hook cannot bypass permission gate.
- Optional process isolation in Phase 2.

### 5.5 Packaging and Discoverability (inspired by Pi packages)
- Support plugin roots from:
  - workspace local (`./plugins`)
  - user global (`~/.ziva/plugins`)
  - package-managed roots (future: pip/npm bridges)
- Auto-discovery from conventional folders:
  - `tools/`, `skills/`, `hooks/`, `memory/`
- Optional `plugin manifest index` file to pin versions and disable auto-load.

## 6. OpenAI Agents SDK Adapter

Single provider adapter only:
- Module: `adapters/openai_agents/provider.py`
- Responsibility:
  - map runtime messages -> OpenAI Agents input
  - run agent turn
  - emit normalized runtime events

Normalized event model:
- `token_delta`
- `tool_start`
- `tool_end`
- `permission_request`
- `error`
- `done`

Important constraint:
- Runtime remains owner of tool registry and permissions.
- SDK is used for model orchestration, not as owner of app state.

## 7. ACP Protocol Support

ACP server as transport adapter.

Phase 1 methods:
- `initialize`
- `ping`
- `chat`
- `tools/list`

Protocol choice:
- JSON-RPC 2.0 over stdio for first implementation.

Future-compatible note:
- Codex exposes MCP server mode; for Ziva we keep ACP first, and reserve MCP server adapter as Phase 2 bridge.

Behavior rules:
- Stateless requests allowed.
- Session-bound chat supported via `session_id` in params.
- Errors follow JSON-RPC error object.

## 8. Runtime Lifecycle

Turn lifecycle:
1. `before_turn` hooks
2. build effective prompt + context
3. call OpenAI Agents adapter
4. execute tool calls via permission gate
5. `after_tool` hooks per tool
6. finalize response
7. `after_turn` hooks

Failure lifecycle:
- Retry only on transient model errors.
- Tool failures become structured error events.
- Never hide permission denials.

## 9. UI Smoothness Design Requirements

Even before desktop UI code, backend must guarantee stream quality:

- First token latency target: `< 700ms` (excluding model-side queueing)
- Event cadence: incremental deltas, no giant flush
- Backpressure handling for slow clients
- Session replay cursor for reconnect

Desktop-facing API (Phase 1):
- `POST /sessions`
- `POST /sessions/{id}/turns`
- `GET /sessions/{id}/events` (SSE)

Design note from Codex/Pi comparison:
- Keep a single normalized event contract consumed by CLI and desktop.
- Never stream provider-native raw events directly to UI; always map through runtime event schema.

## 10. MVP Milestones

### M1 (Foundation)
- Project scaffold
- Config loader/merge/validate
- Event model + turn engine skeleton

### M2 (Model + ACP)
- OpenAI Agents adapter
- ACP server (`initialize/ping/chat/tools/list`)
- Basic CLI run mode
- Typed event stream tests (golden fixtures)

### M3 (Extensibility)
- Plugin loader + registries
- one sample `tool/skill/hook`
- permission gate

### M4 (Desktop-ready backend)
- SSE event endpoint
- reconnect replay
- minimal session persistence

## 11. Risks and Mitigations

1. SDK event shape changes
- Mitigation: isolate mapping in adapter `mapper.py`.

2. Plugin quality inconsistency
- Mitigation: strict manifest schema + startup validation.

3. UI stutter from oversized events
- Mitigation: enforce chunking and event size limits.

4. ACP interoperability ambiguity
- Mitigation: maintain explicit protocol tests with golden JSON fixtures.

5. Plugin surface becoming too permissive (Pi-style power tradeoff)
- Mitigation: default deny for sensitive permissions; explicit trust levels per plugin source.

## 12. Immediate Implementation Plan

1. Scaffold `codex-rebuild/src/codex_runtime` package.
2. Implement config subsystem + schemas first.
3. Implement event bus + turn engine core.
4. Add OpenAI Agents adapter.
5. Add ACP stdio server.
6. Add one sample plugin for each type (`tool/skill/hook`).
7. Add CLI entrypoint and smoke tests.
