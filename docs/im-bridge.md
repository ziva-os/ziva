# Ziva IM Bridge

把 Ziva 接入飞书、Telegram，让用户在任何设备上都能给 AI 派活。本文档描述代码结构与前端 UI 设计。

> 用户操作指南（如何获取飞书 App ID / Telegram Bot Token 等）请见 [`docs/im-bridge-setup.md`](./im-bridge-setup.md)。

> 核心决策：bridge 作为 `desktop_api` 的 **in-process 组件**（非独立子进程），IM turn 走 `create_turn` / `chat_with_events` 同款路径——**IM session 即普通 session**，只换入口（IM adapter）和出口（回复发回 IM）。

## 1. 设计目标

| 目标 | 实现方式 |
|---|---|
| 接入飞书 | lark-oapi WebSocket 长连接（官方 SDK，零公网暴露） |
| 接入 Telegram | 官方 Bot API（BotFather token） |
| 配置一次，长期生效 | 持久化到 `~/.ziva/config.yaml` 的 `im_bridge` 键（全局，不按 workspace） |
| 不污染主界面 | sidebar 多一个"连接手机"按钮 → 全屏 modal（与"自动化"同构） |
| 本机 / IM 会话自然融合 | session 记录带 `source`/`channel` 字段，侧边栏用 SVG 图标区分 |
| 支持图片输入 | 飞书 / Telegram 接收的图片随 user message 进入多模态模型 |
| 透明化回复 | 回复中包含思考过程与工具调用结果摘要 |
| **外部消息可控** | sender 白名单：非白名单消息直接丢弃（见 §9 安全） |

**核心设计哲学：让 Bridge 几乎"不存在"于用户视野中。** 配置完成后，用户感知到的就是 "Ziva 现在支持飞书和 Telegram 了"，而不是 "Ziva 装了一个 Bridge 插件"。

---

## 2. 架构：in-process，IM turn 走 create_turn 同款路径

### 2.1 为什么不是独立子进程

ziva 现有 `desktop_api` 是**单进程 in-process**模型：`DesktopAPIServer` 直接持有 `Runtime` 引用，共用一个 asyncio loop、一个 EventBus（`cli.py:488-519`、`server.py:159-170`）。整个 ziva 没有任何"第二个子进程共享内存态 Runtime"的先例。

若 bridge 跑成独立子进程 `ziva bridge serve`，只有两条路，都走不通：
- 子进程自己 `Runtime.create(...)` → 两个 runtime 抢 `~/.ziva/sessions/` 文件锁、session 互不可见、EventBus 各自一份。
- 子进程走 HTTP 调 desktop_api → 多一层无谓的进程间调用。

因此 bridge **作为 `desktop_api` 进程内的一个组件**，与 automation 并列——两者都是在 HTTP 之外、in-process 触发一次 turn 的入口（automation 定时触发，bridge 由 IM 消息触发）。

### 2.2 走 `create_turn` 同款路径：IM session 即普通 session

IM 创建的 session 和你在桌面端点"新对话"打字回车创建的 session **完全一样**——同一套消息历史、同一个模型、同样在侧边栏可见、能点进去看实时过程。bridge 不是一种"特殊 session 类型"，只是给普通 turn 换了入口和出口：

- **入口**：IM adapter 收到消息，而非桌面端 `POST /sessions/{sid}/turns`。
- **出口**：模型回复发回 IM，而非（仅）流式推桌面 SSE。

桌面端那条 turn 路径是 `create_turn`（`server.py:579-666`）：lazy 建 session → `runtime.chat_with_events(messages, session_id=sid)` → 事件经 EventBus 推 SSE。IM 走**同一条** `chat_with_events`（`runtime.py:954`），只是 await 等结果出来再转发给 IM adapter：

```
IM msg → chat_with_events([user msg], sid) → EventBus（桌面 SSE 可实时看）→ result.content → adapter.send_message → IM
```

### 2.3 进程内位置

