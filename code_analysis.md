# Ziva 仓库代码分析报告

> **分析时间**：2026-06-25
> **分析对象**：`/Users/wangxinxin/code/ziva`（commit-on-disk 状态）
> **报告版本**：v1.0

---

## 0. TL;DR

Ziva 是一个**类 Codex 的智能体运行时**：Python 后端（`src/ziva_runtime/`）+ Electron 桌面壳（`electron/`）+ Vite/TS 前端（`web/`）+ 16 个内置插件（`plugins/`）+ 50+ 单元测试。提供三条入口：`run`（单轮 CLI）、`acp serve`（JSON-RPC over stdio）、`desktop serve`（HTTP + SSE + 静态 UI）。核心 Model ↔ Tool 循环、自动 compact、MCP 适配、sub-agent、权限审批、thinking 块、image 附件均已实现。本报告聚焦架构、数据流、代码质量与现存遗留问题。

---

## 1. 架构总览

### 1.1 三条入口链路

```
┌────────────────────────────────────────────────────────────────────┐
│  cli.main()  ─→  argparse (run|repl|acp serve|desktop serve)        │
│      │                                                              │
│      ├─ run        → Runtime.create() → runtime.chat_streaming()   │
│      ├─ repl       → Runtime + Rich TUI + _repl_loop()              │
│      ├─ acp serve  → ACPServer(runtime).serve_stdio (JSON-RPC)      │
│      └─ desktop    → DesktopAPIServer(runtime)  (aiohttp + SSE)     │
└────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
         Runtime (src/ziva_runtime/runtime.py)
         ├─ CapabilityRegistry  (tools / hooks / skills / memory / prompts)
         ├─ EventBus (per-session + global broadcast queues)
         ├─ FileStorage  (~/.ziva/sessions/<pid>/{*.json, messages/*.jsonl})
         ├─ PermissionManager + wildcard  (suggest / auto-edit / full-auto)
         └─ Adapter cache  (per (api_type,base_url,api_key))
                               │
                               ▼
   ┌──────────────┬──────────────┬──────────────┐
   │ OpenAIChat   │ AnthropicChat│ MCPClient    │
   │ Adapter      │ Adapter      │ (stdio/sse/  │
   │ (provider.py)│ (provider.py)│  http)       │
   └──────────────┴──────────────┴──────────────┘
```

### 1.2 模块依赖关系（核心 import 链路）

- `app/cli.py` → `runtime.py` + `protocols/acp.py` + `transports/desktop_api/server.py`
- `runtime.py` → `adapters/openai|anthropic/provider.py` + `adapters/mcp/client.py` + `capabilities/{registries,events}` + `config/loader.py` + `permissions/*` + `plugins/loader.py` + `session/compaction.py` + `storage/file_storage.py`
- `protocols/acp.py` → `runtime.py`（仅依赖 ChatMessage / Runtime 引用）
- `transports/desktop_api/server.py` → `runtime.py` + `permissions/*` + `storage/file_storage.py` + `session/compaction.py`
- `plugins/loader.py` → `plugins/manifest.py` + `capabilities/registries.py`
- 所有 adapter/transport 都依赖 `shared_types.py`（数据类中心）

### 1.3 子系统关键文件与行数

| 子系统 | 关键文件 | 行数 | 职责 |
|---|---|---|---|
| CLI 入口 | `src/ziva_runtime/app/cli.py` | 516 | argparse + REPL + ACP stdio serve + Desktop serve |
| CLI 渲染 | `src/ziva_runtime/app/display.py` | 104 | Rich 输出封装（暂未在 cli.py 中使用，独立模块） |
| 核心运行时 | `src/ziva_runtime/runtime.py` | **1788** | Runtime 类 + Model↔Tool 循环 + MCP 接入 + hooks + persistence |
| 共享类型 | `src/ziva_runtime/shared_types.py` | 154 | ChatMessage / ChatResult / ToolResult / StreamDelta / SessionState / CancellationToken |
| ACP 协议 | `src/ziva_runtime/protocols/acp.py` | 227 | JSON-RPC 2.0 over stdio，5 个 method |
| OpenAI 适配 | `src/ziva_runtime/adapters/openai/provider.py` | 338 | OpenAI SDK Chat Completions + 流式 + tool_calls 解析 |
| Anthropic 适配 | `src/ziva_runtime/adapters/anthropic/provider.py` | 335 | Anthropic SDK + thinking 块 + 多事件流 |
| 重试 | `src/ziva_runtime/adapters/retry.py` | 86 | MAX_RETRIES=2，429/5xx/529 + 内容敏感误判 |
| MCP 客户端 | `src/ziva_runtime/adapters/mcp/client.py` | 465 | MCPServerConfig + MCPToolWrapper + 多 transport 解析 |
| MCP 服务端 | `src/ziva_runtime/adapters/mcp/server.py` | 228 | 替代 openai-agents.mcp 的薄壳 stdio/sse/http |
| 配置 | `src/ziva_runtime/config/loader.py` | 244 | YAML 分层合并 + 严格 schema 校验 |
| 指令文件 | `src/ziva_runtime/config/instructions.py` | 24 | `~/.ziva/AGENTS.md` + workspace `.ziva/AGENTS.md` |
| 插件 manifest | `src/ziva_runtime/plugins/manifest.py` | 62 | `PluginManifest` 数据类 + 校验 |
| 插件 loader | `src/ziva_runtime/plugins/loader.py` | 74 | discover_manifests + load_plugins + enabled 规则 |
| 会话压缩 | `src/ziva_runtime/session/compaction.py` | 409 | CompactAgent + prune + estimate_tokens + INVARIANT 注释 |
| 文件存储 | `src/ziva_runtime/storage/file_storage.py` | 287 | fcntl 锁 + JSONL 消息 + 附件目录 |
| 桌面 HTTP+SSE | `src/ziva_runtime/transports/desktop_api/server.py` | **1939** | 50+ endpoint + automation + WS + 文件面板 + 语音识别 |
| Electron 主进程 | `electron/main.ts` | 191 | 子进程拉起 Python + IPC handlers + CDP 注册 |
| Electron preload | `electron/preload.ts` | 14 | `contextBridge.exposeInMainWorld` 暴露 5 个 invoke |
| CDP 桥接 | `electron/cdp-bridge.ts` | 624 | HTTP `/json/*` + WS `/devtools/*` + flatten true/false 双模式 |
| Web 入口 | `web/index.html` | — | 单页 SPA 入口 |
| Web 主控 | `web/src/main.ts` | ~5000+ (296KB) | 消息列表 + composer + 多面板 + slash 命令 + SSE 分发 |
| Web API | `web/src/api.ts` | 295 | `fetch` 封装 + 30s 超时 + 错误码规整 |
| Web SSE | `web/src/sse.ts` | 163 | 单连接 + 指数退避 + onReconnect 回调 |
| Web state | `web/src/state.ts` | 94 | 极简 Store + per-session 字典 |
| Web markdown | `web/src/markdown.ts` | 129 | marked + katex + prismjs + extractThinking |

