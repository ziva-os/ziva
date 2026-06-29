# ziva 代码分析报告

**项目**: `/Users/wangxinxin/code/ziva` — Codex-like CLI/Desktop backend runtime
**生成时间**: 2026-06-23
**Python ≥ 3.10**，主依赖 `pyyaml / aiohttp / rich / prompt_toolkit / openai / mcp / anthropic`
**测试**: 57 个 `tests/test_*.py`（`conftest.py:25-37` 提供 adapter cache 隔离和 echo 工具注入）

---

## 1. 项目结构与架构

### 1.1 顶层目录

| 目录 | 职责 |
| --- | --- |
| `src/ziva_runtime/` | Python 后端核心（runtime、协议、传输、capability、session、storage、permissions、adapters、config、plugins） |
| `web/` | TypeScript + Vite 实现的 Web UI（聊天面板、Files/Terminal/Agent Browser 侧栏、设置），通过 `fetch`/`EventSource(SSE)` 与 Python 后端 HTTP+4097 端口通信 |
| `electron/` | Electron 桌面壳子：spawn `python -m ziva_runtime ...` + 暴露 CDP 桥接给 chrome-devtools-mcp；通过 IPC `get-backend-url` 通知渲染层后端地址（`electron/main.ts:14-35`, `137-141`） |
| `plugins/` | 5 大类扩展（`tools / hooks / memory / skills` 外加 `prompts` 目录常量）通过 manifest + entry 动态加载（`src/ziva_runtime/plugins/loader.py:11-17`） |
| `tests/` | pytest 套件；`conftest.py:25-37` 自动重置 adapter 单例并注入 `tool.echo` 桩 |
| `docs/`, `scripts/`, `solar_system/`, `scratch/`, `tmp/` | 设计文档（`agent-contracts.md`、`collaboration-plan.md`）、smoke / real API 脚本、临时调试目录 |

### 1.2 Python 后端模块依赖（`src/ziva_runtime/`）

```
runtime.py (主胶水层, 1780 行)
  ├─ adapters/openai/provider.py       (ModelAdapter, OpenAIChatAdapter, _ThinkTagParser)
  ├─ adapters/anthropic/provider.py    (AnthropicChatAdapter)
  ├─ adapters/mcp/{client,server}.py   (MCPClient / MCPServer*)
  ├─ adapters/retry.py                 (call_with_retry, _is_retryable, 状态码 429/5xx/529)
  ├─ capabilities/{events,interfaces,registries}.py
  │     EventBus (per-session + global queue) ─ registries.CapabilityRegistry
  ├─ config/{loader,instructions}.py   (load_effective_config, validate_config, AGENTS.md)
  ├─ permissions/manager.py            (PermissionManager, ask/reply, RejectedError/DeniedError)
  ├─ plugins/{loader,manifest}.py
  ├─ session/compaction.py             (compact_messages, prune_messages, _llm_context)
  ├─ shared_types.py                   (ChatMessage/ChatResult/Event/ToolResult/MCPConnectStatus/SessionState/CancellationToken)
  └─ storage/file_storage.py           (JSONL + fcntl 锁 + workspace SHA-256 16 字符 PID)

transports/desktop_api/server.py  ── 引入 aiohttp.web，注册 50+ 路由
protocols/acp.py                  ── JSON-RPC 2.0 over stdio，5 个 chat 变体
app/cli.py                        ── argparse 子命令：run / repl / acp serve / desktop serve
```

### 1.3 前端 ↔ 后端通信
- **Web UI** (`web/src/main.ts:1-7, web/src/api.ts:55-82`)：`fetch` 调 HTTP；SSE 走单一全局 `/events`（`web/src/sse.ts:22-163`，fan-out 多 session 通过 `session_id` 字段），避免历史 per-session SSE 池造成的 reader loop 风暴
- **Electron 入口** (`electron/main.ts:37-85`)：dev 模式 `python3 -m ziva_runtime desktop serve --port 4097`，prod 用 PyInstaller bundle (`ziva-backend.spec`)；主进程 `spawn` 后通过 stdout `Running on` 关键字同步等待就绪（也带 5s fallback）；`cdp-bridge.ts` 给 MCP 浏览器自动化暴露 `chrome-devtools` target
- **ACP 入口** (`src/ziva_runtime/app/cli.py:477-480, src/ziva_runtime/protocols/acp.py:207-227`)：stdio 逐行 JSON-RPC，readline 放 `asyncio.to_thread`

---

## 2. 核心模块分析