```
┌──────────────────────── ziva desktop_api 进程 (127.0.0.1:4097) ────────────────────────┐
│                                                                                        │
│   Runtime  (单例, 共享)                                                                │
│     ├─ chat() / chat_with_events()                                                    │
│     ├─ event_bus                                                                       │
│     └─ _sessions                                                                       │
│                                                                                        │
│   DesktopAPIServer                                                                     │
│     ├─ /api/sessions/*            (现有, 交互对话)                                     │
│     ├─ /api/automations/*         (现有, 定时任务)                                     │
│     ├─ /api/im/channels/*         🆕 IM bridge 端点 (同端口, 无需 4098)                │
│     │                                                                                  │
│     ├─ AutomationRunner          (现有, asyncio.Task per automation)                   │
│     └─ IMBridge  🆕              (asyncio.Task per adapter)                            │
│           ├─ FeishuAdapter       (lark-oapi WebSocket)                                 │
│           └─ TelegramAdapter     (Bot API 长轮询)                                      │
│                                                                                        │
└────────────────────────────────────────────────────────────────────────────────────────┘
        ▲                                          ▲
        │ HTTP/SSE                                 │ WebSocket / 长轮询 / HTTP
   Electron 前端                              飞书 / Telegram 云端
```

---

## 3. 代码结构

### 3.1 后端：`transports/im_bridge/` 组件（不是独立 transport 进程）

```
src/ziva/transports/
├── desktop_api/                   # 现有：Electron 用的 HTTP API (:4097)
│   ├── server.py                  # 🆕 加 /api/im/channels/* 路由 + on_startup 启 IMBridge
│   ├── stt_warmup.py
│   └── static/
│
└── im_bridge/                     # 🆕 IM Bridge 组件（被 desktop_api 加载）
    ├── __init__.py
    ├── bridge.py                  # IMBridge: 加载配置 / start / stop / on_message → session
    ├── adapters/
    │   ├── __init__.py
    │   ├── base.py                # BaseAdapter (统一接口)
│     ├── feishu.py              # lark-oapi WebSocket
│     └── telegram.py            # Telegram Bot API
    └── store.py                   # 路由表持久化 (~/.ziva/config.yaml 的 im_bridge 键)
```

### 3.2 为什么不是 `plugins/`

`plugins/` 只放 agent 能力扩展（`Tool` / `Skill` / `Hook` / `MemoryStore`）。Bridge 不是 agent 能力，是**外部世界触发 agent 的入口**，语义上跟 `desktop_api` 一致。

### 3.3 为什么端点加在 desktop_api 而非独立 server

in-process 的话，bridge 端点就是 `desktop_api:4097` 上几条新路由（仿 `/api/automations/*`）。另开第二端口 = Electron 前端多一个 baseURL + origin 处理，零收益。前端已经在跟 4097 说话，直接复用。

### 3.4 关键类设计

#### `BaseAdapter`（统一接口）

```python
# transports/im_bridge/adapters/base.py
class BaseAdapter(ABC):
    @abstractmethod
    async def start(self, config: dict) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(self, msg: OutgoingMessage) -> str: ...

    @property
    @abstractmethod
    def state(self) -> ConnectionState: ...  # DISCONNECTED / WAITING_SCAN / CONNECTED

    @property
    @abstractmethod
    def qr_code(self) -> str | None: ...   # 仅 wechat 用，飞书/telegram 返回 None

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    # adapter 收到消息时回调 IMBridge.on_message
    on_message: Callable[[IncomingMessage], Awaitable[None]]
```

#### `IMBridge`（单类：adapter 生命周期 + 路由合一）