---

## 2. 核心模块深入分析

### 2.1 `src/ziva_runtime/runtime.py`（1788 行）

- `Runtime.create(workspace_root, global_config_path, ...)` L397：工厂方法，load effective_config → load_plugins → set approved permissions
- `chat_streaming` L822：流式入口；emit turn_start → 调 `_run_model_tool_loop`
- `_run_model_tool_loop` L884-1223：**单方法 340 行**——核心 while 循环（auto-compact → build prompt → stream → 解析 tool_calls → 并发执行工具 → emit → loop）
- `_execute_tool` L1130+：deny list → sub-agent tool check → approval policy → before_tool hook → asyncio.wait_for → after_tool hook
- `_resolve_image_paths` 已抽出到模块级函数
- `_apply_compact_to_disk` L554 vs `_apply_post_compact`（server.py:552）—— 功能几乎相同，两份维护（详见 §8.1）
- `_build_environment_context` L1597 仍用旧 API `self._current_model_supports_image()`（注释 L1568 标 backward-compat alias，session 切到非 vision 模型时不会刷新 system prompt）

### 2.2 `src/ziva_runtime/app/cli.py`（516 行）

- `main()`：argparse 解析，路由到 `_run_async("run"/"repl"/"acp"/"desktop")`
- `_run_streaming` L141：末尾 strip `^…$`（含 `^think$`）剥 thinking 块——**第三份实现**（详见 §8.1.5）
- `_repl_loop` L187-413：交互循环
- L297-403 slash command 链：11 个 `elif`，无注册表；与 `web/src/main.ts:1094` SLASH_COMMANDS 表是**双份实现**

### 2.3 Adapter 深入

#### `adapters/openai/provider.py`（338 行）

- 流式响应增量解析 + `tool_calls` 数组聚合（部分 SDK 流式只给 delta，需要按 index 拼装 arguments）
- `reasoning_content` 兼容：用 `_REASONING_TAG_RE` 匹配 `^…^` 标签——把 MiniMax 等 provider 把 CoT 放在 `content` 里而不是 `reasoning_content` 的情况转成正确的 reasoning 流
- `extra_body` 注入 L240-247：provider-specific options（如 MiniMax `reasoning_split`）走 `extra_body`，避免被 OpenAI SDK 顶层校验拒绝
- 工具参数 schema 通过 `json.dumps(tc.function.arguments, ensure_ascii=False)` L37 转字符串

#### `adapters/anthropic/provider.py`（335 行）

- `_build_anthropic_messages` L11-116：tool message → `{role:"user", content:[{type:"tool_result",...}]}`；assistant message 把 thinking 块（仅在有真实 signature 时）放在 text 前；image_url data: → base64 source
- 流式 L222-336：`content_block_start / content_block_delta / content_block_stop / message_delta` 四事件全程跟踪 tool_use / thinking / text
- L248-249 `stream_ctx.__aenter__` 单独走 `call_with_retry`，**只有"打开连接"这一步会重试，开始读 stream 后失败不重试**（注释 L245-247）

#### `adapters/retry.py`（86 行）

- `MAX_RETRIES = 2` L17（3 次总尝试）
- `RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}` L18
- `_RETRYABLE_CONTENT_MARKERS = ("1027", "new_sensitive", "1026", "input_sensitive")` L26-28（针对 Anthropic-via-proxy / DeepSeek 的"内容敏感"误判）
- `equal-jitter` 退避 L65：`random.uniform(0.8, 1.2) * BASE_DELAY_MS * 2^attempt`

#### `adapters/mcp/client.py`（465 行）

- `MCPServerConfig` L16-28（name/command/args/url/transport/environment/headers/cwd/timeout/max_retry_attempts/retry_backoff_seconds_base）
- `MCPToolWrapper` L31-100：把 MCP tool 注册成 Ziva Tool plugin，run 时通过 `ctx.metadata["_runtime"]._get_session(...)` 取 session → 取 `session.mcp_client` → `server.call_tool(name, args)`
- `mcp_call_result_to_tool_result` L237-267：覆盖 text / image / audio / resource / resource_link / structured_content / isError 等所有 MCP content 类型
- `parse_mcp_config` L438-465：兼容三种配置形态：dict（`{name: cfg}`）、list（`[{name, command, ...}]`）、legacy `mcpServers`

#### `adapters/mcp/server.py`（228 行）

- 极薄替代 `openai-agents.mcp`（注释 L1-19 详述动机）
- `MCPServerStdio / MCPServerSse / MCPServerStreamableHttp` L175-228，三者唯一差异是 `_create_streams`
- `MCPServer._create_streams` L95-96 是 abstract：`raise NotImplementedError`，由子类覆盖
- L31-39 `_swallow_cleanup_noise` 识别 anyio cancel-scope 噪音；L160-172 cleanup 时拆 ExceptionGroup 静默处理

### 2.4 `src/ziva_runtime/config/loader.py`（244 行）

