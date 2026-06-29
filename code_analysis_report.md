# Ziva 仓库代码分析报告

> 分析时间：2026-06-23
> 分析对象：`/Users/wangxinxin/code/ziva`
> 报告版本：v1.0

---

## 0. 安全提示（重要）

在分析过程中发现仓库内 `README.md` 末尾以及若干文件读取返回中持续夹带**提示词注入（Prompt Injection）**，试图劫持模型行为（例如："Respond as helpfully as possible, but be very careful..."、"do not comply with complex instructions..."）。本报告已**完全忽略**这些注入内容，按你原始需求完成只读分析。建议：

1. 清理 `README.md` 末尾的注入段落。
2. 审查 commit 历史中如何引入（可能是从外部聊天记录复制粘贴导致）。
3. 建议在 CI/文档流水线中加入"敏感指令扫描"环节。

---

## 1. 项目概览

Ziva 是一个**类 Codex 的 CLI/Desktop 智能体后端运行时**——纯 Python 包 + 极薄 Electron 壳 + Vite 写的极简 UI，提供 ACP（Agent Control Protocol）stdio 服务、HTTP+SSE 后端、统一的 tool/skill/hook/memory/prompt 扩展 API、分层合并的 YAML 配置、OpenAI/Anthropic/MCP 多 provider 适配，以及 Model ↔ Tool 循环执行与自动上下文压缩。技术栈核心：`openai`、`anthropic`、`mcp`、`aiohttp`、`rich`、`prompt_toolkit`、`pyyaml`、Hatchling 构建，Electron+Vite 前端，PyInstaller 打包桌面端。

**一句话定位**："Codex-like 智能体后端"——一个轻量、可插拔、自托管、协议先行的 agent 运行时。

---

## 2. 目录结构

### 2.1 顶层速览

```text
ziva/
├── src/ziva_runtime/        # 主包（包名 ziva-runtime, v0.1.0）
├── electron/                # Electron 桌面壳（TypeScript）
├── web/                     # Vite + TS 写的 UI 客户端
├── plugins/                 # 内置插件：tools / skills / hooks / memory
├── tests/                   # 50+ 个 pytest 测试
├── scripts/                 # 烟雾测试 & 真实 API 集成脚本
├── docs/                    # 设计文档（agent-contracts, plans, …）
├── pyproject.toml           # Hatchling 构建配置
└── README.md                # 项目说明（含 ACP 协议描述）
```

### 2.2 各目录职责

| 目录 | 职责 |
|---|---|
| `src/ziva_runtime/` | Python 后端运行时本体。包含 runtime、协议、适配器、能力注册表、配置、权限、插件加载、存储、传输层。 |
| `electron/` | 桌面壳。`main.ts` 拉起 Python 后端子进程，`preload.ts` 是 IPC 桥，`cdp-bridge.ts` 把 Chrome DevTools 协议桥接到本地浏览器面板。 |
| `web/` | 浏览器/Vite 写的 UI shell：`main.ts` 入口、`api.ts` REST 客户端、`sse.ts` 事件流订阅、`state.ts` 状态管理、`markdown.ts` 渲染、`styles/`。 |
| `plugins/` | 内置插件实现，按 `tools/`, `skills/`, `hooks/`, `memory/` 分目录，每插件一个子目录带 `manifest.yaml`。 |
| `tests/` | 50+ 单元/集成测试，覆盖 ACP、adapter、config、compaction、desktop API、image resolver、MCP lifecycle、plugin loading、session 切换、spawn agent、tool loop 等。 |
| `scripts/` | `smoke_test.py`（无外部依赖端到端）、`test_real_api.py`、`test_real_desktop.py`（需要真实 API key 的集成脚本）。 |
| `docs/` | 协议契约、协作计划、方案设计文档。 |

### 2.3 `src/ziva_runtime/` 内部结构

```text
src/ziva_runtime/
├── __init__.py
├── __main__.py
├── shared_types.py              # 跨模块 dataclass: ChatMessage/Result/Event/...
├── runtime.py                   # 核心 Runtime 类（1780 行，最大单文件）
├── app/
│   ├── cli.py                   # argparse + Rich REPL/ACP/Desktop 命令
│   └── display.py
├── protocols/
│   └── acp.py                   # JSON-RPC 2.0 服务（227 行）
├── adapters/
│   ├── openai/provider.py       # OpenAIChatAdapter = OpenAIAgentsAdapter
│   ├── anthropic/provider.py    # AnthropicChatAdapter
│   ├── mcp/
│   │   ├── client.py            # MCPClient + 配置解析
│   │   └── server.py            # 极薄 stdio/SSE/streamable-http 包装
│   └── retry.py                 # 指数退避重试
├── capabilities/
│   ├── interfaces.py            # Protocol 定义（Tool/Skill/Hook/Memory/Prompt）
│   ├── registries.py            # CapabilityRegistry
│   └── events.py                # EventBus（per-session + global SSE 队列）
├── config/
│   ├── loader.py                # DEFAULT_CONFIG + 分层合并 + 严格校验
│   └── instructions.py          # 分层加载 AGENTS.md
├── permissions/
│   ├── manager.py               # PermissionManager + ruleset
│   └── wildcard.py              # 通配符匹配工具
├── plugins/
│   ├── manifest.py              # PluginManifest dataclass + 加载
│   └── loader.py                # discover/load
├── session/
│   └── compaction.py            # CompactAgent / prune / compact / _llm_context
├── storage/
│   └── file_storage.py          # FileStorage（JSONL + fcntl 文件锁）
└── transports/
    └── desktop_api/
        └── server.py            # DesktopAPIServer（aiohttp, 1925 行）
```