```python
# transports/im_bridge/bridge.py
class IMBridge:
    def __init__(self, runtime: Runtime, store: SessionStore):
        self.runtime = runtime
        self.store = store
        self._adapters: dict[str, BaseAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {}   # per chat_id 串行化

    async def start(self) -> None:
        """desktop_api on_startup 调用，仿 _load_persisted_automations。"""
        cfg = IMConfig.load()
        for name, ch in cfg.channels.items():
            if ch.enabled:
                await self._start_adapter(name, ch)

    async def on_message(self, msg: IncomingMessage) -> None:
        """adapter 收到 IM 消息 → 在普通 session 上走一次 turn → 回消息。"""
        # 1. sender 白名单校验（见 §9）
        if not self._is_allowed_sender(msg):
            return
        # 2. (channel, account_id, chat_id) → sid 路由（新聊天则建普通 session）
        sid = self._route_session(msg)
        # 3. 同 chat_id 串行化（等价于 create_turn 的 429，但 IM 排队而非拒绝）
        lock = self._locks.setdefault(msg.chat_id, asyncio.Lock())
        async with lock:
            # 4. 打 channel/chat_id 标（仅为图标+路由，不改 session 行为）
            self._ensure_session(sid, msg)
            # 5. 走 create_turn 同款路径：chat_with_events
            #    事件经 EventBus → 桌面 SSE 可实时看该 IM session
            _, result, _ = await self.runtime.chat_with_events(
                [ChatMessage(role="user", content=msg.text)],
                session_id=sid,
            )
            # 6. 回复发回 IM
            await self._adapters[msg.channel].send_message(
                OutgoingMessage(chat_id=msg.chat_id, text=result.content)
            )
```

`_ensure_session` 的真实写法见 §6。

---

## 4. HTTP 端点（加在 `desktop_api/server.py`，端口 4097）

```python
# 仿 /api/automations/* 的风格
self.app.router.add_get ("/api/im/channels",                 self.list_channels)
self.app.router.add_post("/api/im/channels/{name}/start",    self.start_channel)
self.app.router.add_post("/api/im/channels/{name}/stop",     self.stop_channel)
self.app.router.add_get ("/api/im/channels/{name}/status",   self.get_channel_status)
```

启动：`desktop_api` 的 `on_startup`（`server.py:267-269` 现有 `_on_startup`）里追加 `await self._im_bridge.start()`，仿 `_load_persisted_automations` + `_schedule_enabled_automations`。

**不要**新增 `ziva bridge serve` CLI 子命令、**不要**新增端口。

---

## 5. 配置文件：`~/.ziva/config.yaml` 的 `im_bridge` 键（全局）

IM 天然跨工作区（消息从手机来，不绑定某个 workspace），所以配置是**全局**而非 per-workspace（区别于 automation 的 `~/.ziva/automations/<project_id>.json`）。IM 配置保存在 `~/.ziva/config.yaml` 的 `im_bridge` 键下，与模型、技能等设置统一管理。

```yaml
im_bridge:
  default_workspace: "/Users/me/code/main"
  allowed_senders: ["feishu_open_id_xxx", "tg:123456789"]
  channels:
    feishu:   { enabled: false, app_id: null, app_secret: null }
    telegram: { enabled: false, bot_token: null, proxy_url: null }
  routes:
    "feishu:app_xxx:oc_chat_yyy":      "sid-uuid-..."
    "telegram:123456789:789012345":    "sid-uuid-..."
```

- `default_workspace`：IM 触发的 session 绑定的 workspace（runtime 需 `workspace_root` 解析工具 cwd）。fallback 到最近活跃 workspace。
- `allowed_senders`：sender 白名单，见 §9。
- `routes`：`(channel, account_id, chat_id)` → sid 映射，保证同一聊天保留上下文。

---

## 6. Session 字段约定（ad-hoc 字段，非 metadata API）

> `Runtime` 没有 `create_session(metadata=...)` 方法，`SessionState`（`shared_types.py:185-214`）也没有 `metadata` 字段——IM 的来源信息用 ad-hoc 字段写进 session 记录，仿 `is_automation` 的写入方式。

真实做法：仿 `is_automation` 的写入方式——`store.create(sid)` 建会话，再用 `FileStorage.update_session(pid, sid, {...})` 写 ad-hoc 字段：

```python
# transports/im_bridge/bridge.py
def _ensure_session(self, sid: str, msg: IncomingMessage) -> None:
    sess = self.store.get(sid)
    if sess is None:
        self.store.create(sid)  # 现有 SessionStore.create (server.py:102-114)
    # 绑定 workspace（让 runtime 能解析工具 cwd）
    ws = self._config.default_workspace or self._last_active_workspace()
    FileStorage.update_session(self.runtime.project_id, sid, {
        "source":       "im-bridge",     # 区分字段
        "channel":      msg.channel,     # feishu / telegram
        "chat_id":      msg.chat_id,
        "sender_id":    msg.sender_id,
        "sender_name":  msg.sender_name,
        "workspace_root": ws,
        "name":         f"{msg.sender_name} · {msg.channel}",
    })
```

