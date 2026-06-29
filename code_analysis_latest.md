# Ziva 代码全量分析报告（code_analysis_latest.md）

> 分析对象：`/Users/wangxinxin/code/ziva`
> 分析口径：仅参考当前 on-disk 代码事实，不复用仓库中已有的 `code_analysis.md / code_analysis_fullstack.md / code_analysis_report.md` 任何结论。
> 引用规范：所有结论后跟 `file:line` 行号引用。

---

## 0. TL;DR

- **项目是什么**：Ziva 是一个类 Codex 的 agent 运行时，由三部分组成：Python 后端 `src/ziva_runtime/`（aiohttp + 自研轮子）、Electron 桌面壳 `electron/`（主进程 + CDP bridge）、Vite/TS 单页前端 `web/`（Vite 5 + xterm.js + KaTeX），三条入口 `run`（CLI 单轮）、`acp serve`（JSON-RPC over stdio）、`desktop serve`（HTTP + SSE + 静态 UI，默认 `127.0.0.1:4097`）。
- **技术栈核心**：Python 3.10+ / aiohttp 3.9 / openai 1.x + anthropic 0.30 / mcp 1.x / PyYAML / rich / prompt_toolkit；Electron 35 + ws 8 + electron-store；前端 Vite 5 + marked + Prism + KaTeX + xterm。包管理：Python 用 `pyproject.toml`（hatchling 打包，`requires-python >= 3.10`），前端 npm。运行时 UI 资源用 PyInstaller 把 Vite 产物打进 `src/ziva_runtime/transports/desktop_api/static/`，由后端以 `web.Application` 静态服务（`src/ziva_runtime/transports/desktop_api/server.py:230-237`）。
- **核心抽象**：统一 `Runtime`（1805 行）管一切；`CapabilityRegistry`（34 行）注册 5 类扩展点（tool / skill / hook / memory / prompt）；`EventBus`（62 行）扇出 per-session + global SSE；`FileStorage`（292 行）JSONL 消息 + JSON 元数据 + fcntl 文件锁；`PermissionManager`（295 行）三档 approval + wildcard 规则；`MCPServer{Stdio,Sse,StreamableHttp}`（228 行）薄包装 `mcp` SDK。
- **成熟度**：**内部可用**级。功能完整度（CLI / ACP / Desktop / MCP / 工具 15 个 / 钩子 4 个 / 内存 2 个 / Sub-agent / Compact / 权限 / 自动化 / 语音）已经远远超过"demo"门槛；测试 54 个文件覆盖 ACP/协议/权限/会话隔离/并发等关键路径；但多端间存在不少"运行时硬编码默认值"（provider 缓存、SessionStore 双状态、ad-hoc 锁）和少量遗留"调试 print / TODO 注释"。

---

## 1. 架构总览

### 1.1 三条入口链路

| 入口 | 命令 | 协议 | 数据格式 | 入口文件 |
|------|------|------|----------|----------|
| CLI 单轮 | `python -m ziva_runtime.app.cli run "hi"` | 同步 stdio | `ChatMessage` → `ChatResult` | `src/ziva_runtime/app/cli.py:455-468` |
| ACP 服务 | `python -m ziva_runtime.app.cli acp serve` | JSON-RPC 2.0 over stdio | 请求/响应（带 `chat_stream` / `chat_stream_chunks` / `chat_stream_open` / `chat_stream_next` 流式变体） | `src/ziva_runtime/app/cli.py:477-480` + `src/ziva_runtime/protocols/acp.py:207-227` |
| Desktop API | `python -m ziva_runtime.app.cli desktop serve --port 4097` | aiohttp HTTP + SSE + WS | JSON 响应；`text/event-stream` 事件总线 | `src/ziva_runtime/app/cli.py:482-502` + `src/ziva_runtime/transports/desktop_api/server.py:175-237` |

- CLI `run` 走 `Runtime.chat()` 同步流，输出最终 assistant content；带 `--no-stream` 走 `Runtime.chat()`（非流式）。`src/ziva_runtime/app/cli.py:119-148` 的 `_run_streaming` 在非交互模式下注册"自动批准"回调。
- ACP stdio 是一行一个 JSON-RPC 请求；`serve_stdio` 阻塞在 `sys.stdin.readline()`（`protocols/acp.py:209`），`chat_stream_chunks` 把内存里的事件拆成 "delta/tool_start/tool_end/final" 数组（`acp.py:137-177`）。
- Desktop `aiohttp` 应用注册了 40+ 路由（`server.py:177-237`），其中：
  - 静态：`/` 返回 `index.html`（`server.py:359-364`），`/assets/*` 是构建产物。
  - SSE：`/events`（全局广播，`server.py:790-831`）和 `/sessions/{sid}/events`（per-session，向后兼容）。
  - WebSocket：`/ws/terminal` 是 xterm.js 走 `pty.openpty()` 的真实 shell（`server.py:1758-1835`）。
  - 工具/状态：`/mcp-status`、`/skills`、`/config/yaml`、`/api/agents/{agent_id}` 等。

### 1.2 Runtime 内部组成

`src/ziva_runtime/runtime.py:398-411` 的 `Runtime` dataclass 拥有 5 个核心组件：

| 字段 | 类型 | 职责 | 引用 |
|------|------|------|------|
| `config` | `Dict[str, Any]` | 合并后的有效配置（default + `~/.ziva/config.yaml` + session_override），所有适配器/钩子读它 | `runtime.py:611-642` |
| `registry` | `CapabilityRegistry` | 5 种能力（tool/skill/hook/memory/prompt）的中央注册表 | `runtime.py:612` + `capabilities/registries.py:15-34` |
| `event_bus` | `EventBus` | 每个事件有自增 `seq`、`ts`、原 `payload`；`subscribe(sid)` 给单 session，`subscribe_global()` 给前端全局广播 | `capabilities/events.py:10-62` |
| `workspace_root` | `Path` | 当前工作区根 | `runtime.py:633` |
| `_sessions` | `Dict[str, SessionState]` | 进程内的 session 缓存，含 `mcp_status`、`pending_questions`、`plan` 等 | `runtime.py:443-464` + `shared_types.py:138-156` |

模块间依赖图（精简）：

```
app.cli ───────┬─► runtime (Runtime) ─┬─► adapters.openai ─┐
               │                      ├─► adapters.anthropic
               │                      ├─► adapters.mcp (client/server)
               │                      ├─► adapters.retry
               │                      ├─► capabilities.registries ─► events
               │                      ├─► config.loader ─► instructions
               │                      ├─► permissions.manager
               │                      ├─► plugins.loader ─► manifest
               │                      ├─► session.compaction
               │                      └─► storage.file_storage
               ├─► protocols.acp ────► runtime
               └─► transports.desktop_api.server ─┬─► runtime
                                                   ├─► config.loader
                                                   ├─► permissions
                                                   └─► storage.file_storage
```

- **关键观察**：`adapters.openai_agents` 在 `electron/ziva-backend.spec` 的 `hiddenimports`（`electron/ziva-backend.spec`）里仍列出，但代码里 `adapters/openai/provider.py` 已经被命名为 `OpenAIAgentsAdapter = OpenAIChatAdapter`（`adapters/openai/provider.py:365`）；也就是说 spec 落后于代码。
- **AppState/Store 隔离**：Python 端 `Runtime` 是单例，状态全在 `_sessions` dict（`runtime.py:404`），并通过 `SessionState.load_lock`（`shared_types.py:153`）做并发保护。前端状态在 `web/src/state.ts:79-102` 的 `Store` 类里，per-session 用 `runningSessions[sid]` 等 key 分散（避免 active-session 假设）。

### 1.3 模块依赖关系（精确 import 摘录）

下面三段是从 `runtime.py` 顶部 (line 15-39) 直接抄来的关键导入：

```
from ziva_runtime.adapters.openai.provider import ModelAdapter, OpenAIChatAdapter
from ziva_runtime.capabilities.events import EventBus
from ziva_runtime.capabilities.registries import CapabilityRegistry
from ziva_runtime.config.instructions import load_layered_instructions
from ziva_runtime.config.loader import load_effective_config
from ziva_runtime.permissions import DeniedError, PermissionManager, from_config,
                                   get_permission_manager, RejectedError
from ziva_runtime.plugins.loader import load_plugins
from ziva_runtime.session.compaction import compact_messages, _llm_context,
                              compose_post_compact_on_disk, estimate_tokens,
                              find_last_summary_idx, find_cutoff_in_llm_visible
from ziva_runtime.shared_types import ApprovalRequest, ApprovalPolicy, CancellationToken,
                              ChatMessage, ChatResult, MCPConnectStatus, RuntimeContext,
                              SessionState, ToolCall, ToolCallItem, ToolResult
from ziva_runtime.storage.file_storage import FileStorage, _project_hash
```