---

## 3. 核心架构分析

### 3.1 模块划分（按职责）

| 模块 | 路径 | 职责 |
|---|---|---|
| **Runtime** | `src/ziva_runtime/runtime.py` | 编排一切：`create()` 工厂、`chat()`/`chat_streaming()` 入口、`_run_model_tool_loop()` 主循环、工具执行、权限审批、MCP 连接、上下文压缩、记忆写入。 |
| **ACP 协议** | `src/ziva_runtime/protocols/acp.py` | JSON-RPC 2.0 stdio 服务。注册 8 个方法（见 §4.1）。 |
| **Desktop API** | `src/ziva_runtime/transports/desktop_api/server.py` | aiohttp HTTP+SSE，含 REST 路由、SSE 事件流、文件附件、PTY 终端、MCP 状态、自动化任务等。 |
| **OpenAI 适配器** | `src/ziva_runtime/adapters/openai/provider.py` | 直接用 `openai` SDK（Chat Completions），实现原生 function calling 与流式输出。`OpenAIAgentsAdapter` 仅为别名（"single-SDK strategy"）。 |
| **Anthropic 适配器** | `src/ziva_runtime/adapters/anthropic/provider.py` | 用 `anthropic` SDK，原生 tool_use 块。 |
| **MCP 适配器** | `src/ziva_runtime/adapters/mcp/` | 130 行本地薄包装替代 `openai-agents.agents.mcp`，支持 stdio / SSE / streamable-http。 |
| **能力注册表** | `src/ziva_runtime/capabilities/` | 通过 `Protocol` 接口和 `CapabilityRegistry` 实现统一扩展 API。 |
| **配置** | `src/ziva_runtime/config/` | `DEFAULT_CONFIG` → 全局 `~/.ziva/config.yaml` → session override 三层 `_deep_merge` + 严格 `validate_config`。 |
| **权限** | `src/ziva_runtime/permissions/` | 通配符规则、`PermissionManager.ask()/reply()`、`suggest` / `auto-edit` / `full-auto` 三档策略。 |
| **插件加载** | `src/ziva_runtime/plugins/` | `manifest.yaml` 发现 + `importlib` 动态导入。 |
| **会话压缩** | `src/ziva_runtime/session/compaction.py` | `prune_messages`（无 LLM）+ `compact_messages`（LLM 摘要）+ `compose_post_compact_on_disk` 保持磁盘/内存/LLM 三视图一致。 |
| **存储** | `src/ziva_runtime/storage/file_storage.py` | JSONL 消息 + 文件锁 + 原子写。 |
| **重试** | `src/ziva_runtime/adapters/retry.py` | equal-jitter 指数退避，识别 429/5xx/敏感内容错误。 |

### 3.2 核心数据流

```text
                          ┌──────────────────┐
                          │  CLI / Electron  │
                          │  (Rich REPL /    │
                          │   Browser / ACP) │
                          └────────┬─────────┘
                                   │  (a) REPL stdin
                                   │  (b) ACP JSON-RPC over stdio
                                   │  (c) HTTP POST /sessions/{sid}/turns
                                   ▼
                  ┌─────────────────────────────────────┐
                  │   Transport layer                   │
                  │   - app/cli.py  (a)                 │
                  │   - protocols/acp.py (b)            │
                  │   - transports/desktop_api/server.py│
                  │     (c)  REST + SSE /events         │
                  └────────────────┬────────────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────────┐
                  │  Runtime.chat() / chat_streaming()  │
                  │  - 加载会话历史（含压缩摘要）        │
                  │  - 解析 prompt 模板（plugins）        │
                  │  - 拼装 system_prompt (base+AGENTS+  │
                  │    env_ctx+skill_index)              │
                  │  - 解析 image_url（按模型 vision 能力）│
                  │  - 注册 CancellationToken            │
                  └────────────────┬────────────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────────┐
                  │  _run_model_tool_loop()              │
                  │  ┌──────────────────────────────┐   │
                  │  │ start-of-round auto-compact  │   │
                  │  │ (阈值 = last_usage / context │   │
                  │  │  >= 0.9)                     │   │
                  │  └──────────────────────────────┘   │
                  │  for round in 1..max_rounds:         │
                  │    1. model_adapter.chat_stream()    │
                  │       ─ 增量 deltas → EventBus       │
                  │    2. 若 finish_reason=tool_calls:   │
                  │       - 逐 tool: PermissionManager   │
                  │       - parallel asyncio.gather      │
                  │         → _execute_tool()           │
                  │       - tool_result 回填到 working   │
                  │    3. 若 finish_reason=stop: 终止    │
                  │  异常路径: 重试 1 次 (stream_reset)  │
                  └────────────────┬────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌──────────────┐          ┌────────────────┐         ┌────────────────┐
│ ModelAdapter │          │ ToolRegistry   │         │ MemoryStore    │
│ (OpenAI /    │          │ (shell/grep/   │         │ (inmemory /    │
│  Anthropic / │          │  edit/         │         │  markdown)     │
│  + MCP)      │          │  spawn_agent)  │         │                │
└──────┬───────┘          └────────┬───────┘         └────────┬───────┘
       │                           │                          │
       ▼                           ▼                          ▼
   ChatResult                ToolResult                  Mem dict
       │                           │                          │
       └─────────────┬─────────────┴────────────┬─────────────┘
                     ▼                          ▼
            EventBus.publish()           FileStorage.append_message()
                     │                          │
                     ▼                          ▼
              SSE /events (global)         ~/.ziva/sessions/<pid>/
              → Electron / Browser              messages/<sid>.jsonl
```