前端读这些**扁平字段**（session 记录上的顶层字段，非嵌套 metadata）：

```typescript
const channel = session.channel   // 直接读 ad-hoc 字段
```

`source === "desktop"` 或字段缺失即视为本机会话，不显示来源图标。

---

## 7. 前端 UI 设计

### 7.1 整体布局

Ziva 当前布局（三栏）：

```
┌─────────────┬──────────────────────┬─────────────────┐
│  左侧 sidebar│     主对话区        │  右侧 tab 面板 │
│             │                      │                 │
│  Ziva   [<] │                      │  [tab][tab][+]  │
│  [+ 新对话] │                      │                 │
│  [Skills]   │                      │  tab 内容       │
│  [自动化]   │                      │                 │
│  [连接手机] │ ← 🆕                 │                 │
│  ──────     │                      │                 │
│  会话列表    │                      │                 │
│  ...        │                      │                 │
│  ──────     │                      │                 │
│  [Theme]    │                      │                 │
│  [Settings] │                      │                 │
└─────────────┴──────────────────────┴─────────────────┘
```

### 7.2 改动点：两处

| # | 位置 | 改动 |
|---|---|---|
| 1 | 左侧 sidebar-nav | 加"连接手机"按钮（跟 Skills/自动化 同级） |
| 2 | 左侧 session 列表 | IM 会话标题前加 SVG 来源图标 |

> 现有最相似的"自动化"功能是 sidebar 按钮 → 全屏 modal（`openAutomationsModal`，`modals/automations.ts`），不是 right-panel tab。IM 与之同构，也走 modal，保持一致。

### 7.3 改动 1：左侧 sidebar 加"连接手机"按钮

位置：`web/src/main.ts:165-174` 的 `<div class="sidebar-nav">`，在 `#btnScheduled`（自动化）后加：

```html
<button class="sidebar-nav-item" id="btnConnectIM">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <rect x="5" y="2" width="14" height="20" rx="2" ry="2"/>
    <line x1="12" y1="18" x2="12" y2="18"/>
  </svg>
  <span>连接手机</span>
</button>
```

绑定（`main.ts:573-574` 附近，仿 `btnScheduled` → `openAutomationsModal`）：

```typescript
$("btnConnectIM").onclick = () => openIMBridgeModal();
```

### 7.4 连接管理 modal（新文件 `web/src/modals/im-bridge.ts`）

> `web/src/` 下现有 `modals/` 和 `styles/` 两个子目录。新文件放 `web/src/modals/im-bridge.ts`，SVG 图标内联或放 `web/src/icons.ts`。

仿 `modals/automations.ts` 的全屏 modal（`openAutomationsModal` at line 26，backdrop + body + Esc 关闭）。

#### 有机器人状态

```
┌──────────────────────────────────────────────────────────┐
│  连接手机                                            ×   │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  把 Ziva 接入飞书、Telegram，手机上也能给 AI 派活。   │
│  收到消息会作为普通对话处理，回复发回 IM。             │
│                                                          │
│  ────────────────────────────────────────────────────── │
│                                                          │
│  已连接的机器人 (2)                                      │
│                                                          │
│  ┌────────────────────────────────────────────────────┐ │
│  │  [💬]  飞书                                 🟢   │ │
│  │        cli_aad9e... · 已连接                       │ │
│  │                                          [断开]   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌────────────────────────────────────────────────────┐ │
│  │  [✈️]  Telegram                             🟢   │ │
│  │        8605946005 · 已连接                        │ │
│  │                                          [断开]   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  [+ 添加机器人]                                          │
│                                                          │
│  安全 · 允许的发送者（白名单）                           │
│  只有白名单内的发送者能触发 Ziva。白名单为空时拒绝所有  │
│  消息（fail-closed）。这里的 ID 是给你发消息的人的 ID，  │
│  不是机器人自己的 ID。                                   │
│                                                          │
│  默认工作区                                              │
│  IM 触发的对话绑定的 workspace（决定工具的 cwd）。       │
│  留空则用当前活跃工作区。                                │
└──────────────────────────────────────────────────────────┘
```