- `DEFAULT_CONFIG` L9-54：完整的 fallback config（含 `providers`、`agents: {explore, plan}` 默认 agent 定义）
- `_deep_merge` L57-64：递归 dict merge（list 整体替换，不 concat）
- `validate_config` L83-227：**严格 schema 校验**，例如 L102-107 `thinking_budget_tokens < max_tokens`（Anthropic 硬约束）；L121-126 capabilities 只允许 `thinking/vision/tools` 三个键；L213-223 `agents.{name}.hooks` 必须是 4 个合法 hook 类型之一
- `load_effective_config` L229-244：合并顺序 `DEFAULT_CONFIG → ~/.ziva/config.yaml → session_override`，**只信任这一个全局文件**，忽略 workspace 下的 `.ziva/config.yaml`（注释 L232-237）

### 2.5 `src/ziva_runtime/plugins/`

- `manifest.py`（62 行）：`PluginManifest(id, type, version, entry, config, permissions, enabled_by_default, path)`；强制 `id` 含 `.`、`entry` 形如 `module.py:Symbol`
- `loader.py`（74 行）：
  - `TYPE_DIRS` L11-17：`tool/skill/hook/memory/prompt` → 子目录名
  - `discover_manifests` L31-45：扫 `*/manifest.yaml` + 检查 `manifest.type` 与目录名一致
  - `load_plugins` L48-73：依 manifest 决定 `enabled_by_default` + `memory.backend` / `tools.<id>.enabled` 匹配

**Hook 实际只支持 4 个 lifecycle**（`before_turn / after_turn / before_tool / after_tool`），plugin manifest 的 `event_name` 必须四选一。

### 2.6 `src/ziva_runtime/session/`、`storage/`

#### `session/compaction.py`（409 行）

- `CompactAgent` L20-44：`max_iterations=1` 一次性总结
- `COMPACTION_TEMPLATE` L46-71：中文模板（目标 / 指令 / 发现 / 已完成 / 相关文件）
- `estimate_tokens` L85-95：**启发式**——CJK 2 char/token、ASCII 4 char/token、每消息 +10 overhead
- `prune` L98-142 + `prune_messages` alias：保留最近 K 个 user 消息，替换老 tool output 为 `[old tool result pruned — tool: <name>]` placeholder
- `_pruned_tool_message` L145-181：兼容 dataclass / pydantic v2 model_copy / 普通 clone / fresh ChatMessage 4 种 fallback
- `_llm_context` L197-213：返回 `[last_summary, ...messages_after_it]`
- `compact_messages` L235-303：**核心**——按 asst-turn 切分，调 CompactAgent 总结前段；失败 fallback 到 `_simple_compact_split`（200-char 截断）
- `compose_post_compact_on_disk` L330-366：拼 `[preserved_old, new_summary, ...to_keep]`
- L108-111 / L266-271 两处大段 INVARIANT 注释：**`role="assistant"` 必须与一次 model call 1:1 对应**——任何将来把多 asst 合并的改动都会破坏切分逻辑

#### `storage/file_storage.py`（287 行）

- `_ZIVA_DIR = Path.home() / ".ziva"` L13
- 路径布局：`~/.ziva/sessions/<project_id>/{*.json, messages/<sid>.jsonl, attachments/<sid>/}`
- `_lock` L59-70：基于 `fcntl.flock`，lock file 放 `~/.ziva/.locks/`
- `append_message` / `replace_messages` 都持 exclusive lock；`get_messages` 默认 shared lock，可 `locked=True` 跳过（caller 自管）
- `delete_session` L133-148：删 JSON + JSONL + attachments 目录
- `list_automations` L235-248：兼容 list 和 `{automations:[...]}` 两种盘上格式
- `_project_hash` L16-18：取 `workspace_root` 的 sha256 前 16 字符

---

## 3. 桌面端（Electron）

### 3.1 `electron/main.ts`（191 行）

- `getBackendCommand()` L14-35：dev 模式 `python3 -m ziva_runtime desktop serve`，packaged 模式从 `process.resourcesPath` 拉 PyInstaller 产物 `ziva-backend(.exe)`
- `startPythonBackend()` L37-85：通过 stdout/stderr "Running on" / "started" 关键字判定启动，5s fallback
- `createMainWindow()` L87-111：`webPreferences.preload = preload.js`、`contextIsolation: true`、`nodeIntegration: false`、`webviewTag: true`
- IPC handlers L137-158：`get-backend-url` / `is-electron` / `get-cdp-port` / `register-cdp-page` / `unregister-cdp-page`
- L148-152 **CDP 桥接关键点**：renderer 把 webview 的 `wcId` 传给 main，main 调 `cdpBridge.addPage(wc, {type:"page"})` 拿到 `targetId`，renderer 记下用于 chrome-devtools-mcp 的 `--browser-url`

### 3.2 `electron/preload.ts`（14 行）

`contextBridge.exposeInMainWorld("electronAPI", {...})` 仅暴露 5 个 invoke 接口。**这是 renderer 唯一能与 main 通信的通道**。

### 3.3 `electron/cdp-bridge.ts`（624 行）

- **为什么不用 `--remote-debugging-port`**：会暴露整个 main BrowserWindow（含 Ziva UI 自己）。本桥只暴露 renderer 主动注册的 webContents（agent-browser 面板等），权限模型更安全。
- 协议面 L18-40：HTTP `/json/version /json/list /json/protocol`、WS `/devtools/browser`（Target domain）、WS `/devtools/page/<id>`（直接 page 级）
- 同时支持 `flatten:true`（Puppeteer 新版）和 `flatten:false`（老版 legacy wrapper）
- `dispatchEvent` L524-548：把 webContents 的 CDP 事件广播给**所有订阅者**（多客户端共存）
- `attachDebugger` L482-494 幂等（`attachedPages` set）；L496-504 detach 时容错
- L188-189 `detach()` 在 stop 时同步清掉所有 page

**职责划分清晰**：main.ts 管子进程 + IPC；preload.ts 管 contextBridge；cdp-bridge.ts 是纯网络层（不依赖 Electron 内部状态以外的 webContents）。三者通过 IPC 字符串契约耦合。

---

## 4. Web 前端（`web/src/`）