`transports/desktop_api/server.py:22-26` 多了：

```
from ziva_runtime.config.loader import _deep_merge   # 注意：是 _ 前缀的"私有"符号
from ziva_runtime.permissions import get_permission_manager
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import CancellationToken, ChatMessage, ChatResult, ToolCallItem
from ziva_runtime.storage.file_storage import FileStorage, _project_hash
```

**结论**：跨层调用走 `Runtime` + `FileStorage` + `PermissionManager` 三个"facade"，没有出现反向 import（UI 层不向 adapter 渗透）。`compile()` 干净，但 `transports/desktop_api/server.py` 直接 import 了 `_deep_merge`、`_project_hash` 等"下划线"开头函数——见第 7 节建议。

---

## 2. 关键数据流

### 2.1 一次 chat 的完整生命周期（Desktop 入口为例）

1. **前端**：`web/src/api.ts:118-120` 的 `createTurn(sid, content)` 发 `POST /sessions/{sid}/turns` 携带 `{messages: [{role, content}]}`。
2. **aiohttp 接收**：`transports/desktop_api/server.py:476-562` 的 `create_turn` 做：
   - 拒 429 若 `turn_task` 未结束（`server.py:487-490`）。
   - 准备 `CancellationToken`、`asyncio.create_task(runner())`（`server.py:505-560`）。
   - 把消息写进 `_loaded_sessions[sid].messages`（**注**：in-memory store 与 `FileStorage` 走两套路径，见第 7 节"双写不一致"）。
3. **runner**：`server.py:509-534` 调用 `Runtime.chat_with_events`，最终落 `Runtime.chat_streaming`（`runtime.py:782-883`）。
4. **Runtime 预处理**（`runtime.py:791-836`）：
   - `load_lock` 内读盘加载历史（`runtime.py:796-821`）。
   - `_sanitize_orphaned_tool_calls` 修补取消留下的孤儿 tool_call（`runtime.py:1649-1709`，`runtime.py:820` 处调用）。
   - `_apply_prompt` 让 prompt provider 改写最后一条 user message（`runtime.py:1511-1520`）。
   - `_maybe_apply_skill` 用 `Skill.match` 嗅探并执行技能（`runtime.py:1612-1618`）。
5. **Model↔Tool 循环**：`runtime.py:885-1235` 的 `_run_model_tool_loop`：
   - 起始轮做"自动 compact 钩子"（`runtime.py:951-987`）。
   - `model_cfg` + `turn_adapter` 在 turn 入口快照（`runtime.py:843-848`），避免 mid-turn 模型切换。
   - `_build_tools_param` 从 registry 列出 tool，排除 sub-agent 不可见的 `spawn_agent/get_agent_result/cancel_agent`（`runtime.py:1298-1321`）。
   - `model_adapter.chat_stream(...)` → 消费 `StreamDelta` 序列，对外 yield `delta/reasoning_delta/usage_update`，并最终发出 `model_response`（`runtime.py:1053-1117`）。
6. **工具执行**：
   - 有 tool_calls → `asyncio.gather(*[_run_tool(tc)...])` 并行执行（`runtime.py:1144-1158`），取消时合成 `[cancelled]` 占位（`runtime.py:1146-1158`）。
   - 每个工具通过 `_execute_tool` → `PermissionManager.ask` → `tool.run`（`runtime.py:1385-1509`）。
7. **持久化**：
   - 写消息：`FileStorage.append_message` 追加到 `~/.ziva/sessions/<pid>/messages/<sid>.jsonl`（`runtime.py:1770`、`file_storage.py:158-165`）。
   - 写 usage：`update_session_usage` 把 `last_usage` 塞到 session.json（`runtime.py:1776-1781`）。
8. **事件扇出**：`_emit(sid, event)` 自动加 `seq/ts/session_id`，推到 `EventBus` 的 per-session 队列和 global 队列（`runtime.py:1323-1329`）。
9. **前端**：`web/src/sse.ts:22-163` 的 `SSEPool` 单一全局连接，复用同一个 `/events` 流；`web/src/main.ts:3833-3917` 的 `routeSSEEvent` 按 `session_id` 分发到 active/split/background 三个目的地。

### 2.2 Tool call 协议

- **首选结构化格式**：`[[TOOL_CALL]]{"name":"echo","arguments":{"text":"hello"}}[[/TOOL_CALL]]`（`README.md:28`、`src/ziva_runtime/app/cli.py` 注释）。
- **代码事实**：所有 adapter 都用 **OpenAI/Anthropic 原生 `tool_calls` 字段**（`adapters/openai/provider.py:33-40` 用 `function` 包装；`adapters/anthropic/provider.py:71-78` 用 `tool_use` 内容块），**不解析 `[[TOOL_CALL]]` 字符串**。旧 plain-text 协议在 `_resolve_image_paths` 附近没有解析代码，**仅在 `scripts/smoke_test.py:23-26` 的 fake adapter 里演示**。
- **优先级**：adapter 优先使用 provider 返回的原生 `tool_calls`；fallback 顺序是 "原生 → tool_call chunk 累加（`provider.py:312-336`） → 占位 error"。`runtime.py:1163-1193` 显式区分 `tool_not_found` 和一般错误：
  ```
  is_not_found = tool_output.error and "tool_not_found" in tool_output.text
  ```
- **参数传递**：`tool_calls_acc[idx]["arguments"] += partial_json`（`provider.py:323`），所有增量累加后 `json.loads`；`json.JSONDecodeError` 走 `{"raw": <str>}` 回退（`provider.py:331-335`、`anthropic/provider.py:307-311`）。
- **结果回填**：每个 tool 的 `ToolResult.text` 拼到下一轮的 `role="tool"` 消息里（`runtime.py:1141`、`runtime.py:1209-1212`）。`reasoning_content` / `reasoning_signature` 通过 `runtime.py:1111-1114` 同时绑定到 assistant message，使 Anthropic 多轮 thinking 链路不断（`adapters/anthropic/provider.py:48-58`）。

### 2.3 MCP 客户端如何接入 / 能力如何暴露

- **配置解析**：`adapters/mcp/client.py:438-465` 的 `parse_mcp_config` 同时识别三种格式：
  1. `mcp.servers: [list]`
  2. `mcp.servers: {name: dict}`（dict 形式）
  3. 兜底 `mcpServers` / `mcp_servers`（Cline/Claude 风格）
- **传输**：`MCPServer{Stdio,Sse,StreamableHttp}` 三选一（`client.py:298-331`）。
- **per-session 连接**：`runtime.py:1237-1296` 的 `_connect_mcp_if_needed` 用 `MCPConnectStatus` 五态机（`shared_types.py:12-34`）防止同 session 重复连接：CONNECTED 跳过；CONNECTING 阻塞等；NO_CONFIG / FAILED 接受重试。
- **能力暴露**：每个 MCP 工具包成 `MCPToolWrapper`（`client.py:31-100`），通过 `runtime.py:1267-1280` 的 `self.registry.register(capability_id="mcp.<name>", kind="tool", ...)` 注册到全局。
- **路由**：工具调用时 `MCPToolWrapper.run` 用 `ctx.metadata["_runtime"]` 拿回 runtime，再 `runtime._get_session(ctx.session_id)` 拿 session 级别的 MCP 客户端（`client.py:53-90`），避免跨 session 串台。
- **结果解析**：`mcp_call_result_to_tool_result`（`client.py:237-267`）把 MCP 的 `content: [text/image/audio/resource/...]` 统一转成 `ToolResult`，image 自动转 `data:` URL。

---

## 3. 核心机制详解

### 3.1 Model ↔ Tool 循环的 guardrails

- **最大步数**：`tool.max_rounds`（默认 0=无限；`config/loader.py:18`）。`runtime.py:904-905` 解析，`runtime.py:943` 控制循环。`runtime.py:1226-1235` 在耗尽时发 `model_response.finish_reason="max_rounds"` 兜底。
- **取消**：`runtime.py:945-948` 在循环顶部查 `cancellation_token.is_cancelled`；`runtime.py:1054-1057` 在流中也检查；`runtime.py:1146-1158` 在工具并发阶段 catch `CancelledError`，为每个未完成的 `tool_call_id` 合成 `role="tool", content="[cancelled]"` 占位。
- **重试**：
  - **HTTP 层**：`adapters/retry.py:69-86` 的 `call_with_retry` 用 equal-jitter 指数回退 + `Retry-After` 头处理（最大 3 次）。`adapters/retry.py:17` 的 `MAX_RETRIES=2`、`RETRYABLE_STATUS = {429,500,502,503,504,529}`、`_RETRYABLE_CONTENT_MARKERS = ("1027","new_sensitive",...)` 处理"敏感内容"重试。
  - **Turn 层**：`runtime.py:849-881` 的 for 循环最多 2 次；第二次之前 yield `stream_reset` 事件（`runtime.py:873-879`），前端 `main.ts:4118-4126` 的 `resetStreamingState(sid)` 把已绘制的 partial 清掉。