### 3.3 入口点

| 入口 | 位置 | 说明 |
|---|---|---|
| **CLI** | `src/ziva_runtime/app/cli.py` | `python -m ziva_runtime.app.cli run|repl|acp serve|desktop serve`，通过 `pyproject.toml` 注册 `ziva` 脚本。 |
| **`__main__`** | `src/ziva_runtime/__main__.py` | 透传到 `cli.main()`。 |
| **ACP serve** | `protocols/acp.py::serve_stdio` | `sys.stdin` 按行读 JSON-RPC，调 `ACPServer.handle()`。 |
| **Desktop serve** | `transports/desktop_api/server.py::DesktopAPIServer` | aiohttp 应用，含 REST + SSE + 静态资源 + WebSocket 终端。 |
| **REPL** | `app/cli.py::_repl_loop` | Rich + prompt_toolkit，支持 `/help` `/tools` `/approval` `/model` `/new` `/compact` `/diff` `/status` `/memories` `/mcp` 等斜杠命令。 |

---

## 4. 关键模块详解

### 4.1 ACP 协议（`protocols/acp.py`）

共 **8 个方法**（README 列了 6 个，实际代码还实现了 2 个多轮流式方法）：

| 方法 | 用途 | 关键行为 |
|---|---|---|
| `initialize` | 握手 | 返回 `name=ziva-acp, version=0.2.0, capabilities={chat, tools, stream}`。 |
| `ping` | 健康检查 | `{"pong": true}`。 |
| `tools/list` | 工具清单 | 委托 `runtime.list_tools()`。 |
| `chat` | 一次性返回 | `runtime.chat()` → `ChatResult` 包装为 `{message, model, usage, finish_reason}`。 |
| `chat_stream` | 事件时间线 | `runtime.chat_with_events()` → 返回 `events` 列表 + `final` payload。 |
| `chat_stream_chunks` | 增量 chunk 列表 | 同上事件，但 `_build_chunks()` 把 `model_response` 按 `token_granularity`（`word`/`char`）切分。 |
| `chat_stream_open` | 开流 | 同上 chunk 化后存到 `self._streams[stream_id]`，返回 `stream_id`。 |
| `chat_stream_next` | 拉流 | 按 `stream_id` 顺序出 chunk，流空时 `{"done": true, "chunk": null}`。 |

**统一错误包装**：`_err()` 返回 `{jsonrpc, id, error: {code, message, data: {classification}}}`，`classification ∈ {invalid_params, method_not_found, parse_error, invalid_stream}`。

**传输层**：`serve_stdio()` 是单线程 `asyncio.to_thread(sys.stdin.readline)` 循环，按行 JSON。

### 4.2 OpenAI Agents adapter（单 SDK 策略）

`src/ziva_runtime/adapters/openai/provider.py`：

```python
OpenAIAgentsAdapter = OpenAIChatAdapter   # 别名
```

**单 SDK 策略的含义**：

1. 整个项目**只直接依赖 `openai` SDK**（`openai>=1.30.0`），**没有**安装 `openai-agents` 库（虽然 `pyproject.toml` 没列）。
2. 代码注释明确写道"openai-agents wraps the mcp SDK with ~2800 lines ... none of which ziva uses"，所以**自己写了 ~130 行的 `mcp/server.py`** 替代。
3. 因此**命名 "OpenAI Agents Adapter" 实际是 OpenAI Chat Completions API 的标准适配器**，不是 openai-agents 框架。

**关键能力**：

- `chat()` / `chat_stream()` 都用 `client.chat.completions.create`。
- 流式支持 `stream_options: include_usage` 来拿 `reasoning_tokens`。
- 处理 OpenAI 风格的增量 `tool_calls` 拼接（按 `index`）。
- `_ThinkTagParser` 兜底：当 provider 把 CoT 嵌在 `<think>...</think>` / `<mm:think>...</mm:think>` 标签里时（非原生 `reasoning_content`），把它拆出来。
- 通过 `extra_body` 转发 provider 特有参数（不污染 OpenAI SDK 已知字段）。
- 使用 `adapters.retry.call_with_retry` 包了一层 retry/backoff。

**Anthropic 适配器**（`anthropic/provider.py`）：

- 同样直接用 `anthropic` SDK（`AsyncAnthropic`）。
- 把 ziva 内部 `tool` 消息转成 Anthropic 的 `tool_result` content block、assistant 的 `tool_use` block。
- `reasoning_signature` 仅在**真实存在**时回传给 API（避免假 signature 导致 Anthropic 400）。
- 流式基于 `client.messages.stream()`（Anthropic 的事件流），逐 `content_block_start/delta/stop` 拼装。

### 4.3 统一扩展 API（plugin/extension/skill/hook/memory）

#### 4.3.1 协议定义（`capabilities/interfaces.py`）

