# Multi-Agent Collaboration Plan

## Objective
Build a Codex-like CLI/Desktop backend in this repo with unified extension API.

## Agent responsibilities

### Codex
- Own runtime architecture and integration.
- Implement extension interfaces and registries.
- Implement ACP transport and OpenAI Agents adapter wiring.
- Maintain model-tool loop and event-stream contracts.
- Merge and stabilize all branches.

### AGY (antigravity)
- Scaffold plugin packs and templates.
- Implement sample/custom plugins for `prompt/tool/skill/hook/memory`.
- Add developer scripts for plugin create/list/validate.

### Claude Code
- Produce spec-quality docs and API contracts.
- Define config/manifest validation constraints and error semantics.
- Add negative and compatibility tests (protocol and plugin failures).

## Integration cadence
1. Interface freeze window for shared contracts each day.
2. PR checks must include protocol and plugin-loader tests.
3. Runtime contract changes require doc updates in same PR.

## Current protocol checkpoint
- ACP methods:
  - `initialize`
  - `ping`
  - `tools/list`
  - `chat`
  - `chat_stream` (returns turn event timeline + final response)
- Desktop API:
  - `POST /sessions`
  - `POST /sessions/{id}/turns`
  - `GET /sessions/{id}/events` (SSE)
- Tool call formats:
  - `[[TOOL_CALL]]{...}[[/TOOL_CALL]]` (preferred)
  - `TOOL_CALL <name> <json>` (legacy fallback)