- **doom-loop 防护**：`plugins/hooks/doom_loop/impl.py:21-32` 用 `(tool, hash(args))` 做计数器，3 次后注入 `<reminder>...</reminder>` 到结果末尾。
- **truncation 防护**：`plugins/hooks/truncation/impl.py:6-14` 为 shell/grep/web_fetch 各自设上限（30k/20k/50k），超过部分写入 `tmp/tool_output_*.txt` 并附"详见文件"提示。
- **未知工具**：`runtime.py:1183-1193` 见到 `tool_not_found` 直接终止当前轮 + 发 `model_response.finish_reason="tool_not_found"`，避免无限重试缺失工具。
- **错误流**：`adapters/retry.py:31-42` 走 `status_code` 属性 → HTTP 文本 → 关键词三重匹配；`_RETRYABLE_CONTENT_MARKERS` 兜底"内容敏感"类（实测对 DeepSeek/部分 Anthropic 代理有效）。

### 3.2 自动 compact（Context Compaction）

- **触发条件**：`runtime.py:955-987` 读 `last_usage.prompt_tokens`（`runtime.py:515-526` 的 `_read_last_usage`），当 `prompt_tokens / context_window >= 0.9` 触发；阈值常量 `AUTO_COMPACT_THRESHOLD = 0.9`（`runtime.py:387`）。
- **不触发场景**：`runtime.py:965-966` 检测 `len(asst_indices) < AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS=5`（`runtime.py:395`）就静默跳过；理由注释清晰："compact_messages needs ≥ K asst messages to do a meaningful split"。
- **压缩算法**：`session/compaction.py:235-303` 的 `compact_messages`：
  1. 取最后 K 个 assistant message 作为分界点（`session/compaction.py:278-284`）。
  2. 前半部分用 `CompactAgent` 让 LLM 跑一个总结（`session/compaction.py:30-44` 的 `run`，prompt 是中文模板 `session/compaction.py:46-71`）。
  3. 后半部分原样保留。
  4. 失败兜底用 `_simple_compact_split`（`session/compaction.py:306-327`），把每条 message 截 200 字符。
- **上下文重建**：
  - 内存侧：`SessionState.history` 直接换成新 working 集（`runtime.py:590`）。
  - 磁盘侧：`compose_post_compact_on_disk` 拼接 `[preserved_old, new_summary, ...to_keep]`（`session/compaction.py:330-366`）；旧 summary 之前的所有内容继续留在磁盘供"展开"按钮读取。
  - `last_usage` 刷新为新 LLM-visible 视图的 `estimate_tokens`，避免下一轮立即再次 compact（`runtime.py:595-601`）。
- **手动 `/compact`**：`runtime.py:919-940` 在 turn 入口用 `compact_messages` + 自定义 `"Manual compact triggered by /compact"` note；HTTP `/sessions/{sid}/compact` 走同样的函数（`server.py:661-748`）。
- **`/prune`**（无 LLM 调用）：`session/compaction.py:98-142` 把"倒数第 K 个 user 消息"之前的所有 `role="tool"` 内容压成 `[old tool result pruned — tool: <name>]`，保留 user/assistant 原样。`server.py:639-659` 暴露为 HTTP 端点。
- **LLM-visible 视图**：`_llm_context`（`session/compaction.py:197-213`）只返回"最后一个 summary 及其后"；磁盘里 `[msg1, msg2, summary, msg3, msg4, summary2, msg5]` 中 LLM 只见 `[summary2, msg5]`。

### 3.3 权限系统

- **三档 policy**（`permissions/manager.py:17-27`）：
  - `full-auto`：直接放行（`runtime.py:1414-1415`）。
  - `auto-edit`：禁止 `shell:execute`（`runtime.py:1416-1418`）。
  - `suggest`：走 `PermissionManager.ask`（`runtime.py:1419-1474`）。
- **规则评估顺序**（`permissions/manager.py:150`）：`ruleset`（配置文件）→ `session_rules`（本 session 临时）→ `state["approved"]`（全局永久）；`evaluate` 反向遍历找第一个匹配（`permissions/manager.py:77-82`），新规则覆盖旧。
- **wildcard 匹配**：`permissions/wildcard.py:8-30` 自己写一个 `re.escape + * → .*` 转换器，Windows 下默认大小写不敏感。
- **用户响应**：
  - CLI：`cli.py:208-218` 的 `_on_pending` 用 `input()` 接收 `(y)es / (a)lways / (s)ession / [n]o`。
  - Web：前端 `appendApprovalCard` → `replyPermission`（`api.ts:203-205`）→ `POST /api/permissions/{request_id}/reply`（`server.py:1058-1068`）。
- **持久化**：
  - `once` / `reject`：仅影响当前 future。
  - `always`：写入 `state["approved"]`（`permissions/manager.py:228-237`），进程内永久。
  - `always_session`：写入 `state["session_approved"][sessionID]`（`permissions/manager.py:238-264`），session 维度。同时联动放行其他 pending requests（`permissions/manager.py:251-264`）。
- **错误抛出**：`DeniedError`（规则 deny）、`RejectedError`（用户拒绝）、`CorrectedError`（用户拒绝并给 feedback）—— 三种都被 `_execute_tool` 翻译为 `ToolResult(text="Error: ...", error=True)` 回给 LLM（`runtime.py:1469-1474`）。
- **窗口/事件触发**：`runtime.py:1449-1456` 通过 `event_callback` 把请求广播到 SSE 通道（`type: "permission_request"`），前端渲染为 `appendApprovalCard`（`main.ts:4256-4264`）。

### 3.4 五个扩展点（注册 / 调用）

| 扩展点 | 注册路径 | 触发位置 | 备注 |
|--------|----------|----------|------|
| `tool` | `plugins/loader.py:48-73` 的 `load_plugins` → `registry.register(capability_id="tool.<name>", kind="tool", ...)` | `runtime._build_tools_param`（`runtime.py:1298-1321`）列出；`runtime._execute_tool`（`runtime.py:1385-1509`）执行 | MCP 工具在 `runtime.py:1267-1280` 动态追加 |
| `skill` | `plugins/loader.py:48-73` 同样路径；`id` 形如 `skill.<name>` | `runtime._maybe_apply_skill`（`runtime.py:1612-1618`）；UI 在 `_run_model_tool_loop` 起始把 skill index 拼进 system prompt（`runtime.py:995-1003`） | 实际只 sniff 最后一个 user message；不是真正的 LLM 嗅探 |
| `hook` | 同上；`id` 形如 `hook.<name>` | `runtime._run_hooks`（`runtime.py:1627-1638`）按 `event_name` + `matcher` 过滤；事件源 `before_turn / after_turn / before_tool / after_tool`（`runtime.py:680, 772, 1483, 1506`） | 钩子可改写 payload（如 `after_tool` 截断、注入 reminder） |
| `memory` | 同上；`id` 形如 `memory.<name>` | `runtime._store_memory`（`runtime.py:1620-1625`）在 `chat()` 末尾 `await store.put("last_turn", ...)` | 选 backend 由 `config.memory.backend` 决定；`memory.markdown` 默认 `enabled_by_default=false`（`plugins/memory/markdown/manifest.yaml`），`memory.inmemory` 总是启用 |
| `prompt` | 同上；`id` 形如 `prompt.<name>` | `runtime._apply_prompt`（`runtime.py:1511-1520`）取第一个 prompt provider 改写最后一条 user message | `template: "default"` |

- **钩子的全局 ↔ sub-agent 隔离**：sub-agent 的 `agent.hooks` 列表控制其能看到哪些 hook 类型（`config/loader.py:223-237`），默认空=继承全部。
- **钩子的 `matcher`**：`fnmatch.fnmatch` 通配符匹配工具名（`runtime.py:1628-1636`）；不匹配 tool 事件的钩子被跳过。

### 3.5 ACP 协议