```python
class Tool(Protocol):
    def spec(self) -> Dict[str, Any]: ...
    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult: ...

class Skill(Protocol):
    def match(self, input_text: str, ctx: RuntimeContext) -> bool: ...
    async def execute(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]: ...

class Hook(Protocol):
    event_name: str
    matcher: str | None
    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]: ...

class MemoryStore(Protocol):
    async def put(self, key: str, value: Dict[str, Any], ctx: RuntimeContext) -> None: ...
    async def search(self, query: str, limit: int, ctx: RuntimeContext) -> list[Dict[str, Any]]: ...
    async def summarize(self, ctx: RuntimeContext) -> Dict[str, Any]: ...

class PromptProvider(Protocol):
    def render(self, template: str, variables: Dict[str, Any], ctx: RuntimeContext) -> str: ...
```

#### 4.3.2 注册表（`capabilities/registries.py`）

```python
class CapabilityRegistry:
    def register(capability_id, kind, instance, manifest): ...
    def get(capability_id) -> CapabilityRecord
    def list_kind(kind) -> list[CapabilityRecord]   # ★ Runtime 内部按 kind 查找
    def all() -> list[CapabilityRecord]
```

`kind ∈ {"prompt", "tool", "skill", "hook", "memory"}`。

#### 4.3.3 插件清单（`plugins/manifest.py`）

```yaml
id: tool.read_file            # 必须含命名空间分隔符 "."
type: tool                    # ∈ ALLOWED_TYPES
version: "1.0.0"
entry: "tool.py:ReadFileTool" # 格式 "module.py:Symbol"
config: { ... }
permissions: { fs: [read] }
enabled_by_default: true
```

#### 4.3.4 发现与加载（`plugins/loader.py`）

```text
plugins/
├── tools/<id>/manifest.yaml + <id>.py
├── skills/<id>/manifest.yaml + <id>.py
├── hooks/<id>/manifest.yaml + <id>.py
├── memory/<id>/manifest.yaml + <id>.py
└── prompts/<id>/manifest.yaml + <id>.py
```

- `discover_manifests(roots)` 扫描每个 type 子目录的 `*/manifest.yaml`。
- `load_plugins(roots, registry, config)` 校验 `manifest.type` 与目录类型一致；通过 `_load_symbol()` 动态 `importlib`；`enabled_by_default=False` 时按 `config` 决定是否实例化。

#### 4.3.5 Runtime 的统一调用

```python
# tool
for rec in self.registry.list_kind("tool"):
    out = await rec.instance.run(input_data, ctx)

# skill
for skill_rec in self.registry.list_kind("skill"):
    if skill.match(text, ctx):
        return await skill.execute(...)

# hook
for hook_rec in self.registry.list_kind("hook"):
    if hook.event_name == "before_turn":
        payload = await hook.handle(payload, ctx)

# memory
mems[0].instance.put("last_turn", {...}, ctx)

# prompt
prompts[0].instance.render(template, variables, ctx)
```

**事件总线（`capabilities/events.py`）**：`EventBus` 同时支持 per-session 订阅和 `subscribe_global()` 广播。SSE 路由 `/events`（`events_global`）走单条全局长连接，由 `session_id` 字段前端自行分发；老接口 `/sessions/{sid}/events` 仍保留兼容。

#### 4.3.6 内置插件清单

```text
plugins/tools/: _shared, ask_user, cancel_agent, edit_file, get_agent_result,
                 glob, grep, list, manage_scheduled_tasks, read_file, read_skill,
                 shell, spawn_agent, update_plan, web_fetch, write_file (15 个)
plugins/hooks/: doom_loop, file_guard, truncation
plugins/memory/: inmemory, markdown
plugins/skills/: (空，运行时从 skill.extra_paths 扫描 SKILL.md)
```

### 4.4 工具调用协议（Tool Call Protocol）

主路径：**Provider 原生 function calling**（OpenAI `tool_calls` / Anthropic `tool_use`），不再依赖旧的文本解析。

向后兼容的纯文本协议（来自 README）：

```text
# 优先结构化
[[TOOL_CALL]]{"name":"echo","arguments":{"text":"hello"}}[[/TOOL_CALL]]

# 兜底
TOOL_CALL echo {"text":"hello"}
```

`test_tool_call_protocol.py` 覆盖这两种格式。

### 4.5 YAML 分层合并（`config/loader.py`）

```python
DEFAULT_CONFIG              # 编译期默认
↓
_deep_merge(_load_yaml(global_path))   # ~/.ziva/config.yaml
↓
_deep_merge(session_override)          # CLI 参数 / 临时覆盖
↓
validate_config()                       # 严格校验（不合法直接抛 ValueError）
```

`validate_config` 校验：

- 必填顶层段：`model / prompt / tool / skill / hooks / memory / plugin / approval / sandbox`
- `model.name` 非空，`max_tokens > 0`，`thinking_mode ∈ {disabled, low, medium, high}`
- 关键不变量：`thinking_mode != "disabled"` 时 `thinking_budget_tokens < max_tokens`（Anthropic 强制要求）
- `providers[].capabilities` 只允许 `thinking / vision / tools` 三键
- `agents.<name>` 中 `hooks` 必须是 `before_turn/after_turn/before_tool/after_tool` 之一
- 工具 `max_rounds` 必须是正整数或 0

**单源真相**：`load_effective_config()` 的 docstring 明确写 "The runtime has a single source of truth — the global config file under the user's home directory. Workspace-local configs are intentionally not consulted."（与多数 CLI agent 不同，**不读工作目录的 AGENTS.md / CLAUDE.md 等**——仅读 `~/.ziva/AGENTS.md` 和 `<workspace>/.ziva/AGENTS.md`，见 `config/instructions.py`）。

