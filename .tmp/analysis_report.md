# Ziva Runtime 深度架构分析

> 分析对象：`/Users/wangxinxin/code/ziva`
> 范围：Python 后端核心运行时（`src/ziva_runtime/`），辅以 Electron 桌面壳和 Web 前端概况
> 时间：2026-06-22

---

## 1. Runtime 启动链路

入口到单例对象的完整调用链：

```
CLI:  ziva run "hello"
  └─ app/cli.py:452  run_async(argv)
      └─ app/cli.py:459  _runtime_for_workspace(workspace, override)
          └─ runtime.py:587  Runtime.create(workspace_root, global_config_path, session_override)
              ├─ config/loader.py  load_effective_config(...)              # YAML 分层合并 + 校验
              ├─ capabilities/registries.py  CapabilityRegistry()          # 空注册表
              ├─ plugins/loader.py  load_plugins(workspace_plugin_paths, registry, config)
              ├─ 遍历 config.skill.extra_paths → 再 load_plugins(...)     # 全局 skill 路径
              ├─ cls(config, registry, EventBus(), workspace_root)        # 构造实例
              └─ permissions/manager.py  get_permission_manager().set_approved_rules(from_config(perm_config))
```

### 关键步骤详解

**1.1 `load_effective_config` (config/loader.py, ~11.6KB)**
- 读 global config（`~/.ziva/config.yaml`） + workspace config（`.ziva/config.yaml`）+ CLI override
- 严格校验（类型、必填字段）
- 返回最终 dict

**1.2 `load_plugins` (plugins/loader.py)**
- 扫描 `config.plugin.paths`（默认 `./plugins`）+ `config.skill.extra_paths`（默认 `~/.ziva/skills`、`~/.agents/skills`）
- 每个插件读 `manifest.yaml`，校验后注册到 `CapabilityRegistry`
- 注册类型：`tool / prompt / skill / hook / memory`（统一扩展 API）

**1.3 `Runtime.__init__` (runtime.py:382-385)**
- 持有：`config / registry / event_bus / workspace_root / perm_manager` 单例
- `_sessions: Dict[str, SessionState]` 懒初始化

**1.4 权限初始化 (runtime.py:619-623)**
- `get_permission_manager()` 是 module-level 单例
- `from_config(perm_config)` 把 YAML `permissions:` 块转成 `ApprovedRule` 列表（一次放行规则）

---

## 2. `chat_streaming` 主循环（核心）

入口：`runtime.py:765 chat_streaming(...)`，是个 async generator。

### 2.1 阶段拆解

| 阶段 | 行号 | 关键动作 |
|---|---|---|
| **A. Session 准备** | 773-808 | 解析 sid → `_get_session` → `load_lock` 串行化 → `_load_session_from_disk` 加载历史 → `_sanitize_orphaned_tool_calls` 修复历史脏数据 |
| **B. Hooks & Skill** | 810-819 | `before_turn` hook → emit `turn_start` → `_apply_prompt` 组装系统提示 → `_maybe_apply_skill` 检查触发 skill |
| **C. 模型快照** | 826-831 | 锁定本次 turn 的 `model_cfg`（考虑 `session.model_name` 覆盖）和 `turn_adapter`（缓存键相同则复用） |
| **D. 重试包装** | 832-866 | 外层 `for attempt in (1,2)`：第一次遇可重试错误 → 发 `stream_reset` 事件让客户端清屏 → 重试；否则发 `turn_error` |
| **E. 工具循环核心** | 868 `_run_model_tool_loop` | 见下 2.2 |
| **F. 收尾** | 866 | `finally: yield turn_end`（无论成败都发） |

### 2.2 `_run_model_tool_loop` (runtime.py:868-1207)