- **method 清单**（`protocols/acp.py:24-52`）：
  | method | 用途 | 实现 |
  |--------|------|------|
  | `initialize` | 握手 | `_ok(name="ziva-acp", version="0.2.0", capabilities={chat,tools,stream})`（`acp.py:29-37`） |
  | `ping` | 健康检查 | `{"pong": true}`（`acp.py:38-39`） |
  | `tools/list` | 工具清单 | `runtime.list_tools()`（`acp.py:40-41`） |
  | `chat` | 同步单轮 | `runtime.chat(messages, session_id=...)` → 最终 result（`acp.py:54-68`） |
  | `chat_stream` | 一次性返回完整事件时间线 + final | `runtime.chat_with_events(...)`（`acp.py:70-89`） |
  | `chat_stream_chunks` | 增量 chunk 列表 + final | `_build_chunks` 按 `token_granularity`（`char`/`word`）切分（`acp.py:91-98, 137-187`） |
  | `chat_stream_open` | 开启一个流缓冲 | 返回 `stream_id`（`acp.py:100-108`） |
  | `chat_stream_next` | 拉取下一个 chunk | 游标自增；`done=true` 时 `del self._streams[stream_id]`（`acp.py:110-129`） |
- **错误格式**（`acp.py:192-204`）：`{jsonrpc:"2.0", id, error:{code, message, data:{classification}}}`；常用 classification：`parse_error`（`-32700`）、`invalid_params`（`-32602`）、`method_not_found`（`-32601`）、`invalid_stream`（`chat_stream_next` 找不到 stream）。
- **流事件**：`chat_stream` 返回 `events: [...]` 每个 event 含 `seq/ts/round/type/...`；`chat_stream_chunks` 的 `chunk: {type, content/payload, seq, round, ts}`，`type ∈ {delta, tool_start, tool_end, final}`。
- **stdio 读取**：`acp.py:209` 用 `asyncio.to_thread(sys.stdin.readline)` 避免阻塞 event loop；`json.JSONDecodeError` 走 `-32700` 路径。

### 3.6 Desktop API

- **路由全景**（`server.py:177-237`）：
  - 会话：`GET/POST /sessions`、`GET /sessions/{sid}/messages`、`GET /sessions/{sid}/turns`、`POST /sessions/{sid}/turns`、`POST /sessions/{sid}/compact`、`POST /sessions/{sid}/prune`、`POST /sessions/{sid}/cancel`、`POST /sessions/{sid}/attachments`、`GET /attachments`、`PATCH /sessions/{sid}`、`DELETE /sessions/{sid}`。
  - 工作区：`POST /api/workspace/switch`、`POST /api/workspace/remove`、`GET /api/workspace/recent`、`GET /api/system/choose-folder`。
  - 面板：`GET /api/files/tree`、`GET /api/files/read`、`GET /ws/terminal`、`GET /api/proxy`、`POST /api/stt`。
  - 配置：`GET/PATCH /config`、`GET/PUT /config/yaml`、`GET/PUT /config/json`。
  - MCP：`GET /mcp-status`。
  - Skills：`GET /skills`、`GET /skills/file`。
  - Sub-agent：`GET /api/agents`、`GET /api/agents/{agent_id}`、`POST /api/agents/{agent_id}/cancel`。
  - 权限：`POST /api/permissions/{request_id}/reply`、`POST /sessions/{sid}/questions/reply`。
  - Git：`GET /sessions/{sid}/git-branches`、`POST /sessions/{sid}/git-checkout`、`GET /api/workspace/git-branches`、`POST /api/workspace/git-checkout`。
  - Diff：`GET /sessions/{sid}/diff`、`POST /sessions/{sid}/revert`。
  - 状态：`GET /status`。
- **SSE 事件格式**（`runtime._emit` + `server.py:790-831`）：每条事件固定字段 `{session_id, seq, ts}` + 原 payload；前端按 `data: <json>\n\n` 协议解析（`sse.ts:118-127`）。
- **UI shell 加载**：
  - 静态资源由 vite build 写入 `src/ziva_runtime/transports/desktop_api/static/`，后端 `GET /` 读 `index.html` 返回（`server.py:359-364`），`/assets/*` 走 `web.Application.add_static`（`server.py:235`）。
  - dev 模式下前端用 `vite.config.ts:13-21` 的 proxy 把 `/sessions /automations /status /config /api` 转到 `127.0.0.1:4097`，`/` 走 vite dev server 自身。
- **认证**：**没有任何鉴权**（读完全部路由无 `Authorization` 头处理）。desktop API 默认 `127.0.0.1` 绑定，host 防火墙是唯一防线；`pyproject.toml` 没有 auth 相关依赖。

---

## 4. 前端与桌面壳

### 4.1 Electron 主进程（`electron/main.ts`）

- **职责**：
  - 启动 Python 子进程（`main.ts:14-85` 的 `startPythonBackend`），命令由 `getBackendCommand` 决定：dev 走 `python3 -m ziva_runtime desktop serve --port 4097`（`main.ts:18-25`），packaged 走 PyInstaller 产物 `ziva-backend`（`main.ts:28-34`）。
  - 启动 `BrowserWindow` 指向 `http://127.0.0.1:4097`（`main.ts:104`）。
  - 启动 CDP bridge（`main.ts:121-133`），让 `chrome-devtools-mcp` 可以 `--browser-url=http://127.0.0.1:9222` 接入。
  - 桥接 IPC：`get-backend-url / is-electron / get-cdp-port / register-cdp-page / unregister-cdp-page`（`main.ts:137-158`）。
- **Backend "startup" 探测**：子进程 stdout/stderr 中匹配 `"Running on"` 或 `"started"`（`main.ts:50-65`），否则 5 秒兜底 resolve（`main.ts:78-83`）。

### 4.2 Preload（`electron/preload.ts`）

- **暴露的 API**（`preload.ts:3-13`）：`getBackendUrl / isElectron / getCdpPort / registerCdpPage / unregisterCdpPage`。
- **安全模型**：`contextIsolation: true, nodeIntegration: false, webviewTag: true`（`main.ts:96-101`）。所有 node 能力走 IPC。

### 4.3 CDP bridge（`electron/cdp-bridge.ts`）

- **目的**：让 `chrome-devtools-mcp` 能用 Puppeteer 驱动"Agent Browser" webview，但不会污染主 UI。
- **协议实现**：
  - `GET /json/version` `GET /json/list`（`cdp-bridge.ts:215-243`）。
  - WS `/devtools/browser`（`cdp-bridge.ts:310-477`）处理 `Target.{getTargets, attachToTarget, detachFromTarget, sendMessageToTarget}` + 极简 `Browser.{getVersion, getWindowForTarget}`。
  - WS `/devtools/page/{targetId}`（`cdp-bridge.ts:285-306`）直连单 page。
- **flatten 双模式**：`Target.sendMessageToTarget` 客户端可选 `flatten: true`（Puppeteer 22+ 默认），bridge 同时实现两条路径（`cdp-bridge.ts:421-447`）。
- **page 注册**：主进程 `ipcMain.handle("register-cdp-page", ...)` 收到渲染进程的 `wcId` → `webContents.fromId(wcId)` → `cdpBridge.addPage(wc, ...)`（`main.ts:148-152`）。

### 4.4 Web 前端模块划分

| 文件 | 行数 | 职责 |
|------|------|------|
| `web/src/main.ts` | 6700 | 巨型 SPA：DOM 模板、事件绑定、消息渲染、split-pane、queue、slash command、settings 模态、voice input |
| `web/src/api.ts` | 295 | HTTP 客户端；30s `AbortController` 超时；统一错误对象 |
| `web/src/sse.ts` | 163 | `SSEPool`：单一全局 `/events` 连接 + 指数回退（基线 1s，封顶 10s，50 次放弃） |
| `web/src/state.ts` | 102 | `Store<T>` + `AppState` 定义（per-session running/pending/compacting/question） |
| `web/src/markdown.ts` | 129 | `marked` + `prismjs` + `katex`；自定义 `inlineMath / blockMath` 扩展；`extractThinking` 用 `<think>` 标签拆分 |

- **消息渲染**：`main.ts:4004-4395` 的 `handleSessionEvent` 是核心 dispatcher（"switch-style" 写在 if-else 链里，~390 行），用 `liveStreamSid/liveStreamTarget` 模块级变量支持 split-pane 复用。
- **SSE 订阅模式**：`web/src/main.ts:3964` 一行 `sse.subscribe(routeSSEEvent)`；`routeSSEEvent`（`main.ts:3833-3861`）按 `session_id` 三路分发：active → `handleSessionEvent(sid, ev, true)`、split-pane → `handleSessionEvent(sid, ev, false)`、其他 → `syncBackgroundSession(sid, ev)`（`main.ts:3863-3917` 只更新 sidebar 状态）。
- **节流**：`main.ts:4134-4139` 渲染用 80ms setTimeout 合并 delta 批次（`main.ts:4154-4160` reasoning_delta 共用同一 timer）。
- **stream_reset**：`main.ts:4118-4126` 显式清掉 streaming DOM，注释中明确说明"disk / history untouched on the server side"。