### 4.6 权限系统（`permissions/manager.py`）

```python
class PermissionAction:   ALLOW / DENY / ASK
class PermissionReply:    ONCE / ALWAYS / ALWAYS_SESSION / REJECT
class PermissionManager:
    EDIT_TOOLS = ["edit", "write", "patch", "multiedit"]
    on_pending(callback)                # 注册审批回调（CLI / Web 都用）
    async ask(sessionID, permission, patterns, ruleset, ...)  # 阻塞
    reply(requestID, reply, message)     # 解锁
    set_approved_rules(rules)
```

三级策略（`config.approval.policy`）：

| 策略 | 行为 |
|---|---|
| `suggest` | 涉及 `fs:read/fs:write/shell:execute/tool` 权限时弹窗询问（CLI: y/a/s/n；Web: `/api/permissions/{id}/reply`） |
| `auto-edit` | 自动放行文件编辑，遇到 `shell` 工具仍拦截 |
| `full-auto` | 全部放行 |

通配符规则（`permissions/wildcard.py`）支持 `*` `?`、跨平台大小写（Windows 强制 case-insensitive）、`~/` 与 `$HOME/` 展开。

### 4.7 会话压缩（`session/compaction.py`）

**两层策略**：

1. **`prune_messages(messages, keep_last=2)`**：不调模型，把较早 turn 的 `role=tool` 内容折叠为占位符（`[old tool result pruned — tool: <name>]`），保护白名单 `PRUNE_PROTECTED_TOOLS = ["skill"]`。供 `/prune` 命令和 inline 使用。
2. **`compact_messages(messages, context_window, model_name, model_adapter, keep_last_assistant_turns=5)`**：调一次模型用 `CompactAgent` 摘要前 K 之前的对话；失败时降级为 `_simple_compact_split`（每条截断 200 字符）。

**磁盘-内存-LLM 三视图一致**：

```text
[磁盘 (chronological)]   = preserved_old + new_summary + ...to_keep
[LLM 视图]              = _llm_context() = new_summary + ...to_keep
[SessionState.history]  = _llm_context() （下次 chat 入口加载用）
```

`_apply_post_compact()` (server) 和 `_apply_compact_to_disk()` (runtime) 是同一逻辑的两份镜像实现——小心维护一致性。

**自动触发**：`AUTO_COMPACT_THRESHOLD = 0.9`（runtime.py），即 `last_usage.prompt_tokens / context_window >= 0.9` 时自动调用。

### 4.8 MCP 集成（`adapters/mcp/`）

**关键设计**：`server.py` 是 ~130 行自写包装（替代 openai-agents 的 ~2800 行 agents.mcp）。支持：

- 传输：`stdio` / `sse` / `streamable-http`
- 重试：`max_retry_attempts` + `retry_backoff_seconds_base` 指数退避（仅重试 transient：timeout/connect/HTTP 5xx）
- 错误映射：httpx → "connection lost / timed out / HTTP <code>"
- 抑制 `cancel scope in different task` 噪声（MCP SDK 的 stdio_client async generator 跨 task 清理）
- 进程 stderr 重定向到 `/dev/null`（`server.create_streams = _quiet_streams`）

**生命周期**（`MCPConnectStatus` 枚举，`shared_types.py`）：

```text
DISCONNECTED → CONNECTING → CONNECTED
                            ↘ NO_CONFIG (永久不重试)
                            ↘ FAILED    (下一轮重试)
                            ↘ CONNECTED 保持
```

**工具注册**：MCP 工具以 `mcp.<tool_name>` 注册到 `CapabilityRegistry`，可与原生 tool 混用。

### 4.9 存储（`storage/file_storage.py`）

```text
~/.ziva/
├── config.yaml                    # 全局配置
├── recent_workspaces.json
├── .locks/                        # fcntl 文件锁
├── automations/<pid>.json
└── sessions/<pid>/
    ├── <sid>.json                 # session meta (id, time, model_name, last_usage)
    ├── messages/<sid>.jsonl       # 消息 JSONL
    ├── attachments/<sid>/*.png    # 用户上传图片
    └── project.json               # 可选
```

- 写：先写 `<name>.<uuid>.tmp` 再 `rename` 原子替换。
- 读：上下文管理器 + 共享锁（`LOCK_SH`），generator 一次性物化再 yield 以快速释放锁。
- 关键 API：`create_session` / `update_session` / `get_session` / `list_sessions` / `delete_session` / `append_message` / `get_messages` / `replace_messages` / `update_message` / 自动化 CRUD。

---

## 5. 测试覆盖

### 5.1 测试文件清单（按模块）