### 2.1 `runtime.py`（"runtime" 模块 = 编排引擎）
- **职责**: 串接 config / 插件 / adapters / session / event_bus，驱动单轮 chat 与多轮 tool loop。
- **关键类/函数**:
  - `Runtime`  dataclass: `runtime.py:381-398`
  - `Runtime.create()`: `runtime.py:586-625`（加载 config + 双源 plugin（workspace + skill.extra_paths）+ 初始化 PermissionManager）
  - `Runtime.chat()`: `runtime.py:627-757`（同步入口，加锁从磁盘恢复 history，跑一次 loop）
  - `Runtime.chat_with_events()`: `runtime.py:759-763`（清空 history 缓冲 → 调 chat → 返回整段 event 列表）
  - `Runtime.chat_streaming()`: `runtime.py:765-866`（带 `stream_reset` 一次重试 + turn_error/turn_end 收尾）
  - `Runtime._run_model_tool_loop()`: `runtime.py:868-1207`（启动期 auto-compact + 思考 + 流式 + 工具并行执行 + 取消时补 tool_result）
  - `Runtime._execute_tool()`: `runtime.py:1357-1477`（deny list → sub-agent 限制 → approval policy 钩子 → 实际执行 + 前后 hook）
  - `Runtime._build_tools_param()`: `runtime.py:1270-1293`（子 agent 屏蔽 `spawn_agent / get_agent_result / cancel_agent`）
  - `Runtime._connect_mcp_if_needed()`: `runtime.py:1209-1268`（状态机 `MCPConnectStatus`：DISCONNECTED/CONNECTING/CONNECTED/NO_CONFIG/FAILED）
  - `Runtime._emit()`: `runtime.py:1295-1301`（注入 `session_id/seq/ts` 后 broadcast）
  - `Runtime._sanitize_orphaned_tool_calls()`: `runtime.py:1629-1689`（补齐取消/崩溃留下的 orphan tool_use，避免 Anthropic 400）
  - `_create_adapter()` / `_reset_adapter_registry()`: `runtime.py:88-123`（按 `api_type, base_url, api_key` 缓存 + 测试隔离 helper）
- **对外接口**: `chat / chat_with_events / chat_streaming / on_ask_user / set_user_answer / cancel_all_questions / list_tools / list_sessions / get_session / delete_session / update_session_usage / shutdown / build_skill_index / _connect_mcp_if_needed`

### 2.2 `protocols/acp.py`
- **职责**: JSON-RPC 2.0 over stdio 的 ACP 协议，5 个 chat 变体 + tool 列表/ping。
- **关键类/函数**:
  - `ACPServer` + `handle()`: `acp.py:15-52`
  - `_chat()` / `_chat_stream()` / `_chat_stream_chunks()` / `_chat_stream_open()` / `_chat_stream_next()`: `acp.py:54-129`
  - `_build_chunks()` + `_split_content()`: `acp.py:137-186`（`word`/`char` 粒度切分）
  - `_ok()` / `_err()`: `acp.py:188-204`（错误码沿用 JSON-RPC：`method_not_found=-32601` / `invalid_params=-32602` / `parse_error=-32700`，统一 `data.classification` 字符串）
  - `serve_stdio()`: `acp.py:207-227`（readline 在 `asyncio.to_thread`，避免阻塞事件循环）
- **对外接口**: `ACPServer(runtime).handle(request_dict)`，CLI 入口 `python -m ziva_runtime.app.cli acp serve`

### 2.3 `capabilities/`（能力注册中心）
- `events.py:10-62` `EventBus`（`subscribe / subscribe_global / publish / history / unsubscribe / unsubscribe_all`，global queue 是 UI 单一 SSE 连接的依据）
- `interfaces.py:1-31` Protocol 类型：`PromptProvider / Tool / Skill / Hook / MemoryStore`
- `registries.py:1-34` `CapabilityRegistry`（按 id 注册 `CapabilityRecord`）

### 2.4 `plugins/`
- `loader.py:31-73` `discover_manifests / load_plugins`：扫 `plugins/{tools,skills,hooks,memory,prompts}/*/manifest.yaml`，`manifest.entry` 形如 `impl.py:ShellTool`
- `manifest.py:25-62` `load_manifest`：强校验 `id.namespace`、entry `module.py:Sym`、type ∈ `ALLOWED_TYPES`

### 2.5 `adapters/`
- `openai/provider.py`: `_build_api_messages` (`provider.py:11-46`)、`_ThinkTagParser` (`provider.py:49-117`)、`OpenAIChatAdapter.chat / chat_stream` (`provider.py:139-335`)
- `anthropic/provider.py`: `AnthropicChatAdapter.chat / chat_stream`（`provider.py:133-335`，仅对 `__aenter__` 做 `call_with_retry`，stream 自身靠 runtime 的 `stream_reset` 重试）
- `mcp/client.py` + `mcp/server.py`: `MCPClient`（`client.py:270-...`） + 本地包装的 `MCPServerStdio/Sse/StreamableHttp`
- `retry.py`: `call_with_retry` (`retry.py:69-86`) — 等抖指数退避 + 429/5xx/529 + content marker 重试

### 2.6 `permissions/`
- `manager.py` `PermissionManager` (`manager.py:108-295`)：`ask/reply/set_approved_rules/disabled_tools`，自定义异常 `PermissionError / RejectedError / CorrectedError / DeniedError`
- `wildcard.py` `match` (`wildcard.py:8-30`)：`*`→`.*`、`?`→`.`、支持 `space + *` 可选尾随