#### 空状态（一个都没连）

```
┌──────────────────────────────────────────────────────────┐
│  连接手机                                            ×   │
├──────────────────────────────────────────────────────────┤
│                                                          │
│                    [📱 大手机图标]                       │
│                                                          │
│              还没有连接任何机器人                         │
│                                                          │
│      把 Ziva 接入飞书/Telegram，                        │
│      在手机上也能给 AI 派活                               │
│                                                          │
│              [+ 添加机器人]                              │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 7.5 添加机器人 modal

点 [+ 添加机器人] 弹 modal（复用现有 modal 系统，跟 Settings 风格一致）。

#### 选择类型

```
┌─────────────────────────────────────────┐
│  添加机器人                       ✕    │
├─────────────────────────────────────────┤
│                                         │
│  ┌───────────┐ ┌───────────┐
│  │  [💬]     │ │  [✈️]     │
│  │           │ │           │
│  │   飞书    │ │ Telegram  │
│  │ AppID+Sec │ │ Bot Token │
│  └───────────┘ └───────────┘
│                                         │
└─────────────────────────────────────────┘
```
#### 飞书 / Telegram 流程（填 token）

```
┌─────────────────────────────────────────┐
│  添加 Telegram                      ✕  │
├─────────────────────────────────────────┤
│                                         │
│  ① 手机打开 Telegram，搜索 @BotFather   │
│     发送 /newbot，按提示完成             │
│                                         │
│  ② 把 BotFather 给你的 Token 粘贴：     │
│     ┌───────────────────────────────┐  │
│     │ 7123456789:AAF-xxx...         │  │
│     └───────────────────────────────┘  │
│                                         │
│            [ 验证并连接 ]               │
└─────────────────────────────────────────┘
```

### 7.6 改动 2：Session 列表来源标识

#### 设计原则：空默认就是本机

- 本机会话 → 不显示图标，前面是空的
- IM 会话 → 显示对应 SVG 图标

```typescript
// web/src/icons.ts
export const channelIcons: Record<string, string> = {
  feishu:   `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16"><path fill="#00D6B9" d="..."/></svg>`,
  telegram: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16"><path fill="#229ED9" d="..."/></svg>`,
}

export function channelIconHtml(channel: string | undefined): string {
  if (!channel) return ''   // 空默认就是本机，不渲染
  return channelIcons[channel] || ''
}
```

#### Session 列表渲染

`_doRenderSessions`（`main.ts:1465-1535`）现有 item 结构（chevron + running-dot + name + time + split-btn + del-btn）在 `.session-name` 前插入图标。沿用现有安全渲染路径（`esc()` 转义不可信字段），仅新增 `session-source` 图标段：

```typescript
// main.ts，_doRenderSessions 内构建 item HTML（沿用现有 esc() 转义路径）
// 仅展示新增的 session-source 段；其余字段（checkbox/chevron/running-dot/name/time/split/del）保持现状
const channel = s.channel   // 直接读 ad-hoc 字段（非 s.metadata?.channel）
const iconHtml = channel ? channelIconHtml(channel) : ''

// 把下面这段插入到现有 item 模板的 .session-chevron 之后、.session-name 之前：
//   ${iconHtml ? `<span class="session-source">${iconHtml}</span>` : ""}
// name 字段继续走 esc(s.preview || s.id)，保持 XSS 安全
```

#### 视觉效果

```
[ ] 帮我重写 utils.ts                  ← 本机
[ ] 调试登录流程                        ← 本机
[✈️] 帮我总结这个 PDF                  ← Telegram（蓝色）
[💬] 帮我看下排期                      ← 飞书（青色）
[ ] 重构 auth 模块                     ← 本机
```

#### 图标规范

| 平台 | 颜色 | SVG 尺寸 | 备注 |
|---|---|---|---|
| 飞书 | `#00D6B9`（官方青） | 16×16 | 用品牌色 |
| Telegram | `#229ED9`（官方蓝） | 16×16 | 用品牌色 |