| 文件 | 行数 | 职责 |
|---|---|---|
| `main.ts` | ~5000+ (296KB) | UI 主控：消息列表、composer、右侧面板（review/plan/terminal/browser/files/agent-browser）、skills browser、splits、slash command 菜单、SSE 事件分发 |
| `api.ts` | 295 | `fetch` 封装 + 30s 默认超时 + 错误码规整（`http_500` / `error_code` / `status`） |
| `sse.ts` | 163 | **单全局 SSE 连接** + 指数退避重连（BASE=1s、MAX=10s、MAX_RETRIES=50）+ `onReconnect` / `onPermanentDisconnect` 回调 |
| `state.ts` | 94 | 极简 Store（`Set<Listener>` + `get/set/subscribe`） |
| `markdown.ts` | 129 | `marked` + `katex`（行内 + 块级 `$…$` / `$$…$$`）+ `prismjs` 高亮 + copy button + `extractThinking` 拆 `^…^` |

**关键设计**：
- `state.ts` 的 `AppState` 用 **per-session 字典** 存 `runningSessions / pendingMessages / promptDrafts / compactingSessions`，避免 session 切换时 UI 状态互相串扰
- `sse.ts` 把 N 个 session 合并到**一条 SSE 连接**（`/events` global broadcast），前端按 `session_id` 字段路由——替代了原来的 per-session SSEPool，避免 N 条连接把浏览器 tab 拉死
- `main.ts:1094-1107` SLASH_COMMANDS 表（与 CLI 的 elif 链**双份实现**，需保持一致）

---

## 5. 插件形态

### 5.1 `plugins/hooks/`（3 个，全部 enabled_by_default:true）

| 插件 | Manifest | impl.py 行数 | 作用 |
|---|---|---|---|
| `doom_loop` | hook.doom_loop | 44 | `after_tool` 钩子，相同 (tool_name, args_hash) 累计 ≥3 次在 tool output 末尾追加 `<reminder>` 提示 |
| `file_guard` | hook.file_guard | 43 | `before_turn` 钩子，检测 user 消息含 `image_url` 时追加文本 hint（劝模型别无脑调 image tool） |
| `truncation` | hook.truncation | 86 | `after_tool` 钩子，超 30k/20k/50k 字符的 shell/grep/web_fetch 输出落盘到 `<workspace>/tmp/tool_output_*.txt`，返回 head+tail 预览（`_UNLIMITED = {"read_file"}`） |

### 5.2 `plugins/memory/`（2 个）

| 插件 | 默认启用 | 存储位置 | 接口 |
|---|---|---|---|
| `inmemory` | true | 进程 dict | `put / search（子串）/ summarize` |
| `markdown` | false（需 `memory.backend: markdown`） | `~/.ziva/memories/<key>.md`（YAML frontmatter + 字段 sections） | 同上，但 search/summarize 都基于小写子串匹配 |

### 5.3 `plugins/tools/`（16 个）

| 工具 | 权限 manifest | 作用 | 关键约束 |
|---|---|---|---|
| `ask_user` | `agent: [ask]` | 阻塞式询问，per-session future | 单 round 阻塞；multi_select 支持 |
| `spawn_agent` | `agent: [spawn]` | 启动子 agent（foreground/background） | 子 agent 不能嵌套 spawn；可走预定义 agent（`config.agents.explore/plan/...`） |
| `get_agent_result` | `agent: [spawn]` | 同步等 background agent（block + timeout 上限 600s） | |
| `cancel_agent` | `agent: [spawn]` | 取消 background agent（task.cancel + 标志位） | |
| `shell` | `shell: [execute]` | 子进程执行 + 实时输出 | 默认 30k 截断由 truncation hook 处理 |
| `edit_file` | `fs: [read, write]` | 全量/局部文件编辑（带 diff） | |
| `write_file` | `fs: [write]` | 整文件写 | |
| `read_file` | `fs: [read]` | 读文件（offset/limit） | |
| `list` | `fs: [read]` | 列目录 | |
| `glob` | `fs: [read]` | glob 模式匹配 | |
| `grep` | `fs: [read]` | ripgrep 替代 | 20k 截断 |
| `web_fetch` | `web: [fetch]` | HTTP GET + HTML 转 markdown | 50k 截断 |
| `web_search` | （MCP/外部注入） | 默认走 MiniMax web_search | |
| `read_skill` | `skill: [read]` | 读 SKILL.md 内容 | |
| `update_plan` | `plan: [update]` | 更新 todo 列表 | UI 持久化在 session |
| `manage_scheduled_tasks` | `automation: [manage]` | 调度任务 CRUD | |
| `_shared/diff_utils.py` | — | 共享 diff 工具 | |

**所有工具类都遵循同一个协议**：`spec() -> dict(name, description, input_schema)` + `async run(input_data, ctx) -> ToolResult(text, images, error, metadata)`。见 `capabilities/interfaces.py:12-14`。

---

## 6. 数据流

### 6.1 端到端调用链（CLI `run "fix the bug"` → 输出回答）

```
1. cli.main() → asyncio.run(run_async(["run", "fix the bug"]))
2. run_async → build_parser → 解析 args
3. → _runtime_for_workspace(workspace)
      → Runtime.create(workspace_root, global_config_path)
        → load_effective_config(读取 ~/.ziva/config.yaml → 校验)
        → load_plugins([workspace/plugins], registry)  // tools/hooks/memory
        → load_plugins([~/.ziva/skills, ~/.agents/skills], registry)  // skills
        → PermissionManager.set_approved_rules(from_config(permissions))
4. → messages = [ChatMessage(role="user", content="fix the bug")]
5. → _run_streaming(runtime, messages, session_id=...)
      → 注册 perm_manager.on_pending → 全 auto-approve
      → 收集所有 model_response.content → 拼成最终输出
6. → runtime.chat_streaming(messages, session_id=...)
      → _get_session(sid) → lazy load disk JSONL → append new user msg → persist
      → _run_hooks("before_turn", ...)  // file_guard hook 注入 image hint
      → yield {"type": "turn_start", "session_id": sid}
      → 调 _resolve_image_paths（按 session.model_name 的 vision 能力分支）
      → _run_model_tool_loop(rendered_messages, sid, ctx, ...)
```