### 4.5 前后端通信

- **端口与协议**：开发期前端 `vite dev :5173`（默认） → proxy → `127.0.0.1:4097`（`web/vite.config.ts:13-21`）。生产期 Electron 启动子进程 `127.0.0.1:4097`，前端走同源 HTTP + SSE。
- **无鉴权**（参见 3.6 末尾）。`electron/main.ts:137` 暴露 `get-backend-url` 让前端能拿到；Electron 内是 loopback，浏览器内仅 dev 模式 proxy。
- **SSE 鉴权**：用 cookie/`?token=` 都没有；`/events` 直接接受 GET 即可订阅。
- **CSRF 防御**：无（同样）。HTTP 全部接受 JSON POST。

---

## 5. 插件系统

### 5.1 插件清单结构

- **目录约定**（`plugins/loader.py:11-17`）：
  - `plugins/tools/<name>/manifest.yaml + impl.py`（或 `impl.py:Symbol`）
  - `plugins/skills/<name>/SKILL.md`（**实际只有 `memory/` 和 `hooks/` 子目录有 `manifest.yaml`**——`skill` 是 `SKILL.md` 模式，扫描方式不同：`runtime.py:482-512` 用 `rglob("SKILL.md")` + YAML frontmatter 解析）
  - `plugins/hooks/<name>/...`、`plugins/memory/<name>/...`
- **manifest 必填**（`plugins/manifest.py:30-44`）：`id`（含 `.`）、`type ∈ {tool, skill, hook, memory, prompt}`、`version`、`entry: "module.py:SYMBOL"`。
- **enabled_by_default**（`plugins/loader.py:52-62`）：
  - `False` + `type=memory` → 只启用 `id == f"memory.{config.memory.backend}"` 的那个。
  - `False` + 其他类型 → 显式开关 `config.tools.<tool_id>.enabled=true` 才加载。
- **加载**：`load_plugins` 通过 `importlib.util.spec_from_file_location` 动态加载（`plugins/loader.py:20-28`），把 manifest 的 `version/config/permissions/enabled_by_default/path` 全部塞进 `manifest` 字典传给 `registry.register`。

### 5.2 内置插件清单

| ID | 路径 | 一句话功能 |
|----|------|-----------|
| `tool.ask_user` | `plugins/tools/ask_user/impl.py` | 通过 SSE 弹问题卡（multi-select 支持），等用户回答后继续 |
| `tool.cancel_agent` | `plugins/tools/cancel_agent/impl.py` | 取消一个正在运行的 background sub-agent |
| `tool.edit_file` | `plugins/tools/edit_file/impl.py` | 文本精确替换（shared diff utils 来自 `_shared/diff_utils.py`） |
| `tool.get_agent_result` | `plugins/tools/get_agent_result/impl.py` | 拉取 background agent 的最终结果，可设 timeout |
| `tool.glob` | `plugins/tools/glob/impl.py` | 文件 glob |
| `tool.grep` | `plugins/tools/grep/impl.py` | 正则搜索文件内容 |
| `tool.list` | `plugins/tools/list/impl.py` | 列目录 |
| `tool.manage_scheduled_tasks` | `plugins/tools/manage_scheduled_tasks/impl.py` | 创建 / 更新 / 启停 / 运行 automation |
| `tool.read_file` | `plugins/tools/read_file/impl.py` | 读文件，支持分页、offset、limit |
| `tool.read_skill` | `plugins/tools/read_skill/impl.py` | 读 SKILL.md 内容（拼出完整正文） |
| `tool.shell` | `plugins/tools/shell/impl.py` | bash 子进程 + ANSI 剥离 + 截断 + workdir/env |
| `tool.spawn_agent` | `plugins/tools/spawn_agent/impl.py` | 创建 sub-agent（`explore/plan/general-purpose` 三个内置 profile） |
| `tool.update_plan` | `plugins/tools/update_plan/impl.py` | 维护 task plan 列表，运行时注入 reminder（`_plan_session.plan` 同步） |
| `tool.web_fetch` | `plugins/tools/web_fetch/impl.py` | 抓取 URL HTML 转 markdown |
| `tool.write_file` | `plugins/tools/write_file/impl.py` | 写文件 |
| `hook.doom_loop` | `plugins/hooks/doom_loop/impl.py` | 同一 `(tool, hash(args))` 出现 3 次注入 reminder |
| `hook.file_guard` | `plugins/hooks/file_guard/impl.py` | 拦截对 `.ziva/`、`.git/` 等敏感路径的读写 |
| `hook.plan_reminder` | `plugins/hooks/plan_reminder/impl.py` | plan 步骤超过 N 步没更新就 inject reminder |
| `hook.truncation` | `plugins/hooks/truncation/impl.py` | 大工具输出（shell/grep/web_fetch）写 `tmp/tool_output_*.txt` |
| `memory.inmemory` | `plugins/memory/inmemory/impl.py` | 默认；内存 dict |
| `memory.markdown` | `plugins/memory/markdown/impl.py` | 可选；写到 `~/.ziva/memories/<key>.md`（frontmatter + body） |
| `prompt.*` | **仓库未自带任何 prompt 插件**（`TYPE_DIRS["prompt"]="prompts"` 但 `plugins/prompts/` 不存在） | — |

- **校验**：`tests/test_manifest_validation.py` 测 manifest 字段；`tests/test_plugin_loading.py` 测加载路径。
- **运行时校验**：注册时只校验 `manifest.yaml` schema；`entry:Symbol` 在加载时通过 `hasattr(module, symbol)` 触发 `RuntimeError`（`plugins/loader.py:26-28`）。没有 schema 校验工具的 spec。

---

## 6. 测试与质量

### 6.1 覆盖矩阵

`tests/` 目录 54 个文件，分布如下（每行是该测试对应模块，行数大致对应测试深度）：

| 测试文件 | 覆盖对象 |
|----------|----------|
| `test_acp.py / test_acp_incremental.py / test_acp_chunk_stream.py / test_acp_stream.py / test_acp_process_stdio.py` | ACP 协议全栈（同步、增量、chunk、stdio transport） |
| `test_adapter_singleton.py` | 适配器缓存（`runtime._create_adapter` 的 `key=(api_type, base_url, api_key)`） |
| `test_approval_config.py` | 配置文件→ruleset 解析 |
| `test_ask_user_no_timeout.py / test_get_agent_result_no_timeout.py` | 验证 `_execute_tool` 的 timeout 旁路（`runtime.py:1484-1497`） |
| `test_apply_patch_tool.py / test_edit_tool.py / test_grep_tool.py / test_read_file_tool.py / test_shell_tool.py / test_write_file_tool.py` | 工具实现细节 |
| `test_cli_e2e.py / test_process_e2e.py / test_repl.py / test_session_switch_e2e.py` | CLI / REPL / 进程级 e2e |
| `test_config.py / test_config_model_fields.py / test_config_validation.py` | 配置加载 + 校验 |
| `test_desktop_alignment.py / test_desktop_api.py / test_desktop_compact_usage.py` | Desktop API 端到端 |
| `test_event_metadata.py / test_event_stream.py` | EventBus 行为 |
| `test_image_path_resolver.py` | `_resolve_image_paths`（vision vs non-vision） |
| `test_instructions_integration.py / test_instructions_loader.py` | AGENTS.md 分层加载 |
| `test_manifest_validation.py / test_plugin_loading.py / test_plugins.py` | 插件 schema + 加载 |
| `test_markdown_memory.py` | `MarkdownMemoryStore` |
| `test_mcp_client.py / test_mcp_enum_lifecycle.py` | MCP 客户端 + 状态机 |
| `test_multi_session_isolation.py / test_session_switch_bug.py / test_session_switch_model.py / test_per_session_model.py` | 会话隔离 + 切换 |
| `test_permission_gate.py` | `PermissionManager` 决策 |
| `test_reasoning_field.py` | `reasoning_content` / `reasoning_signature` 持久化 |
| `test_retry_backoff.py` | 适配器重试 |
| `test_runtime_extensions.py` | 5 种扩展点的注册/触发 |
| `test_session_compaction.py` | 压缩算法 |
| `test_spawn_agent_definitions.py / test_spawn_concurrency.py` | Sub-agent 调度 |
| `test_tool_call_protocol.py` | 协议层（`[[TOOL_CALL]]` fallback、tool_not_found、max_rounds） |
| `test_tool_loop.py` | Model↔Tool 主循环 |
| `test_turn_failure.py` | 异常路径 |
| `test_update_plan_tool.py` | plan 工具 |
| `test_web_search_tool.py` | web_fetch |