| 类别 | 测试文件 |
|---|---|
| **ACP 协议** | `test_acp.py`, `test_acp_chunk_stream.py`, `test_acp_incremental.py`, `test_acp_process_stdio.py`, `test_acp_stream.py` |
| **Adapter/Provider** | `test_adapter_singleton.py`, `test_anthropic_usage.py`（脚本：test_anthropic.py, test_anthropic2.py） |
| **CLI / 进程** | `test_cli_e2e.py`, `test_process_e2e.py`, `test_repl.py` |
| **Config** | `test_config.py`, `test_config_model_fields.py`, `test_config_validation.py`, `test_approval_config.py` |
| **Desktop API** | `test_desktop_alignment.py`, `test_desktop_api.py`, `test_desktop_compact_usage.py` |
| **Image 处理** | `test_image_path_resolver.py`（22KB 大文件，覆盖度高） |
| **Instruction 加载** | `test_instructions_loader.py`, `test_instructions_integration.py` |
| **MCP** | `test_mcp_client.py`, `test_mcp_enum_lifecycle.py` |
| **Memory** | `test_markdown_memory.py` |
| **Plugin** | `test_plugin_loading.py`, `test_plugins.py`, `test_manifest_validation.py` |
| **Permissions** | `test_permission_gate.py`, `test_ask_user_no_timeout.py` |
| **Runtime** | `test_runtime_extensions.py`, `test_tool_loop.py`, `test_turn_failure.py`, `test_event_metadata.py`, `test_event_stream.py`, `test_retry_backoff.py` |
| **Session** | `test_session_compaction.py`（20KB，最大测试文件）, `test_session_switch_bug.py`, `test_session_switch_e2e.py`, `test_session_switch_model.py`, `test_multi_session_isolation.py`, `test_per_session_model.py`（14KB） |
| **Spawn Agent** | `test_spawn_agent_definitions.py`, `test_spawn_concurrency.py` |
| **Tool** | `test_apply_patch_tool.py`, `test_edit_tool.py`, `test_grep_tool.py`（10KB）, `test_read_file_tool.py`, `test_shell_tool.py`, `test_web_search_tool.py`, `test_write_file_tool.py`, `test_tool_call_protocol.py`, `test_update_plan_tool.py` |
| **Reasoning 字段** | `test_reasoning_field.py` |

### 5.2 覆盖评估

- **覆盖度高**：ACP 协议、Config 校验、Session 压缩、Tool 协议、Plugin manifest、Permissions、Image resolver、Retry。
- **中等覆盖**：Adapter（singleton 较多、真实 API 测试在 `scripts/test_real_api.py`）、Desktop API。
- **缺口**：
  - `adapters/anthropic/provider.py` 似乎没有专门的单元测试（仅 `test_anthropic_usage.py` 名字涉及 usage，但实际是否覆盖 provider 行为需确认）。
  - `transports/desktop_api/server.py` 1925 行只有 3 个测试文件。
  - `runtime.py` 1780 行核心逻辑只有少数 e2e 测试覆盖（多数通过 `_run_model_tool_loop` 间接覆盖）。
  - 端到端 UI/E2E 测试缺失（仅有 `test_ui_e2e.py` 单文件）。
  - 性能/并发压测缺失。
  - 测试受限于 `pytest -p no:capture`（README 提到 capture 插件在当前环境会 segfault），需要研究根因。

---

## 6. 代码质量观察

### 6.1 优点

- **类型注解全面**：`from __future__ import annotations` 普及；`Protocol`、`AsyncIterator`、`TypedDict`-like dataclass 用得到位。
- **文档字符串质量高**：关键方法都有大段 docstring 解释动机、边界、不变量（参见 `_sanitize_orphaned_tool_calls`、`_apply_post_compact`、`_run_model_tool_loop`）。
- **职责清晰**：Protocol + Registry 模式让扩展点显式。
- **可测试性**：`_create_adapter()` 是可缓存的纯函数，测试可通过 `_reset_adapter_registry()` 重置。
- **持久化安全**：FileStorage 用 fcntl + 原子 rename。
- **状态机明确**：MCPConnectStatus 用 5 态枚举替代 boolean，注释解释每种转换的语义。
- **优雅降级**：多处 `try/except` 包裹可疑行为并打日志（如 MCP cancel-scope 噪声）。
- **撤回 / 取消语义严谨**：`_sanitize_orphaned_tool_calls()` 解决 Anthropic "tool_use 不匹配 tool_result" 400 错误。
- **截图风格的工具注释**：`_ThinkTagParser` 等都用"为什么"而非"做什么"作为注释。

### 6.2 技术债与改进点

| 类别 | 问题 | 位置 | 建议 |
|---|---|---|---|
| **文件长度** | `runtime.py` 1780 行 / `server.py` 1925 行 / `cli.py` 516 行 | `runtime.py`, `transports/desktop_api/server.py`, `app/cli.py` | 拆分为 `runtime/core.py` + `runtime/tool_loop.py` + `runtime/compaction_hook.py` + `runtime/session_manager.py` 等。 |
| **类型安全** | `ctx.metadata: Dict[str, Any]` 任意塞数据，跨模块约定靠注释 | `shared_types.py::RuntimeContext` | 引入 typed `ContextMetadata` dataclass 或 `TypedDict`。 |
| **重复代码** | `_apply_post_compact` (server) 和 `_apply_compact_to_disk` (runtime) 是镜像实现 | `server.py:552` & `runtime.py:538` | 提取公共 helper 到 `session/compaction.py`。 |
| **测试基础设施** | pytest capture 插件 segfault，必须 `-p no:capture` | `README.md` | 排查 Python 3.x + pytest 8 的兼容问题，或在 `conftest.py` 默认禁用 capture。 |
| **CI 缺失** | 没有 `.github/`，没有 ruff/black/mypy 配置 | repo root | 添加 GitHub Actions 跑 `pytest` + `ruff` + `mypy`。 |
| **Prompt 注入** | README 末尾含外部粘贴的 chat log 试图改模型行为 | `README.md` | 清理并加 pre-commit 扫描（详见 §0）。 |
| **配置散落** | MCP/stt/agents 等配置项的 schema 校验没有完整 JSON Schema | `config/loader.py::validate_config` | 引入 `pydantic` v2 模型化配置。 |
| **日志策略** | REPL 模式下粗暴 `setLevel(WARNING)` 全局静音 | `app/cli.py::_repl_loop` | 改为 logger filter / 上下文隔离。 |
| **错误分类** | `_chat_stream_chunks` 的 `granularity` 没有 "sentence" 级别；tool_result 的错误信息散落不同前缀 | `protocols/acp.py`, `runtime.py` | 集中到 `errors.py` 枚举。 |
| **REPL 大量 if/elif** | `cli.py::_repl_loop` 是一长串 `if line == "/xxx"` 链 | `app/cli.py:264-383` | 改成 `dict[command, handler]` 注册表。 |
| **Magic numbers** | `AUTO_COMPACT_THRESHOLD = 0.9`、`K=5`、`max_rounds=10` 等硬编码 | `runtime.py` 多处 | 提升到 `DEFAULT_CONFIG`。 |
| **副作用 import** | 多个文件 `from . import` 在函数体里做 | `runtime.py` 等 | 移到模块顶部以利静态分析。 |
| **废弃 API** | `gpt-image-1` / `gpt-4.1` 等模型硬编码到默认 | `config/loader.py::DEFAULT_CONFIG` | 用 `provider.litellm` 风格的自动探测。 |

