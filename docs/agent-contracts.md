# Agent Contracts (AGY / Claude Code / Codex)

## Shared invariants
- All extension types use manifest + entry loading.
- Runtime event schema changes require tests in `tests/`.
- ACP and Desktop API behavior must remain backward compatible.
- Tool call protocol supports:
  - Structured block: `[[TOOL_CALL]]{"name":"...","arguments":{...}}[[/TOOL_CALL]]`
  - Legacy fallback: `TOOL_CALL <name> <json>`

## AGY (antigravity) contract
- Own `plugins/` scaffolding and plugin generator scripts.
- Implement richer default plugins (filesystem, grep-like, task memory).
- Add plugin lint checks (manifest completeness and entry existence).

## Claude Code contract
- Own docs/spec and schema constraints.
- Define formal JSON schema for config + plugin manifests.
- Add negative tests for malformed tool protocol blocks and invalid manifests.

## Codex contract
- Own runtime core and transport integration.
- Maintain model-tool loop, event-stream contract, and protocol compatibility.
- Merge, resolve regressions, and keep full tests green.

## PR acceptance gate
- `PYTHONPATH=src python -m pytest -q -p no:capture tests`
- `PYTHONPATH=src python scripts/smoke_test.py`