### 6.2 Model ↔ Tool 循环内部（`_run_model_tool_loop` L884-1223）

```
while max_rounds is None or round_idx < max_rounds:
  ┌── 取消检查 (CancellationToken)
  │
  ├── 0.9*context_window 触发 auto-compact
  │     ├── ≥5 个 asst-turn → compact_messages → apply to disk + reset last_usage
  │     └── 跳过（保留 silent 等待后续轮）
  │
  ├── 构建 effective_prompt:
  │     base_prompt + instructions + env_context + skill_index
  │
  ├── 解析 thinking_config（按 per-model capability）
  │
  ├── stream = model_adapter.chat_stream(working, model, prompt, tools, thinking)
  │     收集: full_content, full_reasoning_content, final_tool_calls, final_usage, finish_reason
  │
  ├── if not final_tool_calls:
  │     → emit model_response / round_complete → persist assistant → return
  │
  ├── else (有 tool calls):
  │     emit tool_start × N
  │     tool_results = asyncio.gather(*[self._execute_tool(tc, ctx) for tc in final_tool_calls])
  │       _execute_tool 内部：
  │         deny list check → sub-agent tool check → approval policy
  │         → before_tool hook
  │         → asyncio.wait_for(tool.run(...), timeout)
  │         → after_tool hook（doom_loop / truncation）
  │     emit tool_end × N
  │     追加 tool_msg + image_msg 到 working
  │     emit round_complete
  │     loop back
```

### 6.3 Tool Call 协议（实际实现 vs README）

**重要发现**：README 与 docs/agent-contracts.md 提到的两种格式

```
[[TOOL_CALL]]{"name":"echo","arguments":{"text":"hello"}}[[/TOOL_CALL]]
TOOL_CALL echo {"text":"hello"}
```

**在生产 runtime 代码里**完全没实现 parse 逻辑——`grep -r "TOOL_CALL" src/ziva_runtime/` 0 命中。运行时用的是 **SDK 原生 tool_calls 数组**（OpenAI `choice.message.tool_calls` / Anthropic `content_block.type=="tool_use"`），由各 adapter 解析成 `ToolCallItem(id, name, arguments)`。

`TOOL_CALL …` 字符串格式**只在 `scripts/test_real_api.py` / `scripts/smoke_test.py` / 6 个测试用例**里出现，作为 fake adapter 喂给 runtime 的字符串输入。换句话说，这种"文本协议"是测试用的烟雾弹，**生产代码不需要解析它**。README 在误导。

### 6.4 ACP 错误分类（5 个错误码 + classification）

```
-32700  parse_error      → JSON 解析失败（_serve_stdio L218）
-32601  method_not_found → handle() 未识别的 method（L52）
-32602  invalid_params   → messages 为空 / stream_id 缺失（L57/L73/L94/L113/L116）
-32602  invalid_stream   → stream_id 不在 _streams 字典里（L116）
-32000  无 business code → CDP 桥接层用，runtime 不涉及
```

所有 ACP 错误都附带 `error.data.classification` 字符串，遵循 README L41-48 的 schema。

### 6.5 SSE 事件流

事件类型枚举（来自 `_run_model_tool_loop` / spawn_agent / ask_user / automation）：

| type | 来源 | 字段 |
|---|---|---|
| `turn_start` | `chat_streaming` L827 | session_id |
| `turn_end` | `chat_streaming` L882 | session_id |
| `turn_error` | `chat_streaming` L879 | session_id, error, class |
| `turn_cancelled` | `chat`/`chat_streaming` | session_id |
| `status` | auto-compact trigger L967 | content:"compact", round |
| `context_compacted` | auto-compact 成功 L985 | round |
| `delta` | stream text L1058 | content, round |
| `reasoning_delta` | stream reasoning L1050 | content, round |
| `usage_update` | stream usage L1073 | usage |
| `model_response` | 每轮结束 L1082 | content, usage, finish_reason, round |
| `round_complete` | 每轮完成 L1093/L1209 | round, latency_ms, usage |
| `tool_start` | 工具执行前 L1118 | round, tool, arguments, call_id |
| `tool_end` | 工具执行后 L1160 | round, tool, arguments, output, error_class, call_id |
| `tool_not_found` | 工具未注册 L1171 | round, error_class |
| `permission_request` | perm_manager.ask emit L1441 | request info |
| `ask_user_question` | ask_user tool L93 | call_id, question, options, multi_select |
| `subagent_start` / `subagent_end` | spawn_agent L146/L183/L218 | call_id, agent_id, task, background, status, tools_used |
| `automation_run` | automation runner L327 | automation_id, name, scheduled, status, error? |
| `stream_reset` | chat_streaming retry L872 | attempt, reason, class |
| `cancelled` | 每轮入口 L945 | round |

每个 event 都通过 `_emit()` L1311-1317 注入 `session_id / seq / ts`（前端按 seq 排序），global broadcast queue 让 `/events` 一条连接就能接收所有 session。

---

## 7. 测试覆盖（`tests/`）

**53 个测试文件**，覆盖范围按主题分组：

### 7.1 协议 / ACP（5 个）
`test_acp.py`、`test_acp_chunk_stream.py`、`test_acp_incremental.py`、`test_acp_process_stdio.py`、`test_acp_stream.py` —— ACP 各 method 的 chat / chat_stream / chat_stream_chunks 分支。

### 7.2 工具插件（10+ 个）
- `test_apply_patch_tool.py`：**已孤儿化**（见 §9.3）
- `test_edit_tool.py`、`test_read_file_tool.py`、`test_write_file_tool.py`、`test_grep_tool.py`、`test_shell_tool.py`、`test_web_search_tool.py`、`test_update_plan_tool.py`