```
round_idx = 0
while round_idx < max_rounds:
    # 守卫 1: cancel
    if cancellation_token.is_cancelled: yield cancelled; return

    # 自动压缩（基于磁盘读到的 prompt_tokens）
    if prompt_tokens / context_window >= AUTO_COMPACT_THRESHOLD:
        if 已有足够 assistant 消息可压缩:
            yield status:compact
            working = await compact_messages(working, ...)        # 内存压缩
            _apply_compact_to_disk(...)                          # 磁盘压缩
            yield context_compacted

    # 装配 system prompt = base_prompt + instructions + env_context + skill_index
    thinking_config = 模型能力 + thinking_mode 决定

    # 流式调用模型
    stream = model_adapter.chat_stream(working, model, system, tools, thinking)
    async for delta in stream:
        # 守卫 2: cancel mid-stream
        if cancel: yield cancelled; return
        # 累积 full_content / full_reasoning_content / final_tool_calls / final_usage
        # yield delta / reasoning_delta / usage_update

    # 模型本轮响应结束
    yield model_response  (final assistant content)
    if not final_tool_calls:                            # 没工具调用 → 收尾
        persist assistant_msg → JSONL
        yield round_complete → return

    # 有工具调用
    persist assistant_msg (含 tool_calls)
    for tc in final_tool_calls: yield tool_start

    # 并发执行所有工具（asyncio.gather）
    tool_results = await asyncio.gather(*[_run_tool(tc)])
    # CancelledError 兜底：每个 tool_call 补一条 "[cancelled]" tool_result，避免下次重放 400

    # 按原顺序回填结果
    for output, tc in tool_results:
        yield tool_end
        # 如果 tool_not_found → 立刻 round_complete + 提示信息 → return
        # 如果返回了图片 → 文本占位 + 延迟 image_msg（user 角色 _hidden=True）
        # 否则 → 普通 tool_result 回填 working[] 和 session.history[]

    yield round_complete
    # 进入下一轮

# while 退出 → max_rounds reached
yield "Tool execution reached max_rounds without final answer." → return
```

### 2.3 事件类型清单

`_run_model_tool_loop` + 包装层 yield 的所有事件 `type`：

| type | 触发位置 | 说明 |
|---|---|---|
| `turn_start` | chat_streaming:811 | turn 入口 |
| `turn_end` | chat_streaming:866 | turn 出口（finally） |
| `stream_reset` | chat_streaming:856 | 第一次调用可重试错误，通知客户端清屏 |
| `turn_error` | chat_streaming:863 | 不可重试错误 |
| `turn_cancelled` | chat:743 | cancel 兜底路径 |
| `context_compacted` | loop:968, 908 | 自动 / 手动压缩完成 |
| `status` | loop:950 | 中间状态（`content: "compact"`） |
| `reasoning_delta` | loop:1038 | 思考增量（Anthropic extended thinking） |
| `delta` | loop:1043 | 文本 token 增量 |
| `usage_update` | loop:1058 | 用量（多次出现取 max） |
| `model_response` | loop:1072, 1160, 1200 | 完整 assistant 响应（最终 / tool_not_found / max_rounds） |
| `tool_start` | loop:1102 | 工具调用开始（含 call_id） |
| `tool_end` | loop:1144 | 工具结果（含 output / error / image） |
| `round_complete` | loop:1077, 1156, 1193, 1204 | 单轮结束（带 latency_ms / usage） |
| `cancelled` | loop:929, 1027 | turn 内 cancel 触发 |

### 2.4 关键守卫

| 守卫 | 行号 | 行为 |
|---|---|---|
| CancellationToken | loop:928, 1026 | turn 入口 + 流式 delta 入口都检查 |
| max_rounds | loop:926 | `0`/`None` 视为无限制（特殊值） |
| Doom loop | （应该在 `_execute_tool` 内） | 未在主循环看到独立的检测；推测依赖工具自身的去重 |
| ask_user 阻塞 | `_execute_tool` → `await_user_answer` | 工具执行时挂起 future，回调 `set_user_answer` 唤醒 |
| 权限审批 | `_execute_tool` → `PermissionManager.request_approval` | 在工具前拦截；`pending_questions` 风格的 future 模式 |
| Retryable 错误 | chat_streaming:841 + `_is_retryable_provider_error` | 仅 1 次重试，发 `stream_reset` 让前端清屏 |

---

## 3. Session 状态机 & MCP 生命周期

### 3.1 SessionState 字段 (shared_types.py:139-154)