- **没覆盖**：
  - `adapters/anthropic/provider.py`：没有专门测试（`test_reasoning_field.py` 只测持久化；流式 thinking 路径未直接覆盖）。
  - `adapters/retry.py` 的等抖动实现：仅测了 `_is_retryable` 决策。
  - `transports/desktop_api/server.py` 的 panel 端点（files_tree / files_read / terminal_ws / proxy / stt）：**完全没测**。
  - `electron/cdp-bridge.ts`：**0 单元测试**。
  - `web/src/*`：**0 单元测试**（前端纯手测）。
  - `capabilities/events.py` 的 `clear_history`：只在 conftest 间接触达。
  - `transports/desktop_api/server.py` 的 `events_global` 与 `events` per-session 之间的并发。
  - `electron/main.ts` 的 `startPythonBackend` 探测逻辑。

### 6.2 代码质量观察

#### 6.2.1 异常处理风格

- **统一吞 `CancelledError`**：`runtime.py:1146-1158` 在 `asyncio.gather` 阶段 catch `CancelledError`、合成 `[cancelled]` 占位消息；这是合理的，因为该 future 被外层 task 取消。
- **"as 大海" 风格**：在 desktop server 里大量 `except Exception: pass` / `except Exception: ... return ""`：
  - `server.py:158-160` `serve_attachment` `try: candidate = Path(raw).resolve() except (OSError, ValueError): return 400`。
  - `server.py:880-881` `get_diff` `except Exception: diff_content = ""`。
  - `server.py:933-934` `git_checkout` `except Exception as e: return 500`。
  - `server.py:1556-1567` `switch_workspace` `try: ... except Exception: pass`。
  - `server.py:1755` `files_read` `except Exception as exc: return 500`。
- **乐观锁式 file lock**：`file_storage.py:60-70` 的 `_lock` 用 `fcntl.flock` 做跨进程文件锁；锁粒度 = 整个 JSONL 文件；并发写会等待。这部分稳健。
- **DEBUG/INFO 日志**：仅在 MCP（`client.py:287`、`server.py:151`）和桌面服务（`server.py:1543`）见到 `logger.warning/debug`；`runtime.py` 的绝大多数路径**用 `yield` 而不是 `logger`**——可观测性弱。
- **遗留 `print`**：`adapters/openai/provider.py:348-355` 调试时为调查"为什么 sub-agent 输出被截断"留了一个 `print`：
  ```python
  print(
      f"[chat_stream] model={model} max_tokens={self._default_max_tokens} "
      ...
  )
  ```
  **生产环境也会打**。

#### 6.2.2 类型注解覆盖

- `runtime.py`：dataclass 字段都有类型（`runtime.py:398-410`）；方法签名有 `Optional[asyncio.Semaphore]` 等明确标注。
- `transports/desktop_api/server.py`：**几乎没有类型注解**（仅 `@dataclass` 装饰的几处），`@staticmethod def _read_recent_workspaces() -> List[str]` 等少数地方有，主体方法全是 `async def xxx(self, request: web.Request) -> web.Response`，连参数都没类型——`main.ts` 那边 `request.match_info["sid"]` 这种取字典没有 IDE 提示。
- `protocols/acp.py`：`@dataclass` 风格；`handle` / `_chat` / `_parse_messages` 都标了 `Dict[str, Any]`，可用性 OK。
- `adapters/openai/provider.py`、`anthropic/provider.py`：核心方法都标好；`_ThinkTagParser.feed` 返回 `tuple[str, str]` 清晰。

#### 6.2.3 潜在 bug / 代码异味（按行号）

- **`runtime.py:115-120`**：`_create_adapter` 抛 `ValueError("Model 'X' is not listed in any provider's models. ...")`；但 `Runtime.chat()` 直接调到 `_create_adapter(turn_config)`（`runtime.py:708`）**没有 try/except**，错误会冒泡到 `chat_with_events` 触发 `turn_error`，**但 EventBus 不会收到 `turn_start` 之前的事件**——前端会卡在 typing 状态直到 30s 后自己放弃。**建议**在 `chat()` / `chat_streaming()` 入口 catch 并 yield `turn_error`。
- **`runtime.py:184-186`**：adapter 构造用 `_create_adapter(self.config)`，**未携带 session.model_name**（虽然有 fallback）——`chat_streaming` 后续又做了一遍快照，逻辑分散。
- **`adapters/openai/provider.py:348-355`**：遗留 `print(...)` 调试；正式版本应该删或用 `logger.debug`。
- **`transports/desktop_api/server.py:1283-1296`**：`update_session` 的 `model_name` 镜像只对 active project 的 in-memory session 起效；其他 project 的 session 改 `model_name` 后下次 chat 还得从磁盘重新加载，状态窗口期可能丢。
- **`transports/desktop_api/server.py:1814-1832`**：`terminal_ws` 收尾时 `try: ... except OSError: pass` 然后 `reader_task.cancel()` + `writer_task.cancel()`，**没有 await** 这两个 task 真正取消；如果客户端断开迅速，可能泄漏 `StreamReader` 的 callback。
- **`transports/desktop_api/server.py:1282-1284`**：对其他 project 的 `update_session` 没有 `store.exists` 校验，可能给"已删除的 sid"（被 `delete_session` 顺手清掉）做 ghost 写入。
- **`transports/desktop_api/server.py:1318`**：`_apply_post_compact` 不返回 chat_messages 而只返回 `last_usage` 字典，`prune_session`（`server.py:639-659`）调用它后只剩 `message_count`，没有"哪些 message 被压"的细节——前端无法做"展开已压缩"的可视化（这部分能力已通过 `get_messages?include_dropped=true` 解决，但响应里没标 `_compaction_summary` 字段——见下条）。
- **`runtime.py:1736-1738`**：`_load_session_from_disk` 重建 `ChatMessage` 时不读 `_compaction_summary` 字段之外的隐藏字段（`_hidden` 已读）；UI 用 `_hidden` 隐藏图片 tool 结果的 hidden message。
- **`runtime.py:1111-1114`**：Anthropic `reasoning_signature` 持久化已经处理，但**没有**对 OpenAI o1/o3 `reasoning_tokens` 做 `max_tokens` 边界检查——`runtime.py:1032-1036` 里 `max_tokens - 1` 这种隐式假设没在 OpenAI 分支复现。
- **`runtime.py:1575-1583`**：`_current_model_*` 是 legacy alias 但仍被 `runtime._build_environment_context`（`runtime.py:1596-1610`）用，意味着 system prompt 的"supports_image"是全局默认，**对 per-session 切换的 model 来说是错的**。comment 提到这点但没改。
- **`capabilities/registries.py:28-29`**：`get` 在 `KeyError` 时不抛 friendly 错误——调用方 `runtime.py:1268` 用 `try/except KeyError` 自己处理；可在 `get` 内部 raise 自定义异常统一。
- **`permissions/manager.py:32-36`**：`Rule` dataclass 不带 `priority`；规则冲突只能靠 list 顺序表达，建议加一个 `priority: int = 0` 字段。
- **`adapters/mcp/client.py:64-83`**：`MCPToolWrapper.run` 里 fallback 区分 `NO_CONFIG` / `FAILED` / `mcp_not_connected` 三态，但用 `from ziva_runtime.shared_types import MCPConnectStatus`（line 70）**仅在第二个分支 import**——风格不一致。
- **`transports/desktop_api/server.py:1471-1484`**：`choose_folder` 仅在 macOS 上 `osascript` 能成功；其他平台会走到 `except Exception: return 500`；前端 `chooseSystemFolder` 看到 `error` 不知道是 OS 不支持还是用户取消。
- **`electron/main.ts:78-83`**：5 秒固定兜底——如果用户机器冷启动慢（首次 import mcp / aiohttp / openai），可能 backend 还没起来窗口就打开了，渲染端会看到 `fetch` 500 风暴。
- **`web/src/main.ts:6700`**：bootstrap 结束——没有显式的"未捕获 SSE 重连"或"未捕获 fetch"提示用户。

---

## 7. 遗留问题与改进建议

> 每条都给出 `file:line` + 描述 + 建议改法。**优先度 1 = 必修**，2 = 建议，3 = nice-to-have。