### 7.3 Runtime 核心（10+ 个）
- `test_acp.py` / `test_tool_loop.py` / `test_tool_call_protocol.py` —— Model↔Tool 循环 + 协议
- `test_session_compaction.py`（19KB）—— 压缩 / prune / noop / 边界 case
- `test_session_switch_bug.py` / `test_session_switch_e2e.py` / `test_session_switch_model.py` / `test_multi_session_isolation.py` / `test_per_session_model.py`（16KB）
- `test_runtime_extensions.py` / `test_adapter_singleton.py` / `test_retry_backoff.py`

### 7.4 配置 / 校验（5 个）
`test_config.py`、`test_config_model_fields.py`、`test_config_validation.py`、`test_instructions_integration.py`、`test_instructions_loader.py`、`test_manifest_validation.py`

### 7.5 桌面 API（3 个）
`test_desktop_api.py`、`test_desktop_alignment.py`、`test_desktop_compact_usage.py`

### 7.6 权限 / 事件 / 插件加载（8 个）
`test_permission_gate.py`、`test_event_metadata.py`、`test_event_stream.py`、`test_plugin_loading.py`、`test_plugins.py`、`test_markdown_memory.py`、`test_approval_config.py`、`test_ask_user_no_timeout.py`、`test_get_agent_result_no_timeout.py`

### 7.7 流程 / 推理（4 个）
`test_reasoning_field.py`、`test_spawn_concurrency.py`、`test_spawn_agent_definitions.py`、`test_image_path_resolver.py`（21KB，最大单测）

### 7.8 测试缺口（明显）

| 缺口 | 备注 |
|---|---|
| `tests/test_cli_e2e.py` / `test_process_e2e.py` | 仅 `subprocess` 拉起 `cli.py run`，没有覆盖 desktop serve / acp serve 的 end-to-end |
| `electron/` 全无测试 | cdp-bridge.ts 624 行的协议逻辑（flatten true/false 两套）0 覆盖 |
| `web/src/*.ts` 全无测试 | sse.ts 重连逻辑、state.ts store、main.ts 渲染流程 |
| `transports/desktop_api/server.py`（1939 行） | 50+ endpoint 大部分靠 `test_desktop_api.py` 1 个 smoke test 覆盖 |
| `adapters/anthropic/provider.py` | 无独立测试 |
| `permissions/manager.py` | 仅 `test_permission_gate.py`，三层规则交叉的复杂场景未覆盖 |
| `_resolve_image_paths` | 仅 `test_image_path_resolver.py`，但 `test_desktop_alignment.py` 6KB 可能覆盖 vision 分支 |

### 7.9 `conftest.py` 关键 fixture

```python
@pytest.fixture(autouse)
def _reset_adapter_registry():
    """Clear the module-level adapter cache before each test."""
@pytest.fixture(autouse)
def _register_test_echo_tool(monkeypatch):
    """Inject a test-only 'echo' tool so legacy tests can run without network."""
```

注意 `_reset_adapter_registry` 是 autouse：所有测试都先清缓存再跑，避免 base_url/api_key 相同的 adapter 跨测试复用。

---

## 8. 代码质量观察（基于实际阅读）

### 8.1 代码异味

1. **`runtime.py` 1788 行的"上帝类"`**：Runtime` 同时持有 `_get_session`、`_run_model_tool_loop`、`_connect_mcp_if_needed`、`_emit`、`_resolve_image_paths`（虽然 extracted 成模块级函数）、`_apply_compact_to_disk`、`_build_environment_context`、`await_user_answer`、`set_user_answer`、`cancel_all_questions`、`_execute_tool`、`build_skill_index`、`_sanitize_orphaned_tool_calls`、`_load_session_from_disk`、`_persist_message`、`update_session_usage`、`list_sessions`、`shutdown` 等等。`_run_model_tool_loop` 单方法 **340 行**（L884-1223）。

2. **`desktop_api/server.py` 1939 行的"全能 HTTP server"`**：1 个 class 里有 50+ `async def` endpoint + automation 调度 + SessionStore + 文件系统面板 + 终端 WS + 语音识别 + git checkout + 自动 reload。**没有任何路由分组**（无 `web.add_routes([...])`），全部 inline 在 `_setup_app()` L175-237。

3. **`web/src/main.ts` 296KB`**：1 个文件干了全部 UI 逻辑（消息渲染、composer、splits、slash command 菜单、skills browser、image preview、queue 状态等）。函数级别的 `bindEvents()`（L1305）必然包含 1000+ 行。

4. **`cli.py:297-403` 的 slash command 链**：11 个 `elif`，每个直接 `console.print(...)`，没有注册表。`web/src/main.ts:1094` 的 SLASH_COMMANDS 表是**第二份实现**——任何一边改了另一边都不知道。

5. **`_run_streaming` 末尾的 `^…^` 剥离**（cli.py:141）**和** `web/src/main.ts:50 stripThinking()` **和** `markdown.ts:119 extractThinking()` **是第三份实现**。3 个地方各自处理 thinking 块提取，且正则略不同。

6. **adapter `_chatmessage_to_record` (runtime.py:527) 和 `_persist_message` (runtime.py:1729) 有重复的 ChatMessage → dict 序列化代码**（仅差在 `is_subagent / sub_call_id` 字段）。

7. **`_apply_post_compact`（server.py:552）和 `_apply_compact_to_disk`（runtime.py:554）功能几乎相同**：都做 on-disk replace + 更新内存 session.history + 重置 last_usage。两份维护必然漂移。

8. **`runtime.py:1597` `_build_environment_context` 使用过时的 `self._current_model_supports_image()`**：该方法 L1568 注释明确"backward-compat alias"，本类其它代码（L721）已迁移到 per-model 版本。这里没跟上是隐蔽 bug：session 切到非 vision 模型时，system prompt 的 `supports_image:` 字段仍显示旧模型的 vision 状态。

### 8.2 错误处理一致性