### 2.7 `config/`
- `loader.py`: `DEFAULT_CONFIG` (`loader.py:9-54`)、`_deep_merge` (`loader.py:57-64`)、`validate_config` (`loader.py:83-227`)、`load_effective_config` (`loader.py:229-244`)。workspace-local config 故意不读（注释明确禁止）
- `instructions.py:10-23`：仅 `~/.ziva/AGENTS.md` 与 `<workspace>/.ziva/AGENTS.md`

### 2.8 `session/compaction.py`
- `CompactAgent.run` (`compaction.py:21-43`)、`compact_messages` / `prune_messages` / `compose_post_compact_on_disk` / `_llm_context` / `estimate_tokens` / `find_last_summary_idx` / `find_cutoff_in_llm_visible`
- **关键设计**: 磁盘布局为 `[...preserved_old, summary1, ...recent_after_summary, summary2, ...]`，LLM 只看 "最新 summary + 之后消息"

### 2.9 `transports/desktop_api/server.py`（80+ HTTP 路由，1925 行）
- 关键方法（路由全部在 `_setup_app` `server.py:175-237` 集中注册）：
  - `create_turn` (POST) `server.py:476-550`（拒绝 in-flight 重复 turn，runner 写 FileStorage + emit turn 终止事件）
  - `compact_session` / `prune_session` `server.py:627-736`（`/compact` 同步调用模型摘要，触发完整 `_apply_post_compact` 流程）
  - `events_global` / `events` (SSE) `server.py:738-816`（订阅 `EventBus`，写 `data: {json}\\n\\n`，捕获 `ConnectionResetError`）
  - `upload_attachment` / `serve_attachment` `server.py:1076-1195`（multipart 落盘 + 路径白名单）
  - `terminal_ws` (WebSocket) `server.py:1720-1797`（PTY 桥接）
  - `speech_to_text` `server.py:1829-1890`（mlx-whisper 跑在 `run_in_executor`）
  - `update_config` / `save_config_json` / `save_config_yaml` `server.py:1366-1453`（先重读 disk 再 merge 写回，避免 stale 进程覆盖新编辑）
  - `switch_workspace` `server.py:1498-1554`（**仅换 workspace_root + project_id + 重载 automations**，不重建 runtime，注释明确这是 by design）

### 2.10 `app/cli.py`
- `build_parser` (`cli.py:22-56`)、`run_async` (`cli.py:452-504`)：四个子命令 `run / repl / acp / desktop`
- `_repl_loop` (`cli.py:187-413`)：11 个 slash 命令（`/quit /help /tools /approval /history /clear /model /new /compact /diff /status /memories /mcp`）

---

## 3. 关键协议与数据流

### 3.1 ACP 协议（`protocols/acp.py`）

| method | 行为 | 返回 |
| --- | --- | --- |
| `initialize` | 返回 server info | `{name, version, capabilities:{chat,tools,stream}}` |
| `ping` | 健康检查 | `{pong: true}` |
| `tools/list` | 列出已注册 tool | `{tools: [Tool.spec()...]}` |
| `chat` | 一次性非流 | `{message:{role,content}, model, usage, finish_reason}` |
| `chat_stream` | 流式 + 终稿 | `{session_id, events:[...], final:{...}}` |
| `chat_stream_chunks` | 切好块的流 | `{session_id, chunks:[{type,content/payload,seq,round,ts}, ..., {type:final,...}]}` |
| `chat_stream_open` | 把 chunks 缓存在 server 端 | `{stream_id, session_id, size}` |
| `chat_stream_next` | pull 下一块 | `{done, chunk}`（`stream_id` 找不到 → `invalid_stream`） |

**错误形状**（`acp.py:188-204`）:
```json
{"jsonrpc":"2.0","id":...,"error":{"code":-32602,"message":"...","data":{"classification":"invalid_params"}}}
```
分类字符串：`parse_error / method_not_found / invalid_params / invalid_stream`。

### 3.2 Desktop HTTP 路由（`transports/desktop_api/server.py:175-237`）

```
GET   /                                          index.html
GET   /sessions | POST /sessions                 list / create（含 ?model_name=…）
GET   /sessions/{sid}/messages[?include_dropped] LLM-visible 或全量（含 dropped 折叠）
GET   /sessions/{sid}/turns | POST /sessions/{sid}/turns
POST  /sessions/{sid}/compact | /prune | /cancel
POST  /sessions/{sid}/attachments | GET /attachments?path=…  （白名单 403）
GET   /events | /sessions/{sid}/events           SSE（per-session + global fan-out）
GET   /sessions/{sid}/{tools,plan,diff,git-branches}
POST  /sessions/{sid}/{git-checkout,revert}
PATCH /sessions/{sid}                            rename / model_name
GET/POST/PATCH/DELETE  /automations…
GET/POST  /api/permissions/{rid}/reply
POST  /sessions/{sid}/questions/reply            ask_user 解析
GET/POST /api/workspace/{git-branches,git-checkout,switch,remove,recent}
GET   /api/system/choose-folder                  osascript 弹框（macOS only）
GET   /api/files/{tree,read} | /api/proxy
GET   /ws/terminal                               PTY WebSocket
POST  /api/stt                                   mlx-whisper
GET   /api/agents | /{agent_id} | /{agent_id}/cancel
GET   /mcp-status | /config | /config/{yaml,json} | /skills | /skills/file
```