**实现注意**：SVG 必须带 `xmlns="http://www.w3.org/2000/svg"` 命名空间，否则用 `DOMParser` 解析后无法得到 `SVGSVGElement`；`web/src/main.ts` 通过 `innerHTML` 插入侧边栏图标时则不受此限制，但统一声明命名空间更稳妥。

---

## 8. IM session 可见性

与 automation 不同（`is_automation` 隐藏 session、抑制中间事件），**IM session 正常显示在侧边栏**（带来源图标），用户能看到 IM 对话历史、点进去看完整消息。`list_sessions`（`server.py`）已过滤 `is_automation`，IM session 不打该标记，故天然可见——无需改过滤逻辑。

---

## 9. 安全：sender 白名单（fail-closed）

IM bridge 接通后，**任何能给该账号发消息的人都能触发 ziva 跑 `shell` / `edit_file` 等工具**——等于把本机代码执行权限暴露给 IM 联系人。必须加 sender 白名单：

```python
# transports/im_bridge/bridge.py
def _is_allowed_sender(self, msg: IncomingMessage) -> bool:
    allowed = self._config.allowed_senders
    if not allowed:
        return False   # 未配置白名单 → 一律拒绝（fail-closed）
    return msg.sender_id in allowed
```

- `allowed_senders` 在 `~/.ziva/config.yaml` 的 `im_bridge` 下配置（飞书 open_id / 微信 wxid / Telegram user id）。
- **fail-closed**：白名单为空 → 拒绝所有消息（而非放行所有）。
- 首次收到非白名单消息时，可在桌面端弹通知"收到来自未知 sender 的消息，是否放行？"——但默认拒绝。
- 可选增强：IM session 限制可用工具集（禁用 `shell` / `edit_file`，仅留只读工具）。

---

## 10. 关键设计决策记录

| 决策 | 选择 | 否决方案 |
|---|---|---|
| Bridge 位置 | `transports/im_bridge/` 组件 | ❌ `plugins/`（语义混淆：plugin 是 agent 能力，不是入口） |
| **进程模型** | **in-process 组件，desktop_api on_startup 启动** | ❌ 独立子进程 `ziva bridge serve`（与同进程 import Runtime 互斥；子进程要么抢 session 文件锁，要么走 HTTP 调自己） |
| **后端通信** | **直接 import Runtime（同进程）** | ❌ 走 HTTP API 调自己（多一层） |
| **HTTP 端点** | **加在 desktop_api :4097** | ❌ 独立 BridgeAPIServer 第二端口（前端多一个 baseURL，零收益） |
| **会话创建** | **`store.create()` + `FileStorage.update_session` ad-hoc 字段** | ❌ 给 `SessionState` 加 `metadata` 字段（侵入核心类型 `shared_types.py`） |
| **IM turn 路径** | **走 `create_turn` / `chat_with_events`，与本地对话同路径（IM session 即普通 session）** | ❌ 复用 automation 的 `runtime.chat` 链路（automation 是无头特殊场景：`is_automation` 隐藏 + 抑制事件） |
| adapter 编排 | 单 `IMBridge` 类（adapter 生命周期 + 路由合一） | ❌ 拆 `BridgeDaemon` + `SessionRouter` 两类（路由是 dict 查表，不值得单独成类） |
| **UI 入口** | 左侧 sidebar 按钮 → 全屏 modal | ❌ 顶部图标（Ziva 主界面顶部没设置区） |
| **UI 视图** | 全屏 modal（与"自动化"同构） | ❌ 右侧 tab `im-connect`（"自动化"是 modal，一个 tab 一个 modal 不一致） |
| **前端目录** | `web/src/modals/im-bridge.ts` + `web/src/icons.ts` | ❌ 新建 `renderers/` / `components/` 目录（与现有 `modals/` 结构不一致） |
| Session 标识 | SVG 图标 | ❌ emoji（跨平台渲染不一致） |
| Session 标识 | 简单常驻图标 | ❌ hover 才显示（增加交互成本） |
| Session 标识 | 空默认就是本机 | ❌ 给本机会话加"💻"图标（视觉噪声） |
| 路由粒度 | `(channel, account_id, chat_id)` 一个 session | ❌ 每条消息一个 session（丢失上下文） |
| 配置范围 | 全局 `~/.ziva/config.yaml` 的 `im_bridge` 键 | ❌ per-workspace（IM 天然跨工作区） |
| IM session 可见性 | 正常显示（带图标） | ❌ 像 automation 一样隐藏（用户要看 IM 对话历史） |
| **sender 鉴权** | **白名单 fail-closed** | ❌ 无鉴权（任意联系人可触发本机 `shell` / `edit_file` 工具执行） |