- `tool_not_found / permission_denied / permission_rejected / permission_error / timeout / cancelled / mcp_unavailable / mcp_not_configured / mcp_connect_failed / mcp_not_connected / mcp_server_not_found / mcp_timeout / mcp_call_failed / mcp_connection_lost / recursive_forbidden / spawn_agent_unavailable / missing_task / unknown_agent / missing_agent_id / agent_not_found / agent_not_running / missing_question / no_runtime / runtime_unavailable` —— **错误代码字符串非常一致**（都是 `Error: <code>\n<message>` 格式），由各 tool impl 各自构造。`ToolResult.text` 即错误载体，`error=True` 标志在结构化字段上。
- ACP 错误有 `code`（数字 JSON-RPC）+ `classification`（人类可读字符串）双层。
- HTTP endpoint 错误用 `web.json_response({"error": "code_string", "message": "..."}, status=...)`。
- 3 种错误表达共存，靠调用方识别——存在自动化测试覆盖，但缺一份"错误码字典"文档。

### 8.3 类型 / 接口清晰度

- **正面**：`capabilities/interfaces.py` 用 `Protocol` 定义了 Tool/Skill/Hook/MemoryStore/PromptProvider 5 个接口；`shared_types.py` 用 dataclass 集中所有跨模块数据结构。
- **负面**：
  - `runtime.py` 大量函数返回 `Dict[str, Any]` / `list[dict]`（如 `_apply_prompt`、`_build_tools_param`、`_load_session_from_disk`），调用方拿到 `dict` 后靠字符串 key 访问。
  - `event` 是裸 `Dict[str, Any]`，每个 type 对应不同字段，全靠注释 + 测试保证。
  - `desktop_api/server.py` 里多个 endpoint 直接返回 `web.json_response(self.runtime.config)` —— `Runtime.config` 是 `Dict[str, Any]`，序列化输出由用户配置直接决定，没有 schema 校验。
  - `PluginManifest.permissions` 是 `Dict[str, Any]`，实际值是 `{"fs": ["read", "write"], "shell": ["execute"], ...}` 这种结构——未类型化。

### 8.4 文档 / 注释质量

- **整体水平偏上**：`runtime.py` 中关键函数都有 10-30 行 docstring 解释为什么这么做（如 `_resolve_image_paths` 78 行注释，`_sanitize_orphaned_tool_calls` 60 行注释，`compact_messages` 38 行注释）。
- `session/compaction.py` 顶部有 INVARIANT 注释（L108-111、L266-271）明确警告未来重构方向。
- `transports/desktop_api/server.py` 事件处理函数大部分有 5-15 行注释说明边界 case。
- `electron/cdp-bridge.ts` 顶部 40 行注释解释了为什么不用 `--remote-debugging-port` 和 flatten 双模式设计——非常清晰。
- **缺口**：
  - 没有 ADRs / 架构决策记录（在 `docs/` 只有 3 个零碎 markdown：`agent-contracts.md` 是角色分工约定、`collaboration-plan.md`、`plans/2026-05-22-ziva-architecture-design.md`）。
  - 没有 API reference：`/sessions/{sid}/turns`、`/compact`、`/prune`、`/agents/{aid}` 等 50+ endpoint 没有公开文档。
  - `README.md` 的"tool call protocol"段落（README L24-35）与代码实际行为不符（见 §6.3）。

---

## 9. 遗留问题（来自顶层遗留文件 + grep 实测）

### 9.1 顶层遗留文件

| 文件 | 状态 | 关键问题 |
|---|---|---|
| `task_plan.md` | **个人调研规划** | 与代码完全无关——是用户在 2026-06-13 规划的"AI Agent 框架学习调研"（LangChain/AutoGen/CrewAI/MCP 等框架对比的 5 阶段调研），待决问题 4 项未开始，调研方向未确认。**应作为用户私人文件，不属于仓库内容**。 |
| `findings.md` | 同上 | 仅写了 Phase 1 的核心概念 + 8 框架初始笔记，Phase 3-6 待填充。**不属于代码库**。 |
| `progress.md` | 同上 | 2026-06-13 单条进度日志。**不属于代码库**。 |
| `kimi-compact-issue.md` | **未解决问题** | 用 kimi-k2.6 模型时 `/compact` 报错 `262144 (requested: 265358)`；Claude Code 的 auto-compact 不支持第三方模型；状态列了 2 个未勾待办。**当前仓库的 `AUTO_COMPACT_THRESHOLD = 0.9` 缓解但未根治**——若 model 上下文 = 200k 而 compact 调用本身就需要接近 200k，仍可能撞上限。 |
| `test-results-2026-06-20.md` | 上一轮手动测试记录 | 7 个 query + MCP 复验全部通过，但仅是 2026-06-20 一次快照。 |
| `backend.log` / `backend_test.log` | 空文件 | 0 bytes，未在 .gitignore 排除的迹象（建议加入）。 |
| 顶层散落的 `*.png`（NVDA/tsla/solar/weibo/douyin 等 13+ 个）、`*.txt`、`solar_system.html`（23KB + 16KB 两份）、`test_anthropic*.py`、`tsla_analysis.py`、`normal_distribution.py`、`test_ui_e2e.py`、`CFG`（神秘空文件） | **临时调试文件** | 与 ziva 项目无关，明显是从 douyin_hot/weibo_hot/NVDA 分析等一次性工作遗留。建议清理 + 加 `.gitignore`。 |

### 9.2 代码层未解决的 TODO / FIXME

通过 grep `TODO/FIXME/HACK/XXX` 在 `src/`, `plugins/`, `electron/`, `web/` 内的 Python/TypeScript 源文件：**0 命中**。这反而是好事——代码不留 TODO，技术债以"双份实现/版本漂移"的形式存在（如 §8.1 所列），而不是显式标记。

### 9.3 测试与生产代码漂移

| 漂移项 | 测试引用 | 实际状态 | 影响 |
|---|---|---|---|
| `apply_patch` 工具 | `test_apply_patch_tool.py`（115 行，6 个 case） + `test_plugin_loading.py:16` + `test_plugins.py:15` 全部断言 `"apply_patch" in tool_names` | `plugins/tools/apply_patch/` **不存在**（`glob **/apply_patch` 0 命中） | **这 3 个测试套件一旦运行就会失败**。`apply_patch` 被 `edit_file` 完全替代了但测试未清理。 |
| README 的 TOOL_CALL 文本协议 | README §Tool call protocol、docs/agent-contracts.md §Shared invariants、6 个测试 fixture 用 TOOL_CALL 字符串 | 运行时无 parse 逻辑 | 文档与代码不一致；测试用 fake adapter 喂字符串但实际模型走 SDK 原生 tool_calls |