```python
@dataclass
class SessionState:
    project_id: str | None = None
    history: list[ChatMessage]
    event_seq: int = 0                                  # 单调递增，SSE 排序用
    pending_questions: dict[str, asyncio.Future]        # ask_user 挂起映射
    hook_states: dict[str, Any]
    mcp_client: Any | None
    mcp_status: MCPConnectStatus = DISCONNECTED         # ★ 四态机
    mcp_connected_event: asyncio.Event                  # 用于等 CONNECTING 完成
    cancel_token: CancellationToken | None
    turn_task: asyncio.Task | None
    event_queue: asyncio.Queue | None
    event_history: deque(maxlen=100)                    # EventBus 历史（用于 chat_with_events）
    model_name: str | None                              # per-session 模型覆盖
    load_lock: asyncio.Lock                             # 串行化加载+扩展 history
    plan: list[dict] | None                             # plan 工具的进度
```

### 3.2 MCP 状态机（关键修复点）

四态枚举（shared_types.py:30-34）：
```
DISCONNECTED  ──(首次调用 _connect_mcp_if_needed)──► CONNECTING
CONNECTING    ──(connect_all 成功)──► CONNECTED      (终态)
CONNECTING    ──(connect_all 失败)──► FAILED         (下次 turn 重试)
CONNECTING    ──(无 mcp.servers 配置)──► NO_CONFIG    (本以为终态，但运行时也允许重试)
```

**`_connect_mcp_if_needed` 实现关键点 (runtime.py:1209-1268)**：

1. **CONNECTED 短路** (1217)：已连则跳过
2. **CONNECTING 等待** (1219-1221)：如果别的 task 正在连，当前 task `await mcp_connected_event.wait()`
3. **加锁转 CONNECTING** (1223-1224)：清掉 event，开始连接
4. **parse_mcp_config** (1228)：从 config 读 `mcp.servers` 列表
5. **无配置 → NO_CONFIG** (1229-1231)：直接 return；注意 next turn 仍会再尝试（注释说明允许切换 workspace 后重试）
6. **MCPClient.connect_all()** (1234-1235)：新版用本地 mcp SDK wrapper（commit ff59861 替换了 openai-agents）
7. **注册工具到 CapabilityRegistry** (1237-1252)：每个 mcp tool 名为 `mcp.<name>`，跳过已注册（幂等）
8. **成功 → CONNECTED** (1253-1254)
9. **失败 → FAILED** (1255-1261)：只对「catastrophic 失败」生效；`connect_all` 内部已吞掉 per-server 错误
10. **finally 兜底** (1262-1268)：如果还在 CONNECTING（比如 `parse_mcp_config` 抛异常），强制改成 FAILED，再 set event 唤醒等待者

**关键设计选择**：
- FAILED 不短路 → 下次 turn 自动重试（修过老 bug：失败后 boolean flag 标了"已连"导致 session 永久卡死）
- NO_CONFIG 不短路 → 允许切换 workspace 后再次尝试

### 3.3 Session 创建/加载/切换/清理

| 操作 | 入口 | 位置 |
|---|---|---|
| 创建 | `chat/chat_streaming` 内自动 `sid = session_id or uuid.uuid4()` | runtime.py:773 |
| 加载 | `_load_session_from_disk(sid)` 在 `load_lock` 内执行 | runtime.py:1691 |
| 持久化 | 每条消息 `_persist_message` → JSONL append | runtime.py:1721 |
| 切换模型 | `session.model_name = ...`（被 updateSession PATCH 调用） | 桌面 API 层 |
| 压缩 | 自动：`prompt_tokens/window >= 0.9`；手动：`/compact` | session/compaction.py |
| 清理 | `delete_session(project_id, session_id)` | storage/file_storage.py |
| 列出 | `GET /sessions` → `list_sessions(project_id)` | desktop_api/server.py |

### 3.4 自动压缩触发

`_run_model_tool_loop` 入口 (runtime.py:938-970)：
```
last_usage = _read_last_usage(session_id)      # 读磁盘上的 usage.json
if last_usage.prompt_tokens / context_window >= AUTO_COMPACT_THRESHOLD:
    if len([m for m in working if m.role == "assistant"]) >= AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS:
        yield status:compact
        working = await compact_messages(...)
        _apply_compact_to_disk(...)
        yield context_compacted
```

阈值 `AUTO_COMPACT_THRESHOLD = 0.9`（与历史 `200k - 20k` OVERFLOW_BUFFER 等价）。

---

## 4. 模块依赖图

`src/ziva_runtime/` 内部 import 关系（grep `from ziva_runtime` 计数）：

