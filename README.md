# ziva

Clean-slate implementation of a Codex-like CLI/Desktop backend runtime.

## Implemented
- Unified extension API for `prompt/tool/skill/hook/memory`
- YAML config with layered merge/strict validation
- Plugin manifest loader with runtime checks
- OpenAI Agents adapter (single-SDK strategy)
- ACP protocol server:
  - `initialize`
  - `ping`
  - `tools/list`
  - `chat`
  - `chat_stream` (event timeline + final output)
  - `chat_stream_chunks` (incremental chunk list + final payload)
- Desktop backend API + minimal UI shell with SSE timeline
- Session history endpoints for desktop:
  - `GET /sessions`
  - `GET /sessions/{id}/messages`
  - `GET /sessions/{id}/turns`
- Model -> tool -> model execution loop with guardrails

## Tool call protocol
Preferred structured format:

```text
[[TOOL_CALL]]{"name":"echo","arguments":{"text":"hello"}}[[/TOOL_CALL]]
```

Backward-compatible format:

```text
TOOL_CALL echo {"text":"hello"}
```

## ACP error shape
ACP errors include JSON-RPC error + classification:

```json
{
  "error": {
    "code": -32602,
    "message": "params.messages must be a non-empty array",
    "data": {"classification": "invalid_params"}
  }
}
```

## Commands
```bash
# single prompt
PYTHONPATH=src python -m ziva_runtime.app.cli run "hello"

# ACP server over stdio
PYTHONPATH=src python -m ziva_runtime.app.cli acp serve

# desktop backend API (HTTP + SSE + minimal UI)
PYTHONPATH=src python -m ziva_runtime.app.cli desktop serve --host 127.0.0.1 --port 4097
# then open http://127.0.0.1:4097/
```

## Tests
Note: in this environment, pytest capture plugin segfaults; run with `-p no:capture`.

```bash
PYTHONPATH=src python -m pytest -q -p no:capture tests
PYTHONPATH=src python scripts/smoke_test.py
```