---

## 11. 实施阶段

### Phase 0 · 微信 adapter 保留但不暴露（历史方案）

个人微信没有官方 Bot API。原方案采用**外部网关 + 二维码登录**：`WechatAdapter` 通过 WebSocket 连接到一个兼容网关，由网关负责真实的微信协议；Ziva 只负责展示二维码、接收扫描事件、收发消息。该适配器文件 `src/ziva/transports/im_bridge/adapters/wechat.py` 继续保留，但**当前 UI 与默认配置不再暴露微信入口**，因为个人微信网关需要用户自行部署且存在账号风险。

### Phase 1 · 后端骨架 + 飞书 adapter 跑通

文件：
```
src/ziva/transports/im_bridge/
├── __init__.py
├── bridge.py              # IMBridge
├── store.py               # IMConfig + 路由表持久化
└── adapters/
    ├── __init__.py
    ├── base.py            # BaseAdapter
    └── feishu.py          # lark-oapi WebSocket
```
- `desktop_api/server.py` 加 `/api/im/channels/*` 路由 + `on_startup` 启 `IMBridge`。
- `_ensure_session` 写 ad-hoc 字段（§6）。
- sender 白名单（§9）。

验证：飞书发消息 → ziva 建 session → 回飞书。

### Phase 2 · 前端入口 + 连接管理 modal

文件：
```
web/src/
├── main.ts                # 加 btnConnectIM + onclick → openIMBridgeModal
├── modals/
│   └── im-bridge.ts       # 全屏 modal（空状态 + 通道列表 + 添加按钮）
└── icons.ts               # channelIcons + channelIconHtml
```

验证：点"连接手机" → modal → 填飞书凭证 → 连接 → 状态绿。

### Phase 3 · session 来源图标

- `_doRenderSessions`（`main.ts:1465-1535`）在 `.session-name` 前插 `channelIconHtml(s.channel)`。

验证：飞书来的消息在侧边栏带青色飞书图标。

### Phase 4 · 扩展 Telegram + 图片输入 + 透明化回复

文件：
```
src/ziva/transports/im_bridge/adapters/
├── telegram.py            # Bot API 长轮询（支持 proxy_url）
└── feishu.py              # 已扩展图片下载
```

- `IncomingMessage.images` 字段承载飞书/ Telegram 接收的图片 URL / base64。
- `bridge.py` 将图片随 user message 作为 `image_url` / `image_base64` part 传入 `chat_with_events`，模型具备 vision 能力时即可理解图片。
- `chat_with_events` 事件流包含 `reasoning_delta`、`tool_start`、`tool_end`；`_format_reply` 把这些信息组装成透明化回复文本，再发回 IM。

验证：
- Telegram 发文字 / 图片 → session → 收到文字回复。
- 飞书发图片 → session → 收到图片理解回复。
- 触发工具调用时，回复里能看到工具名与结果摘要。

---

## 12. 不在范围内

- IM 文件消息（非图片的文件传输）。
- 主动推送（ziva 主动给 IM 发消息，非回复）——后续可加"任务完成通知"。
- 群聊（首版只单聊；群聊需处理 @ 机器人触发 + 噪声过滤）。
- 非 Electron 的 web/dev 端管理 IM bridge——仅桌面端配置。