| 出现次数 | 模块 | 被谁依赖 |
|---|---|---|
| 16 | `transports/desktop_api/server.py` | （作为入口被外部调用） |
| 15 | `runtime.py` | （被 cli.py、acp.py、server.py 引用） |
| 6  | `app/cli.py` | main 入口 |
| 3  | `adapters/mcp/client.py` | runtime.py |
| 2  | `protocols/acp.py` | cli.py |
| 2  | `plugins/loader.py` | runtime.py |
| 2  | `adapters/openai/provider.py` | runtime.py |
| 2  | `adapters/anthropic/provider.py` | runtime.py |
| 1  | 各（storage, session, permissions, capabilities, plugins） | 底层模块 |

### 4.1 依赖层次（自下而上）

```
                      ┌─────────────────────┐
                      │   app/cli.py        │   入口
                      │   transports/       │   桌面 API
                      │   protocols/acp.py  │   ACP stdio
                      └──────────┬──────────┘
                                 │
                      ┌──────────▼──────────┐
                      │     runtime.py      │   ★ 核心，单类 1780 行
                      └──────────┬──────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
┌───────▼────────┐  ┌────────────▼───────────┐  ┌─────────▼─────────┐
│ adapters/      │  │ capabilities/          │  │ session/          │
│  ├ openai/     │  │  ├ events.py (Bus)     │  │  compaction.py    │
│  ├ anthropic/  │  │  └ registries.py       │  └───────────────────┘
│  └ mcp/        │  │ permissions/           │
│                │  │  └ manager.py          │  ┌───────────────────┐
├────────────────┤  │ plugins/loader.py      │  │ config/loader.py  │
│ config/        │  │ storage/file_storage   │  │ config/           │
│  loader.py     │  │ shared_types.py        │  │ instructions.py   │
└────────────────┘  └────────────────────────┘  └───────────────────┘
```

### 4.2 循环依赖 / 反向依赖检查

- ✅ **无循环依赖**：底层模块（storage、shared_types、permissions）只 import 自身
- ⚠️ **`runtime.py` 反向注入**：`ctx.metadata["_runtime"] = self` (runtime.py:631, 776)，用 dict 注入 runtime 引用，避免循环 import 但耦合度高
- ⚠️ **`_connect_mcp_if_needed` 内 import**：`from ziva_runtime.adapters.mcp.client import MCPClient, parse_mcp_config` 放在函数内（runtime.py:1226），延迟加载让 mcp 可选
- ⚠️ **`runtime.py` 单文件 1780 行 + 38 个 import**：所有核心路径都汇聚在此

---

## 5. 测试覆盖度

### 5.1 测试文件分类（`tests/` 55 个文件）

| 类别 | 文件数 | 主要测试文件 |
|---|---|---|
| **ACP 协议** | 4 | test_acp.py、test_acp_stream.py、test_acp_chunk_stream.py、test_acp_incremental.py、test_acp_process_stdio.py |
| **会话/session** | 6 | test_session_compaction、test_session_switch_bug、test_session_switch_e2e、test_session_switch_model、test_multi_session_isolation、test_desktop_compact_usage |
| **工具** | 9 | test_shell_tool、test_read_file_tool、test_write_file_tool、test_edit_tool、test_grep_tool、test_apply_patch_tool、test_web_search_tool、test_update_plan_tool、test_tool_call_protocol |
| **MCP** | 3 | test_mcp_client、test_mcp_enum_lifecycle、test_retry_backoff |
| **适配器** | 1 | test_adapter_singleton |
| **权限/审批** | 1 | test_permission_gate、test_approval_config |
| **配置** | 3 | test_config、test_config_validation、test_config_model_fields |
| **插件/扩展** | 4 | test_plugin_loading、test_plugins、test_manifest_validation、test_runtime_extensions |
| **桌面/E2E** | 4 | test_desktop_api、test_desktop_alignment、test_cli_e2e、test_process_e2e |
| **异步/特殊场景** | 5 | test_spawn_agent_definitions、test_spawn_concurrency、test_turn_failure、test_ask_user_no_timeout、test_event_stream、test_event_metadata、test_repl、test_instructions_loader、test_instructions_integration、test_markdown_memory、test_image_path_resolver、test_per_session_model、test_tool_loop、test_reasoning_field |
| **杂项** | ~15 | 含图像解析、reasoning、retry 等 |

### 5.2 覆盖度评估