`client_max_size=25 MB`（`server.py:176`），仅 `/attachments` 实际吃大文件；`/api/proxy` 白名单 `http(s)`（`server.py:1805-1806`）。

### 3.3 Tool Call 协议 — **两套并存**

- **当前实际路径**: `OpenAIChatAdapter.chat_stream` (`provider.py:223-335`) / `AnthropicChatAdapter.chat_stream` (`provider.py:222-335`) 直接消费 SDK 的原生 `delta.tool_calls` / `content_block_start{type:tool_use}` + `input_json_delta` 累积，最后合成 `ToolCallItem`（`runtime.py:1018-1117` 用 `asyncio.gather` 并发执行）
- **README 文档化（已过时）**: `[[TOOL_CALL]]{"name":"…","arguments":{…}}[[/TOOL_CALL]]`（preferred） + `TOOL_CALL <name> <json>`（legacy）— **源码里没有 parser**（`grep` 全仓 0 个真实实现命中，只在 `tests/test_acp_stream.py:18` 和 `docs/agent-contracts.md:8-9`、`docs/collaboration-plan.md:42-43` 出现）
- **历史兼容**: `test_acp_stream.py:18` 的 mock 仍以 `[[TOOL_CALL]]` 文本走 `tool_calls=[ToolCallItem(...)]`，证明原生协议无依赖文本解析
- **副作用**: 旧 README/agent-contracts/collaboration-plan 把 `[[TOOL_CALL]]` 当 contract 写在 PR 门禁里 (`agent-contracts.md:7-9`)，但生产代码已不实现，属于文档漂移

### 3.4 `chat_stream` 完整调用链（一次 streaming 请求）

```
1. 客户端 fetch POST /sessions/{sid}/turns  body={messages:[{role:user,content}]}
2. server.create_turn (server.py:476-550)
   ├─ 拒绝 in-flight (rt_session.turn_task and not done → 429)
   ├─ 分配 CancellationToken，挂在 SessionState.cancel_token
   └─ 启动 asyncio.Task runner()
3. runner() → runtime.chat_with_events(chat_messages, session_id=sid) (runtime.py:759-763)
   └─ event_bus.clear_history(sid) → runtime.chat(...) (runtime.py:627-757)
       ├─ _get_session(sid) + load_lock → 从 FileStorage 读历史
       ├─ session.history.extend(new_messages) + _persist_message
       ├─ _run_hooks("before_turn")
       ├─ _apply_prompt (capabilities: prompt) + _maybe_apply_skill
       ├─ 构建 turn_config（session.model_name 覆盖 model.name）
       ├─ _create_adapter(turn_config) → _ADAPTER_REGISTRY[(api_type,base_url,api_key)]
       ├─ _resolve_image_paths(vision-aware, base64 data URL or text ref)
       └─ _run_model_tool_loop(rendered_messages, sid, ctx, cancel_token, …) (runtime.py:868-1207)
           ├─ _connect_mcp_if_needed (状态机 + 注册 mcp.* tool)
           ├─ [可选] /compact 分支 (runtime.py:902-923)
           └─ while round_idx < max_rounds:
               ├─ 取消检查 → yield {type:"cancelled"}
               ├─ auto-compact 钩子（prompt_tokens/context_window ≥ 0.9）
               ├─ 组装 system_prompt = base_prompt + instructions(AGENTS.md) + env_context + skill_index
               ├─ thinking_config（仅当 thinking_capable + mode≠disabled）
               ├─ model_adapter.chat_stream(working, model, system_prompt, tools, thinking_config)
               │    → SDK OpenAI/Anthropic 流式（call_with_retry 包裹）
               │    → _ThinkTagParser 兜底 (provider.py:49-117)
               ├─ 对每个 StreamDelta：yield delta/reasoning_delta/usage_update → _emit(session_id)
               ├─ tool_calls 累积完成 → yield model_response
               ├─ [if final_tool_calls]:
               │    ├─ yield {type:"tool_start"} × N
               │    ├─ asyncio.gather([_run_tool(tc) for tc in final_tool_calls])  (parallel)
               │    │    └─ _execute_tool (runtime.py:1357-1477)
               │    │        ├─ deny list + sub-agent 限制
               │    │        ├─ approval policy:
               │    │        │   ├─ full-auto / auto-edit (拒 shell)
               │    │        │   └─ suggest → perm_manager.ask → emit_permission_event → 阻塞等
               │    │        │     ↳ HTTP /api/permissions/{rid}/reply → perm_manager.reply(...)
               │    │        ├─ before_tool hook
               │    │        ├─ tool.run(args, ctx)  (含 spawn_agent / ask_user 显式关 timeout)
               │    │        └─ after_tool hook
               │    ├─ yield {type:"tool_end"} × N
               │    └─ working.append(tool_msg [or image msg])  (assistant → tool 配对)
               └─ yield {type:"round_complete"} → update_session_usage → FileStorage.update_session
4. runner finally 块：_emit turn_cancelled | turn_error | turn_end
5. UI 端 SSEPool (web/src/sse.ts:22-163) 收到 → 按 session_id dispatch 到 renderMessages
```