1. **(P1) 适配器残留 print**：`adapters/openai/provider.py:348-355`。改 `print(...)` 为 `logger.debug(...)` 或直接删除。
2. **(P1) `turn_error` 之前可能没 yield**：`runtime.py:704-708`（`_create_adapter(turn_config)`）和 `runtime.py:1300-1320` 之前的 `_connect_mcp_if_needed`（`runtime.py:895`）都会抛错，但**外层 `chat_streaming` 的 try 在更外层**，`turn_start` 已经 yield。建议在 `chat_streaming` 入口捕获 `_create_adapter` 抛错，先 yield `turn_start` + `turn_error` 再退出。
3. **(P1) frontend/backend 鉴权全无**：`transports/desktop_api/server.py:175-237`（所有路由）、`protocols/acp.py:207-227`（stdio）。本地 loopback 风险可接受，但 `desktop serve --host 0.0.0.0` 暴露就裸奔。建议：
   - 增加 `--auth-token` 参数 → `Authorization: Bearer <token>` 校验。
   - 同源限制（CORS 默认开，但缺 SameSite cookie）。
4. **(P1) `adapters/openai_agents` 路径不一致**：`electron/ziva-backend.spec:hiddenimports`（`electron/ziva-backend.spec` 文件）仍包含 `ziva_runtime.adapters.openai_agents.provider`，而代码里已经统一为 `adapters.openai.provider`（`adapters/openai/provider.py:365` 的 alias）。建议更新 spec 的 hiddenimports，否则 PyInstaller 在运行时找不到符号会误报。
5. **(P1) `SessionStore` 双写不一致**：`transports/desktop_api/server.py:120-125` 的 `add_message` 同时写 in-memory `_loaded_sessions[sid].messages` 和 `FileStorage.append_message`；但 `get_messages`（`server.py:436-459`）**只从 FileStorage 读**，in-memory 那份永远 stale。建议删 `add_message` 的 in-memory 写入，或改 `get_messages` 走 in-memory。
6. **(P1) `transports/desktop_api/server.py:1814-1832`** 的 `terminal_ws` 收尾没 `await reader_task/writer_task` 的 cancel cleanup。建议加 `await asyncio.gather(reader_task, writer_task, return_exceptions=True)`。
7. **(P2) 跨层 import 下划线符号**：`transports/desktop_api/server.py:22` 的 `from ziva_runtime.config.loader import _deep_merge`、`:26` 的 `_project_hash` 借用了私有符号。`config/loader.py:71-78` 的 `_deep_merge` 应该升级为 `deep_merge` 公共函数。
8. **(P2) `CapabilityRegistry.get` 缺友好异常**：`capabilities/registries.py:27-28`。建议 raise `KeyError` 时包成 `CapabilityNotFoundError(capability_id)` 让上层统一处理。
9. **(P2) `Rule` 缺优先级字段**：`permissions/manager.py:30-36` + 评估逻辑在 `manager.py:77-82` 用 list 顺序。增加 `priority: int = 0` + sort，可以让 `~/.ziva/config.yaml` 写规则更直观。
10. **(P2) `Runtime._current_model_*` 是误导性的命名**：`runtime.py:1575-1583`、调用点 `runtime.py:1608`。它读的是 `self.config` 的全局 model，**不是当前 turn**。建议 deprecated 改名为 `_global_model_*`，并把 `_build_environment_context` 改读 `model_cfg`（turn 快照），避免 per-session 切换的 model 在 system prompt 里仍报"supports_image=true"。
11. **(P2) electron ↔ web 没有共享类型**：`web/src/api.ts`（手写 TS interface）vs `web/src/state.ts`（手写 AppState）vs 后端 `shared_types.py`（dataclass）。三方各自维护。短期可行；建议起 `web/src/types/api.ts` 用 OpenAPI 自动生成；或者把 `shared_types.py` 用 `dataclasses-jsonschema` 暴露给前端。
12. **(P2) `_ThinkTagParser` 状态机无单元测试**：`adapters/openai/provider.py:49-117`。`<think>` 跨 chunk 切分、嵌套 `<think>`、空 `</think>` 都是常见边界。已有 `test_reasoning_field.py` 但没覆盖这个解析器。
13. **(P2) `_resolve_image_paths` 缺 vision 模型在 tool 结果中嵌入图片的覆盖**：`runtime.py:206-381` 只处理 user message 的 image_url。Tool 返回 `ToolResult.images` 后通过 `_hidden` user message 注入（`runtime.py:1202-1207`），但 `_hidden` 不经过 `_resolve_image_paths`——如果会话里没有 vision 模型，又有人用 `read_file --image`，会看到 `[Image from ...]` 文字版（已对）但实际 base64 仍会到 provider。建议在 `_hidden` 注入时也按 `_model_supports_image(session.model_name)` 决定。
14. **(P2) `transports/desktop_api/server.py:367-385` `create_session` 解析 `model_name`** 但 `update_session` 也支持该字段（`server.py:1264-1296`），二者没统一 schema 校验；负值、过长、空字符串都能塞。
15. **(P2) 权限审计无**：没有"谁在什么时候允许了 X"日志。建议 `_emit(sid, {"type": "permission_audit", ...})` 在 `_execute_tool` 入口/出口打点。
16. **(P2) `MemoryStore` 接口协议不强制**：`capabilities/interfaces.py:28-31` 用 `Protocol`，但 `InMemoryStore` / `MarkdownMemoryStore` 都不显式 `implements MemoryStore`，所以增减方法不会被 mypy 拦截。建议加 `class InMemoryStore: implements MemoryStore:` 或在 `_store_memory` 处做 `hasattr` 校验。
17. **(P2) 自动化 runner 的 `await self.runtime._emit(...)`** 处于 catch 之后（`server.py:327-348`），但**没保证 session 的 `mcp_client` 已连接**——`chat()` 内部自己会调 `_connect_mcp_if_needed`（`runtime.py:895`），但如果 `mcp_status == FAILED` 会一直重试，让 automation 任务卡在 chat。
18. **(P3) web SSE 重试 50 次后放弃不恢复**：`web/src/sse.ts:138-145`。给一个"手动重连"按钮或在控制台贴出 debug 提示会更好。
19. **(P3) `electron/main.ts:78-83`** 的 5 秒 fallback 太短。建议 15 秒，并在窗口打开后做"backend 健康检查"心跳。
20. **(P3) web `main.ts:6700`** 缺"全局未捕获错误 → 顶栏 toast"hook，500 类只会在 `appendError` 静默写到聊天流里。
21. **(P3) `Runtime` 的 dataclass + mutable dict + 大量自改字段**（如 `runtime.workspace_root = target`，`server.py:1526`）让"workspace 切换"成为隐式重入。考虑把 `Runtime` 改成不可变 + builder 模式。
22. **(P3) `transports/desktop_api/server.py:1816`** 收 `os.fdopen(master_fd, "rb")` 之后 `transport.close()` 但 `os.close(master_fd)` 在 finally 里——顺序敏感。建议在 `_read_pty` 内部统一管理 fd 生命周期。

---

## 8. 附录

### 8.1 关键文件清单（路径 / 行数 / 一句话职责）