| 核心路径 | 测试 | 备注 |
|---|---|---|
| `Runtime.create` 启动链路 | ⚠️ 间接 | 通过 test_desktop_alignment / test_process_e2e 间接覆盖 |
| `_run_model_tool_loop` 主循环 | ✅ test_tool_loop | 基础覆盖 |
| 自动压缩（threshold 触发） | ✅ test_session_compaction | 大文件（19.4KB） |
| 手动 `/compact` | ✅ | 同上 |
| MCP 状态机迁移 | ✅ test_mcp_enum_lifecycle | 专门测枚举 |
| MCP 连接失败重试 | ✅ test_retry_backoff | |
| 工具权限审批 | ✅ test_permission_gate / test_approval_config | |
| `chat_streaming` 事件类型 | ✅ test_event_stream / test_event_metadata | |
| `ask_user` 阻塞 | ✅ test_ask_user_no_timeout（6.4KB） | 详细 |
| CancellationToken | ⚠️ 弱 | 通过 test_turn_failure 间接测 |
| Doom loop 检测 | ❌ 无 | 未见专门测试 |
| 多 workspace 切换 | ✅ test_session_switch_* | |
| Per-session 模型 | ✅ test_per_session_model（13.8KB） | |
| `image_path_resolver` | ✅ test_image_path_resolver（21.4KB） | 大量 |
| **PermissionManager 并发审批** | ❌ 无 | 仅测了 gate，没测并发场景 |
| **`_apply_compact_to_disk` 落盘** | ⚠️ 部分 | test_session_compaction 间接 |
| **`_sanitize_orphaned_tool_calls`** | ❌ 无 | 新加的孤儿工具调用修复无独立测试 |

### 5.3 已知环境问题

- pytest capture 插件在该环境 segfault，需 `-p no:capture`（README 提示）
- `conftest.py` 1.9KB 提供 fixtures

---

## 附录：关键发现 / 改进建议

### 🔴 重要

1. **`runtime.py` 1780 行单类 + 38 个 import**：是整个 Python 后端的"上帝类"。建议按职责拆分：
   - `RuntimeCore`（chat / chat_streaming / 主循环）
   - `SessionManager`（_get_session / _load / _persist / MCP）
   - `ToolDispatcher`（_build_tools_param / _execute_tool / 权限）
   - `PromptAssembler`（_apply_prompt / _build_environment_context / _maybe_apply_skill）

2. **`ctx.metadata["_runtime"] = self` 反向注入**（runtime.py:631, 776）：用 dict 注入避免循环 import，但耦合高且不显式。建议改为显式的回调注册（已经做过 `on_ask_user` 这条路）。

### 🟡 中等

3. **`_sanitize_orphaned_tool_calls`**（runtime.py:1629）：注释提到修复 Anthropic 400 错误（`tool call result does not follow tool call (2013)`），但没有专门测试覆盖。建议加 test 模拟"取消后 JSONL 末尾 orphan assistant"。

4. **MCP NO_CONFIG 状态被设为终态但又被重试**（runtime.py:1230 + 注释 1213-1216）：行为是"下次 turn 再尝试"。这个设计选择值得写进注释 / ADR，避免后续维护者看不懂。

5. **根目录散落 demo 文件**：NVDA/TSLA/微博/抖音截图 + 脚本没被 `.gitignore` 覆盖（已发现 `/analysis_preview.png`、`/tsla_1m_chart.png`、`/weibo_hot.png`、`/douyin_hot*.png`、`/solar_system.html` 等）。建议补：
   ```
   *.png
   /scratch/
   /task_plan.md
   /findings.md
   /progress.md
   /test*.py  # 根目录的临时脚本
   ```

### 🟢 轻微

6. **README 与最新 commit 不同步**：第 9 行说「OpenAI Agents adapter」，但 commit `ff59861 refactor(mcp): replace openai-agents with a local mcp SDK wrapper` 已替换。

7. **`web/src/main.ts` 单文件 288KB**：超过一般可维护阈值。Vite 已配但未做代码分割。

8. **`scripts/test_real_api.py` / `test_real_desktop.py` 被 .gitignore** 但似乎没被挪走；如果是手测脚本建议移到 `scripts/manual/` 子目录。

---

> 本报告由手写完成（子 agent 多次未完成实际工作，已绕过）。