---

## 4. 代码模式与约定

### 4.1 异步模型
- **后端 100% async/await**：`runtime.py` 全 async，`_run_model_tool_loop` 是单核协程生成器（`async for event in …: yield`），各工具也 `async def run`。CPU/IO 边界用 `asyncio.to_thread` 离线（`read_file_tool` 同步 IO 包装；`speech_to_text` 跑 mlx-whisper）
- **一处同步混用**: ACP stdio `serve_stdio` 用 `await asyncio.to_thread(sys.stdin.readline)` (`acp.py:209`) 防阻塞事件循环
- **SSE 写入**用同步 `json.dumps`（无 thread offload），后端 CPU 不构成瓶颈；仅 `events_global` 把 `json.dumps` 也丢 `to_thread` (`server.py:808-810`) 以护全局事件循环
- **进程/线程**: 终端 WebSocket 用 `subprocess.Popen` + `pty.openpty` + `StreamReaderProtocol` (`server.py:1720-1797`)

### 4.2 错误处理
- **自定义异常**:
  - `permissions/manager.py:51-74`: `PermissionError / RejectedError / CorrectedError / DeniedError`（`DeniedError.ruleset` 暴露相关规则给 LLM）
  - `ACP errors`: JSON-RPC `-32601/-32602/-32700` + `data.classification` 字符串 (`acp.py:188-204`)
  - `ToolResult.error=True` + 前缀化错误码（`permission_denied / tool_not_found / file_not_found / mcp_call_failed / mcp_timeout / timeout / binary_file / mcp_server_not_found` …）— 这是约定的"软错误协议"，而不是抛异常
- **广泛 `try/except Exception`**：CLI REPL (`cli.py:130-131, 221-222`)、transport SSE 客户端断连 (`server.py:762, 769, 802, 812`)、FileStorage 读取 (`file_storage.py:128-129`)、image path resolver (`runtime.py:306-332`) — 多数是"已知可恢复"，但也吞掉真问题
- **`asyncio.CancelledError` 显式捕获**（runtime.py:1118-1130）补全 tool_result；transport 层 (`server.py:762`) 同

### 4.3 配置加载与校验
- **YAML schema** (`config/loader.py:9-54, 83-227`)：手写 `validate_config`（无 pydantic）— 校验 `model.* / providers[] / tool.max_rounds / memory.context_window_tokens / agents.*.hooks ∈ {before_turn,after_turn,before_tool,after_tool} / agents.*.memory ∈ {inherited, none}`，**未**校验的字段会被 `_deep_merge` 默默接受
- **layered merge**: `DEFAULT_CONFIG` ← `~/.ziva/config.yaml` ← `session_override`，`session_override` 允许 CLI 临时覆盖（`cli.py:416-424`）
- **Anthropic thinking 反向约束**: `loader.py:96-107` 强制 `thinking_budget_tokens < max_tokens`
- **多 source skill 路径**: `skill.extra_paths` 默认 `~/.ziva/skills` + `~/.agents/skills`（`loader.py:21`）

### 4.4 日志/可观测性
- `logging.getLogger(__name__)`（`runtime.py:17` 等）— 基础结构在，但 **没有结构化日志、没有 metrics/tracing**；错误多走 `logger.error/warning`
- **可观测性靠事件总线**: `Runtime._emit` + `EventBus.history` 暴露 seq/ts 化的事件流；前端 SSE 是唯一实时监控通道
- `runtime.py:1297-1299` 给每个事件打 `session_id/seq/ts`；`runtime.py:1076, 1192` `latency_ms` 计入 round 计时
- **tool call_id** 一路贯通到 FileStorage JSONL，便于回溯

### 4.5 测试组织
- 57 个测试；`conftest.py:25-37` 自动 `_reset_adapter_registry()` + 注入 `_EchoTool` —— 这意味着很多 `Runtime.create` 路径的测试其实不依赖外部网络
- 覆盖度（按文件名归类）:
  - **协议/transport**：`test_acp*.py`（6 个）、`test_desktop_*.py`（4 个）、`test_event_stream.py / test_event_metadata.py`
  - **runtime 核心**：`test_tool_loop.py / test_turn_failure.py / test_spawn_*`（4 个）、`test_session_*.py`（5 个：切换/隔离/compaction/compaction_usage）、`test_per_session_model.py`（13.8KB，单点深）
  - **capability**：`test_plugin_loading.py / test_manifest_validation.py / test_runtime_extensions.py`
  - **工具**：`test_apply_patch_tool.py / test_read_file_tool.py / test_write_file_tool.py / test_edit_tool.py / test_grep_tool.py（9.7KB）/ test_shell_tool.py / test_web_search_tool.py / test_update_plan_tool.py / test_ask_user_no_timeout.py / test_image_path_resolver.py（21.4KB，单点深）`
  - **infra**：`test_config*.py`（3 个）/ `test_retry_backoff.py / test_adapter_singleton.py / test_permission_gate.py / test_approval_config.py / test_markdown_memory.py / test_mcp_*.py`（3 个）
  - **端到端**：`test_cli_e2e.py / test_process_e2e.py / test_session_switch_e2e.py / test_desktop_compact_usage.py`