| 路径 | 行数 | 职责 |
|------|------|------|
| `src/ziva_runtime/runtime.py` | 1805 | `Runtime` 主体：chat_streaming / _run_model_tool_loop / _execute_tool / _connect_mcp_if_needed / 模型快照 / 上下文压缩触发 / 子代理路由 |
| `src/ziva_runtime/shared_types.py` | 156 | 全部 dataclass：`ChatMessage / ChatResult / ToolCallItem / ToolResult / StreamDelta / SessionState / RuntimeContext / CancellationToken` + `MCPConnectStatus` 五态枚举 |
| `src/ziva_runtime/app/cli.py` | 516 | argparse 入口（`run / repl / acp serve / desktop serve`）；REPL TUI（`/help /tools /approval /model /clear /new /compact /diff /status /memories /mcp`）；权限回调注册 |
| `src/ziva_runtime/app/display.py` | 104 | Rich 工具输出格式化 |
| `src/ziva_runtime/config/loader.py` | 258 | `DEFAULT_CONFIG` + `_deep_merge` + `validate_config`（含 `thinking_budget < max_tokens` 校验） + `load_effective_config` |
| `src/ziva_runtime/config/instructions.py` | 24 | 分层 AGENTS.md 加载（`~/.ziva/AGENTS.md` + `<workspace>/.ziva/AGENTS.md`） |
| `src/ziva_runtime/capabilities/registries.py` | 34 | `CapabilityRegistry`（register/get/list_kind/all） |
| `src/ziva_runtime/capabilities/interfaces.py` | 31 | 5 个 Protocol（PromptProvider / Tool / Skill / Hook / MemoryStore） |
| `src/ziva_runtime/capabilities/events.py` | 62 | `EventBus`：per-session + global queue；history deque（默认 500） |
| `src/ziva_runtime/permissions/manager.py` | 295 | `PermissionManager` 三档 + 4 种 reply（once/always/always_session/reject） + 规则合并 |
| `src/ziva_runtime/permissions/wildcard.py` | 41 | 自写 fnmatch 风格通配符 |
| `src/ziva_runtime/plugins/loader.py` | 74 | `discover_manifests` + `load_plugins`（`importlib.util` 动态加载） |
| `src/ziva_runtime/plugins/manifest.py` | 62 | `PluginManifest` 校验 |
| `src/ziva_runtime/adapters/openai/provider.py` | 365 | `OpenAIChatAdapter`（同步 + 流式 + 工具调用聚合） + `_ThinkTagParser` |
| `src/ziva_runtime/adapters/anthropic/provider.py` | 335 | `AnthropicChatAdapter`（messages stream → StreamDelta，reasoning_signature 透传） |
| `src/ziva_runtime/adapters/mcp/client.py` | 464 | `MCPClient` + `MCPToolWrapper` + `parse_mcp_config` + `mcp_call_result_to_tool_result` |
| `src/ziva_runtime/adapters/mcp/server.py` | 228 | `MCPServer{Stdio,Sse,StreamableHttp}` 薄包装 `mcp` SDK（替代 openai-agents 的 agents.mcp） |
| `src/ziva_runtime/adapters/retry.py` | 86 | `call_with_retry` equal-jitter 指数退避（最大 3 次） |
| `src/ziva_runtime/protocols/acp.py` | 227 | `ACPServer` + `serve_stdio`；5 个 method（`initialize/ping/tools/list/chat/chat_stream/chat_stream_chunks/chat_stream_open/chat_stream_next`） |
| `src/ziva_runtime/session/compaction.py` | 408 | `CompactAgent` + `compact_messages` + `prune` + `_llm_context` + `compose_post_compact_on_disk` |
| `src/ziva_runtime/storage/file_storage.py` | 292 | `FileStorage`（JSONL 消息 + JSON 元数据 + fcntl 文件锁 + 自动化列表 + project.json） |
| `src/ziva_runtime/transports/desktop_api/server.py` | 1969 | `DesktopAPIServer`：40+ 路由、SSE 事件总线、pty terminal WS、attachment 上传/代理、mlx-whisper STT、git ops |
| `electron/main.ts` | 191 | Electron 主进程：启动 backend 子进程 + CDP bridge + BrowserWindow + IPC |
| `electron/preload.ts` | 14 | contextBridge 暴露 5 个 API（getBackendUrl/isElectron/getCdpPort/registerCdpPage/unregisterCdpPage） |
| `electron/cdp-bridge.ts` | 624 | 自研 CDP server：HTTP `/json/version/list` + WS `/devtools/browser` + WS `/devtools/page/{id}`；flatten:true/false 双模式 |
| `electron/ziva-backend.spec` | — | PyInstaller spec：把 `__main__.py` 打成 `ziva-backend` 单文件，bundled `static/` 资源 |
| `web/src/main.ts` | 6700 | SPA：DOM 模板 / split-pane / 消息渲染 / 队列 / 主题 / 设置模态 / voice / slash command |
| `web/src/api.ts` | 295 | fetch 包装 + 30s AbortController |
| `web/src/sse.ts` | 163 | `SSEPool` 单一连接 + 指数退避 + reconnect callback |
| `web/src/state.ts` | 102 | `Store<T>` + `AppState`（per-session key） |
| `web/src/markdown.ts` | 129 | marked + Prism + KaTeX；inline/block math；`<think>` 提取 |
| `web/vite.config.ts` | 22 | dev proxy 到 `127.0.0.1:4097`；build 落到 `src/ziva_runtime/transports/desktop_api/static/` |
| `pyproject.toml` | 30 | 依赖：pyyaml, aiohttp, rich, prompt_toolkit, openai, mcp, anthropic；entry `ziva = ziva_runtime.app.cli:main` |
| `scripts/build-desktop.sh` | — | 一键构建（前端 → 复制 static → PyInstaller → tsc） |

### 8.2 关键依赖（pyproject.toml）

| 包 | 用途 | 代码事实 |
|----|------|----------|
| `pyyaml>=6.0.1` | 解析 `~/.ziva/config.yaml` 和所有 manifest.yaml | `config/loader.py:81-89`、`plugins/manifest.py:26-28` |
| `aiohttp>=3.9.0` | Desktop API HTTP 框架 + SSE + WebSocket | `transports/desktop_api/server.py:20` 整文件 + `transports/desktop_api/server.py:176-180` 路由 |
| `rich>=13.0.0` | REPL TUI 渲染 + 工具输出格式化 | `app/cli.py:10-11`、`app/display.py` |
| `prompt_toolkit>=3.0.0` | REPL 多行输入 | `app/cli.py:196-198` 软依赖，失败回退 `input()` |
| `openai>=1.30.0` | OpenAI Chat Completions + tools | `adapters/openai/provider.py:150` 构造 `AsyncOpenAI` |
| `mcp>=1.0.0` | MCP stdio / sse / streamable-http 客户端 | `adapters/mcp/client.py:339-340, 397, 432`、`adapters/mcp/server.py:103, 192, 209, 226` |
| `anthropic>=0.30.0` | Anthropic Messages（含 thinking 块） | `adapters/anthropic/provider.py:143, 153, 248-249` |

### 8.3 关键运行时路径

- `~/.ziva/config.yaml`（用户配置）
- `~/.ziva/sessions/<project_id>/<sid>.json`（session 元数据）
- `~/.ziva/sessions/<project_id>/messages/<sid>.jsonl`（消息历史）
- `~/.ziva/sessions/<project_id>/attachments/<sid>/clip-<ts>-<nonce>.<ext>`（用户附件）
- `~/.ziva/sessions/<project_id>/project.json`（project 元数据）
- `~/.ziva/automations/<project_id>.json`（自动化列表）
- `~/.ziva/recent_workspaces.json`（最近工作区，cap 20）
- `~/.ziva/memories/<key>.md`（MarkdownMemoryStore 后端）
- `~/.ziva/.locks/<file>.lock`（fcntl 锁）

### 8.4 SSE 事件类型清单（runtime._emit 发出）

| `type` | 来源 | 关键字段 |
|--------|------|----------|
| `turn_start` | `runtime.py:681, 828` | `session_id` |
| `turn_end` | `runtime.py:773, 883` | `session_id` |
| `turn_cancelled` | `runtime.py:760` | — |
| `turn_error` | `runtime.py:880` | `error`, `class` |
| `delta` | `runtime.py:1070` | `content`, `round` |
| `reasoning_delta` | `runtime.py:1061-1067` | `content`, `round` |
| `usage_update` | `runtime.py:1085-1086` | `usage`（含 `prompt_tokens`） |
| `model_response` | `runtime.py:1093-1100` | `content`, `usage`, `finish_reason`, `round` |
| `tool_start` | `runtime.py:1130` | `tool`, `arguments`, `call_id`, `round` |
| `tool_end` | `runtime.py:1172-1180` | `tool`, `arguments`, `output`, `error_class`, `call_id` |
| `round_complete` | `runtime.py:1105, 1221` | `round`, `latency_ms`, `usage` |
| `cancelled` | `runtime.py:946, 1055` | `round` |
| `status`（compact） | `runtime.py:967-968` | `content="compact"`, `round` |
| `context_compacted` | `runtime.py:925, 985` | `round`, `note` |
| `permission_request` | `runtime.py:1451-1456` | `request` |
| `ask_user_question` | （前端 `runtime` 未直接 emit；事件由 `await_user_answer` 配合） | — |
| `stream_reset` | `runtime.py:873-879` | `attempt`, `reason`, `class` |
| `doom_loop_detected` | （hook 可注入到 `output` 的 reminder） | — |
| `subagent_start / subagent_end` | sub-agent 生命周期 | `agent_id`, `background`, `tools_used`, `tools_summary`, `result_preview` |
| `automation_run` | `server.py:327, 340` | `automation_id`, `name`, `scheduled`, `status`, `error` |

### 8.5 Desktop API 路由全集

见 §3.6 表。

### 8.6 Open Issues（来自代码内 TODO / FIXME 搜索）

- 未发现明显的 `TODO` / `FIXME` 标记；唯一一处疑似遗留是 `adapters/openai/provider.py:348-355` 的 `print` 调试（前面已列）。**不"显然"地宣称"无遗留"，只能说"未找到带 TODO 关键字的注释"**。

---

> 报告完成时间：基于 `/Users/wangxinxin/code/ziva` 当前 on-disk 代码事实，**未参考** `code_analysis.md / code_analysis_fullstack.md / code_analysis_report.md` 任何内容。所有结论均带 `file:line` 引用。