### 9.4 已知 bug（代码中可见的补丁痕迹）

- `runtime.py:749-758` "Cancel may have fired between the assistant tool_calls being persisted (line 848) and the tool_result messages being written (lines 909/920)" —— 注释说明修过一次，但 `_run_model_tool_loop` 经过重构后**注释里引用的行号 848/909/920 已不再是当前实际行号**。注释陈旧。
- `runtime.py:1597` `_build_environment_context` 用了旧 API（见 §8.1 #8）。
- `runtime.py:1131` `if "reasoning_content" not in msg and (thinking_enabled or supports_thinking): msg["reasoning_content"] = ""` —— 给历史 assistant 消息补一个空 reasoning_content。这是为 Anthropic 兼容 OpenAI 客户端做的 hack，但会让某些统计 / display 误以为"有过思考"。
- `desktop_api/server.py:1445` `_pid_for` 在 session 不在 `_sessions` 时会扫所有 recent workspaces 的磁盘找 pid —— 这是为了 workspace 切换后跨 workspace 查询能正确路由，但每次 list 都全表扫描 recent_workspaces.json，性能随 session 数线性。
- `cli.py:511-512` `except (KeyboardInterrupt, SystemExit): sys.exit(0)` 把所有异常都转成 0 退出码（包括 SystemExit）—— 隐藏错误。

### 9.5 子 agent 与父 session 共享 state

`runtime.py:1126-1129` + `plugins/tools/spawn_agent/impl.py:120-134` 显示：sub-agent 调用 `runtime._run_model_tool_loop` 时复用父 session_id（`session_id=ctx.session_id`），仅靠 `metadata["_subagent"]` 区分。结果：
- sub-agent 的 tool_result 会被 append 到父 session.history
- sub-agent 的 tool_start / tool_end 也通过 event_bus 推到父 session 的队列
- 这可能是设计意图（让 LLM 在下一轮能看到子结果），但需要文档说明；并且 `subagent_*` 事件带 `_subagent_call_id` 字段，前端用它做视觉分组。

---

## 10. 主要发现 Top 5

1. **🔴 `apply_patch` 工具被 `edit_file` 替代但测试未清理**（`tests/test_apply_patch_tool.py`、`tests/test_plugin_loading.py:16`、`tests/test_plugins.py:15` 三处断言 `"apply_patch" in tool_names`，而 `plugins/tools/apply_patch/` 目录不存在）。运行 pytest 这 3 个测试集必失败。建议：要么删除测试 + 改名文档，要么把 edit_file 拆出 apply_patch 协议。

2. **🟠 README 与运行时行为不一致**：README 声称工具调用支持 `[[TOOL_CALL]]...[[/TOOL_CALL]]` 和 `TOOL_CALL name args` 两种文本协议，但 `grep "TOOL_CALL" src/` 在生产代码里 0 命中——运行时走的是 OpenAI/Anthropic SDK 原生 tool_calls 数组。这份误导文档会让外部集成方按错误的协议实现。要么实现文本解析（向后兼容某些不支持原生 tool_call 的模型），要么改 README。

3. **🟠 `runtime.py:1597` `supports_image:` 字段读的是 runtime 默认模型的 vision 状态，而不是本 turn 模型**。当用户在前端把 session 切到非 vision 模型时，system prompt 的 `supports_image: false` 仍然显示 `true`（旧值），模型会基于错误信息决定要不要主动调 image 工具。建议把 `_build_environment_context` 改成接收 `model_name` 参数（与 `chat_streaming` 同步），或在 `_emit` 时把 `model_supports_image` 单独 emit。

4. **🟡 三个"超级文件"接近不可维护**：`runtime.py`(1788)、`desktop_api/server.py`(1939)、`web/src/main.ts`(~5000+)。建议至少：
   - 把 `runtime.py` 的 MCP 接入块（`_connect_mcp_if_needed`、`MCPToolWrapper` 路由）抽到独立类
   - 把 `desktop_api/server.py` 的 50+ endpoint 按主题拆到子 router（session.py、automation.py、workspace.py、agents.py、panel.py），主文件保留注册
   - `web/src/main.ts` 按页面/组件拆分（消息列表 / composer / 右侧面板 tabs / skills browser 各自独立文件）

5. **🟡 顶层目录被个人工作遗留物污染**：13+ 个 `*.png`（NVDA/TSLA/douyin/weibo/solar）、`*.txt` 对话记录、3 个 `*.md`（task_plan/findings/progress —— 与 ziva 无关的 Agent 框架调研）、`solar_system.html` 两份（23KB+16KB）、`test_anthropic*.py`、`tsla_analysis.py`、`normal_distribution.py`、`CFG`、`hello.txt`、`backend.log`、`backend_test.log`、`code_analysis_report.md`。这些应清理或加入 `.gitignore`（特别是 `backend.log`/`backend_test.log` 当前是 0 字节空文件）。

---

## 附录：辅助引用

- ACP handle：`src/ziva_runtime/protocols/acp.py:24`
- Model↔Tool 循环：`src/ziva_runtime/runtime.py:884`
- Compact hook：`src/ziva_runtime/runtime.py:954-986`
- Auto-compact threshold 常量：`src/ziva_runtime/runtime.py:386,394`
- MCP 状态机：`src/ziva_runtime/shared_types.py:12-35`
- 工具拒绝/重试逻辑：`src/ziva_runtime/adapters/retry.py:31-43`
- 桌面 SSE：`src/ziva_runtime/transports/desktop_api/server.py:738-819`
- Web SSE 池：`web/src/sse.ts:75-156`
- Electron CDP 桥接点：`electron/main.ts:148-158` + `electron/cdp-bridge.ts:350-447`
- 配置校验：`src/ziva_runtime/config/loader.py:83-227`
- 插件 manifest：`src/ziva_runtime/plugins/manifest.py:25-61`