- **缺失**: 无前端单测（`web/` 没有 `*.test.ts`），无 playwright/e2e；Electron 也没有 spec 测试
- 已知坑：`README.md:64` 注明 "pytest capture plugin segfaults; run with `-p no:capture`"，是基础设施级别的限制

---

## 5. 潜在问题与改进点

### 5.1 代码异味 / 重复
- **`runtime.py` 1780 行 + 单一文件**——`Runtime` dataclass 把"orchestrator + storage helper + skill scanner + permission glue + adapter cache + env context builder"全塞进去。`runtime.py:189-364` 的 `_resolve_image_paths` 单函数 175 行带详尽注释（4 段 ASCII art），可拆成独立模块
- **同样的 ChatMessage → record 转换** 出现 3 次：`runtime.py:511-536`（`_chatmessage_to_record`）、`server.py:559-574`（`_apply_post_compact`）、`server.py:617-625`（`_append_summary_to_disk`）、`runtime.py:1721-1745`（`_persist_message`）— 应统一在 FileStorage 旁的一个 `serializers.py`
- **streaming events 字段重复构造**：`runtime.py:1065-1073` 的 `model_response` 事件和 `runtime.py:1193-1207` 的 `round_complete` 事件有重复组装逻辑
- **`server.py:1486-1496` 和 `server.py:1539-1552`** 重复读写 `recent_workspaces.json`（应抽 `RecentWorkspacesStore`）
- **shell 工具 (`plugins/tools/shell/impl.py`) 加载 `.env` 文件**（`impl.py:46-60`）会覆盖系统环境变量 → **覆盖 `PATH/HOME` 之类敏感值** 的风险，建议限制白名单/只追加不覆盖
- **`_current_model_supports_image` (runtime.py:1548-1563)** 末尾 `return True` 永远不可达（前面 for 循环的 `return` 已覆盖所有情况），是死代码
- **adapter 缓存 `_ADAPTER_REGISTRY` (`runtime.py:42, 88-123`)** 是进程级 module singleton：切换 provider / 改 API key 后要重启；web 上改 config 不会换 adapter。`_reset_adapter_registry` 标注是 test helper（`runtime.py:121-123`），但生产用同样会需要
- **chat 同步/异步两条路径相似度高**：`chat()` (`runtime.py:627-757`) 和 `chat_streaming()` (`runtime.py:765-866`) 共享 70% 逻辑（load + 钩子 + 渲染 + adapter 选择），但都各自重新实现一遍

### 5.2 性能/并发
- **`_run_model_tool_loop` 是单协程生成器** + 内部 `asyncio.gather` 并发执行 tool —— 本身没问题，但 tool 内的同步 CPU/IO（如 read_file 大文件 `asyncio.to_thread`）会占满 thread pool 默认大小（注释提到但未配置）
- **EventBus 全局队列 fan-out** (`events.py:40-45`)：每个 publish 都 `await q.put` 对所有 N 个订阅者；当 UI 既有 global 又有 per-session 队列时同一事件会写 2 遍。N 较大时 fan-out O(N) 等待
- **chat_streaming 的 stream_reset** (`runtime.py:832-866`) 是个 1 次重试——**写入 history 之前**就重置，安全；但 partial UI 状态（streaming 的 delta 已经在前端 DOM 渲染）会残留，靠前端 "drop it" 事件清理
- **`update_config` (`server.py:1366-1399`)** 写完 disk 后 `self.runtime.config = fresh` —— 但 `chat_with_events`/`_create_adapter` 在每个 turn 又重读 config 重建 adapter，所以**全局 config 改完可能要到下一轮才生效**；期间已加载的 `_ADAPTER_REGISTRY` 缓存继续服务旧 key 的请求
- **`switch_workspace` (`server.py:1498-1554`)** 不重置 `_ADAPTER_REGISTRY` 也不清理 `_sessions` 字典：跨 workspace 的旧 session 的 MCP 客户端会保留，project_id 切换后 `FileStorage.append_message` 写到新 pid，**新 session_id 在旧 pid 下找不到 → 列表/switch 时数据错位**（注释说"by design" 但风险是真实存在的）
- **SSE 单条 `ConnectionResetError` 重连** 没有 backoff jitter（web/src/sse.ts:138-150）—— 高频抖动时所有客户端同步重连雪崩（不过 SSEPool 是 per-page，问题有限）
- **`FileStorage.get_messages` 是 generator**（`file_storage.py:163-184`）但实现里 `messages = [...]` 已经一次性读出后再 `yield from` —— 命名误导，应改为函数