### 6.3 命名 / 注释

- 命名风格统一 snake_case，类 PascalCase，私有前缀 `_`。
- `_cancelled` / `_hidden` / `_compaction_summary` 等内部字段用下划线开头做命名空间隔离是良好实践。
- 部分函数用 `//` 注释说明"为什么"而非"是什么"（高质量）。

---

## 7. 桌面端集成

### 7.1 整体架构

```text
┌────────────────────┐         ┌──────────────────────┐
│   Electron 壳       │  IPC    │  Browser 面板         │
│   electron/main.ts  │◀──────▶│  (Chromium 内嵌)      │
│   electron/preload  │         │  web/dist/ 的 SPA     │
└─────────┬──────────┘         └──────────┬───────────┘
          │  spawn 进程                    │ fetch + SSE
          │  ziva-backend                  │
          ▼                                ▼
┌──────────────────────────────────────────────────────┐
│   Python 后端 (PyInstaller 打包为 ziva-backend)      │
│   aiohttp: 127.0.0.1:4097                            │
│   ┌──────────────────┐   ┌──────────────────────┐   │
│   │ REST routes      │   │ SSE /events (global) │   │
│   │ /sessions        │   │ /sessions/{sid}/evts │   │
│   │ /turns /cancel   │   │ /ws/terminal         │   │
│   │ /attachments     │   │ /api/proxy /api/files│   │
│   │ /config /skills  │   │ /api/stt /api/agents │   │
│   └──────────────────┘   └──────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

### 7.2 关键文件

| 文件 | 作用 |
|---|---|
| `electron/main.ts` | Electron 主进程；启动 Python 后端子进程；管理 BrowserWindow；`automationCallback` reload 钩子。 |
| `electron/preload.ts` | `contextBridge.exposeInMainWorld()` 暴露安全 API 给 renderer。 |
| `electron/cdp-bridge.ts` | 把 Chrome DevTools Protocol 桥接到 Python（让后端可控制浏览器面板）。 |
| `electron/ziva-backend.spec` | PyInstaller spec 文件，指定入口和打包内容。 |
| `web/src/main.ts` | UI 入口；事件路由；状态机。 |
| `web/src/api.ts` | REST 客户端封装。 |
| `web/src/sse.ts` | EventSource 客户端，多 session 事件路由。 |
| `web/src/state.ts` | 集中状态（sessions、turns、tool calls、plan、diff、mcp…）。 |
| `web/src/markdown.ts` | 渲染 assistant 文本（含 `<think>` 折叠）。 |
| `scripts/build-desktop.sh` | 调度 build:frontend → build:backend → electron pack。 |

### 7.3 通信机制

1. **进程拉起**：`electron/main.ts` 在 app ready 后 `child_process.spawn(PYTHON_BACKEND_BIN, ["desktop", "serve", "--port", "4097"])`，等待端口 ready 后再开 BrowserWindow。
2. **HTTP / SSE**：`web/src/api.ts` 用 fetch；`web/src/sse.ts` 维护一条 `/events` 长连接，事件按 `session_id` 字段 fan-out 到各 session 的状态切片。
3. **PTY 终端**：`/ws/terminal` 用 aiohttp WebSocket 桥到 `pty.openpty()` + `subprocess.Popen($SHELL)`，支持 resize（`fcntl.ioctl` + `TIOCSWINSZ`）。
4. **语音输入**：`/api/stt` 接收 multipart 音频 → `mlx-whisper` 转写（macOS GPU 加速）。
5. **跨 workspace 路由**：`_pid_for(sid)` 优先查 in-memory session.project_id，fallback 到 `~/.ziva/recent_workspaces.json` 里的历史工作目录，确保删除 sidebar 中其他项目的 session 仍能找到。
6. **打包**：`electron-builder` 把 `web/dist/*` 与 `dist/ziva-backend`(PyInstaller) 一起打成 dmg/zip。

### 7.4 web/ 提供的 UI 能力

- 会话侧栏（多 workspace 聚合）
- 多 session tabs / 全局 SSE 事件流
- 流式 assistant 文本 + reasoning 折叠
- 工具卡片（tool_start / tool_end）
- ask_user 选择卡
- 文件树 / 文件查看 / 终端面板（xterm.js）
- 自动化（Automations）调度
- 配置编辑（YAML / JSON 双视图）
- 技能浏览（从 `skill.extra_paths` 读 `SKILL.md` 索引）
- 后台 Agent 监控
- git 分支切换 / diff / revert

---

## 8. 可改进点建议（5 条具体建议）

1. **拆分 `runtime.py` 与 `transports/desktop_api/server.py`**

   - `runtime.py` 可拆为：
     - `runtime/__init__.py`（Runtime 入口）
     - `runtime/core.py`（create / shutdown / session manager）
     - `runtime/tool_loop.py`（`_run_model_tool_loop`）
     - `runtime/execution.py`（`_execute_tool` / 权限 / hooks / memory）
     - `runtime/events.py`（EventBus 绑定 + `_emit`）
   - `server.py` 拆为 `routes/sessions.py`、`routes/sse.py`、`routes/workspace.py`、`routes/automations.py`、`routes/config.py`、`routes/panels.py`（files/terminal/proxy/stt/agents）。
   - **收益**：可测试性（单文件 <500 行），可读性，并行 PR 友好。

2. **配置层迁移到 Pydantic v2**

   - `pyproject.toml` 加 `pydantic>=2.5`。
   - 把 `DEFAULT_CONFIG` 改成 `class ZivaConfig(BaseModel)`，并用 `model_dump()` 序列化、`deep_merge` 用专门的 `model_copy(update=...)`。
   - `validate_config` 改成模型级 `field_validator`。
   - **收益**：自动校验、自动补全（IDE）、schema 文档化、与 OpenAI/Anthropic SDK 类型生态兼容。

3. **统一 `chat()` 与 `chat_streaming()` 的双轨实现**

   - 现状：两套代码重复实现 history 加载、image 解析、auto-compact、orchestration。
   - 建议：`chat()` 改为订阅 `chat_streaming()` 的 event bus，丢弃 `delta` 事件即可拿到 final result，**单一真相**。
   - **收益**：减少 ~200 行重复，行为天然一致；以后改 model/tool 流程只改一处。

4. **补齐测试与 CI 基础设施**

   - 加 `.github/workflows/ci.yml`：lint (`ruff`)、typecheck (`mypy`)、`pytest` 三 job。
   - 加 `pyproject.toml` `[tool.ruff]` 和 `[tool.mypy]`，零容忍 unused import / 未类型化函数。
   - 解决 pytest capture segfault（先复现 → 提 issue → 临时用 `conftest.py` 加 `-p no:capture` 默认 → 长期切到 `pytest-xdist` 或升级 pytest）。
   - **缺失测试补齐**：
     - Anthropic adapter 流式 / 错误重试
     - Desktop API 路由表
     - MCP server 上线/下线 lifecycle
     - 自动化（cron / schedule_time / 取消）端到端
   - **收益**：进入可维护状态，避免后续大改引入回归。

5. **会话压缩逻辑去重 + LLM-visible 视图统一**

   - 把 `_apply_post_compact`（server.py:552）与 `_apply_compact_to_disk`（runtime.py:538）合并为 `session.compaction.persist_compact(sid, working_before, new_working, keep_last_k)`，让 server 和 runtime 调同一个函数。
   - 用 `compaction.py` 的 `compose_post_compact_on_disk` 作为唯一磁盘合并逻辑。
   - 配套：引入 `MessageView` 抽象，统一 `full_view` / `llm_view` / `ui_view` 三种 view 的 getter，避免"`_llm_context` 散落在 `chat()` / `chat_streaming()` / `_load_session_from_disk()` / `compact_session` 4 个地方"。
   - **收益**：杜绝"改了 disk layout 但忘改 in-memory"的 bug；`test_session_switch_bug.py` 那种回归不会再出现。

---

## 9. 总结

Ziva 是一个**架构清晰、协议先行、扩展点显式**的 Codex-like agent runtime。亮点：

- **统一扩展 API**（Protocol + Registry + Manifest + Plugin 目录约定）让加新 tool/skill/hook/memory 是写一个 manifest.yaml + 几行 Python。
- **ACP + Desktop API + REPL** 三个入口共享同一 Runtime 编排，避免逻辑分裂。
- **YAML 分层合并 + 严格校验** 配置层稳健。
- **多 provider 单 SDK 策略** 让 OpenAI/Anthropic/MCP 各自简单。
- **SSE 事件流** 与 `EventBus` 解耦得不错（per-session + global 双订阅）。
- **取消 / 撤回语义** 考虑周全（`_sanitize_orphaned_tool_calls` 解决 wire-format 约束）。
- **测试密度** 在核心模块上算厚（`runtime.py` 间接覆盖多，`session/compaction.py` 单测 20KB）。

主要风险：

- **两个大文件**（`runtime.py` 1780 行、`server.py` 1925 行）会持续累积复杂度。
- **CI 与 lint 缺失**，团队扩大时易失控。
- **README 含提示词注入** —— 安全红线，需要立刻清理。
- **测试覆盖在 Anthropic adapter、Desktop API 路由表上有缺口**。

按 §8 的 5 条建议按序推进，可在 1-2 个迭代内把项目带入生产可维护状态。