### 5.3 安全隐患
- **Desktop API 无 auth**：`aiohttp.web` 跑在 `127.0.0.1:4097`（`server.py:1636, cli.py:46-47`），绑定本地。**如改 host=0.0.0.0 即裸奔**——任意端可 `POST /sessions/{sid}/turns` 让 AI 执行 `shell` 工具；`--host` CLI flag 默认 `127.0.0.1` 救了一命，但 README 文档没说应保持
- **CORS 未配置**：默认 aiohttp 不发送 `Access-Control-Allow-*`，所以 web 端被同源策略保护（OK），但若有人在浏览器扩展里手动加 header 即可跨域
- **Electron `webviewTag=true`**（`electron/main.ts:100`）—— webview 跑第三方内容，CDP bridge 把 webContents 注册为 chrome-devtools-mcp target（`electron/main.ts:148-152`）—— 是设计选择，但任何注册目标都能被 MCP 操作
- **`/api/proxy?url=…` (`server.py:1799-1827`)** 限定 `http(s)://`，未做目标白名单 → 可被用来让服务器当 SSRF 出口（访问内网 / metadata 服务）
- **路径穿越** 防护是有的（`server.py:1186-1189` attachments 走 `relative_to`、`:1693-1702` files_read 走 `str.startswith(base)`、`:1699-1702` `resolve()` 后比对），但 `files_read` 用 `str.startswith` 而非 `Path.is_relative_to` 是历史 API 兼容（`os.PathLike` 在 3.9+ 才统一），建议改 `Path.is_relative_to`
- **command injection 防护 OK**: `server.py:857, 876, 883, 911, 1293, 1457` 全用 `shlex.quote`；`osascript` 调用 (`server.py:1457-1461`) 用 `create_subprocess_shell` 但参数是硬编码字符串，**无注入面**
- **`shell` 工具 (`plugins/tools/shell/impl.py:46-60`)** 自动 `source` 工作目录的 `.env`，且**用 `os.environ.copy()` 后追加**（不覆盖）。这与 `PYTHONPATH` 等保留，但是用户预期之外
- **API key 校验** 仅要求是字符串（`config/loader.py:117`），没有"非空 / 看起来像 token"的弱校验 → 用户能跑出 "dummy" 真实请求失败
- **provider.api_key 持久化在 `~/.ziva/config.yaml`**：默认文件权限未显式设 `0o600`，与本机其他用户/备份链路能读到
- **permission_request 事件** 包含 `tool / arguments`（`runtime.py:1425-1428`）—— approve SSE 在 HTTP 上明文传命令（含路径/参数）
- **`choose_folder` (`server.py:1454-1469`)** 走 `osascript` 弹原生对话框，但 prompt 字符串硬编码"Select Project Folder"，是 macOS only（Windows/Linux 上是 500）
- **`MCPClient` 命令** 来自 config，未做 allowlist → 用户 config 里 `mcp.servers[].command` 任意可写，等于代码执行（设计如此，但应有审计）
- **REPL/CLI 自动批准策略** (`cli.py:121-131`) 把 `always_session` 写进 PermissionManager，但**REPL 模式是单进程**，**desktop 模式是 `set_user_answer` 显式调用** —— 状态共享上需小心

### 5.4 测试覆盖薄弱环节
- **前端零测试**：`web/` 没有 `*.test.ts`、没有 `vitest.config` —— 所有 SSE/状态机/UI 都靠手测
- **Electron 零测试**：`electron/ziva-backend.spec` 是 PyInstaller 配置，不是测试规格
- **`_ThinkTagParser` (`openai/provider.py:49-117`)** 没有任何直接单测 —— `test_reasoning_field.py` 只测 routing，不测切分边界
- **Anthropic `_build_anthropic_messages` (`anthropic/provider.py:11-116`)** 边界条件（image url 是字符串还是 dict、是 list 还是单 block、reasoning_signature 空）有覆盖但分散
- **MCP 测试较浅**：`test_mcp_*.py` 3 个文件，`test_mcp_enum_lifecycle.py` 看名字像 enum lifecycle 但仅 1.9KB；缺 on-demand reconnect / cancel scope / multi-server 故障转移
- **`web/src/main.ts` 294KB** —— 一坨 UI 逻辑（drag/drop、tab、terminal、image lightbox、slash command、reasoning card、automation UI、mcp ui…） 几乎无测试覆盖
- **adapter cache 单例行为** (`runtime.py:88-123`) 的实际命中/失效路径无单测；`test_adapter_singleton.py` 仅 3.3KB
- **stream_reset 重试路径** (`runtime.py:832-866`) 只有 `test_retry_backoff.py`（3.7KB）覆盖网络层，端到端的"部分 UI 状态被丢弃"没有 contract test
- **`switch_workspace` (`server.py:1498-1554`)** 是高风险路径，零直接单测；`test_per_session_model.py` 14KB 主要是 model 切换
- **`/sessions/{sid}/attachments` + `/attachments` 路径白名单** (`server.py:1076-1195`) 没有路径穿越 negative test

### 5.5 文档缺失/不准确
- **README.md:25-35** 把 `[[TOOL_CALL]]` 协议写为契约（甚至说"Preferred structured format"），但**生产代码不解析**该格式；应当删掉或标注"已废弃，native function calling only"
- **README.md:20-22** 列了 "Session history endpoints for desktop" 但**实际暴露的 endpoint 远超**这些（自动化、文件树、终端 WS、STT、agents… 都没在 README 出现）
- **docs/agent-contracts.md:7-9** 仍在 PR acceptance gate 引用 `[[TOOL_CALL]]` —— 与实际代码冲突
- **docs/collaboration-plan.md:42-43** 同上
- **pyproject.toml:5-8** `version = "0.1.0"` 仍是 `0.1.0`；`src/ziva_runtime/__init__.py:3` 也是 `0.1.0`；`acp.py:34` 返回 `"0.2.0"`（三个版本号不一致）
- **pyproject.toml:10-18** 列了 `prompt_toolkit` 但 `mcp>=1.0.0` 没写 `mcp[cli]` 子依赖，stdio 走 subprocess 时可能缺 `uv`/`npx`
- **没有 CHANGELOG / API 文档**；ACP 协议 5 个 chat 变体只在 `protocols/acp.py:44-51` 自描述；Desktop API 全靠读 server.py 才知道
- **README "Commands" 段落**（README.md:50-61）只有 `run / acp / desktop`，缺 `repl`（`cli.py:49-55, 470-475` 有）
- **`REPL` slash 命令**（`/mcp`、`/memories` 等）只在 `cli.py:280-380` 实现，README 没列
- **README.md:64** "pytest capture plugin segfaults" 是无解的 workaround，没在 pyproject.toml/CI 文档里固化（新人踩坑会重复）

---

## 6. 总结

### 6.1 整体质量判断
Ziva 是一个**功能相当完整、架构清晰的中等成熟度**项目：单 Python 入口 (`runtime.py`) + 4 类适配器（OpenAI/Anthropic/MCP/retry）+ 30+ 个 plugin tool + 双协议（ACP stdio + Desktop HTTP/SSE）+ Electron 包装 + Web UI，前后端在 6 个月内演进到 production-ready 的子集（已经有 PyInstaller 打包、CWD-anchor 路径安全、orphan tool_call 修复、auto-compact、session model override）。代码可读性高，**docstring/注释密度大且诚实**（多处明确"by design" 解释取舍），错误信息用一致的 `Error: <code>\n<msg>` 协议；测试 57 个覆盖了核心路径（compaction / session 模型切换 / 多 session 隔离 / ACP 流 / 工具协议）。但**协议契约与文档漂移明显**（`[[TOOL_CALL]]` 文档化但未实现、`README` 列出的 endpoint 严重不全、三个版本号不一致）、**前端/Electron 零测试**、**Adapter 单例缓存与 workspace 切换的耦合** 未解，存在生产风险（API key 改后需重启、workspace 切换不清 sessions）。

### 6.2 Top 3 优先改进项

1. **删除/更新 `[[TOOL_CALL]]` 文档**：把 `README.md:25-35`、`docs/agent-contracts.md:7-9`、`docs/collaboration-plan.md:42-43` 三处契约同步成"native function calling only"；同时把版本号统一（`acp.py:34` 改 0.1.0）。**影响**：消除 PR gate 的误导，避免后人实现根本不会被用到的 parser
2. **解开 adapter 单例 + workspace 切换耦合**：
   - `_ADAPTER_REGISTRY` 改成按 `(api_type, base_url, api_key, model_name)` 缓存，保存最后使用时间
   - `switch_workspace` (`server.py:1498-1554`) 显式调用 `await self.runtime.shutdown()` 断开旧 session 的 MCP + 清 `_sessions`，新 workspace 真正重建
   - 加单测：`test_adapter_singleton.py` 扩到覆盖"key 变更 → 自动重建"和"workspace 切换 → 旧 session 清理"
3. **补前端 + 关键路径单测**：
   - 给 `web/src/sse.ts`（断线重连、reconnect callback）、`_ThinkTagParser`（`openai/provider.py:49-117` 各种边界）、`files_read` 路径白名单（`server.py:1691-1718`）、`/api/proxy` SSRF 防护、switch_workspace 行为 各加 1 个 focused test
   - 给 web 加 vitest + 1 个 component smoke test（SSEPool + API client 即可）
   - **影响**：覆盖当前的两个最大盲区（无前端测试 + 无安全 negative test）

---

**说明**: 本报告全程使用 `read_file / grep / list / glob` 等只读工具，未修改任何业务代码。`write_file` 工具在此 sub-agent 中不可用，因此报告未落盘到 `.tmp/code-analysis-<ts>.md`（仅以 stdout 形式给出）。如需落盘，请在主 agent 侧执行 `cat` 抓回后保存。
