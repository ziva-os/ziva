# Ziva Apps（应用抽象）

> 状态：设计文档。记录 ziva 上「app」的概念、接口、注册与交互模型。
> 来源：2026-07 架构讨论。本文只定义抽象与约定；具体实现进度见文末「范围与后续」。

---

## 0. 心智模型

**ziva 是一个 OS；app 是跑在 OS 上的一个功能单元。**

从最简单的（任务管理、股票助手）到平台级的（类 Dify 工作流平台、AIGC 创作平台）都是 **app**——它们复杂度不同，但抽象一致。ziva 提供"跑 app、和 app 交互、给 app 提供能力"的运行环境；app 是其中独立、可替换、可分发的功能。

**用 Linux 类比（理解整套抽象的锚点）**：

| Linux | ziva |
|---|---|
| 内核 | ziva runtime——管理特权资源（LLM/agent、工具、知识库、存储） |
| 系统调用（syscall） | `agent.run`（+ 工具/知识/存储 API）——app 请求内核服务的稳定接口 |
| 内核提供的**服务**（非垄断资源） | **LLM 是 ziva 提供的服务**：app 经 `agent.run` 复用 ziva 配置好的模型/工具/scope/额度（推荐）。但 LLM **不像磁盘/网络那样是内核独占**——app 作为普通程序也能**自带 key 直连** LLM，ziva 不（也无法）强制禁止 |
| 用户态程序 | **app**：有自己的代码与内部逻辑（含工作流引擎），对 ziva **不透明** |
| shell | **对话 agent**：面向用户、调用 app、以用户全权限运行 |
| 受限权限的子进程 | app 内部经 `agent.run` 起的 **scoped sub-agent** |
| IPC / 管道 | app 之间通过各自 CLI/MCP 工具接口**互相调用**组合（不靠 ziva 居间） |

两条推论（贯穿全文）：
1. **一个内核、多个调用者**：对话 agent 和 app 内的 agent 都是 `agent.run` 的调用者，区别只在"谁触发、什么 scope"——不存在两套 agent 体系。
2. **app 的内部逻辑对 ziva 不透明**：ziva 只看到 syscall 请求；一个"含 agent 调用的工作流"app 对 ziva 而言和普通 app 没区别（见 §7）。

---

## 设计取向：CLI-first 与借鉴取舍

ziva 是 **LLM-agent 原生 OS**，不是手机/GUI OS。两条取向：

**CLI-first**：app 的程序化接口**默认走 CLI**（agent 用 shell 工具调，不合成工具）；**MCP 为可选**，留给 ①需要 typed 工具 / 富输出直回 agent 的复杂 app、②不可信 / 需沙箱的 app、③外部 MCP 生态。决策轴：简单 / 本地 / 自带 UI（富内容走 UI）→ CLI；复杂 / typed / 不可信 → MCP。

**借鉴 Unix/Android 的"原则"，用 LLM-native 的"机制"实现，拒绝 GUI-OS 的"机器"。**

| 借鉴对象 | 原则 | ziva 形态 | 取舍 |
|---|---|---|---|
| **Unix 管道 / 统一 I/O** | `a \| b \| c` 自由组合 | app CLI 吃 stdin / 吐 stdout；agent 的 shell 支持 `\|` → **几乎免费拿到 Unix 级组合** | ✅ 采纳（CLI-first 最大优势） |
| **per-app 权限 + 沙箱** | 每 app 身份 + 声明权限 | **两档**：本地可信 app 以 ziva 身份跑（轻）；分享/不可信 app 必须沙箱。⚠️ **CLI 比 MCP 难沙箱**（subprocess spawn 后难拦）→ 不可信 app 优先 MCP 或容器化 CLI | ✅ 采纳（两档）；改造（不全员内核沙箱） |
| **签名 + 安装 + 依赖** | APK / PackageManager | bundle + 运行时声明 + `ziva app install` + shim + 签名校验 | ✅ 后阶段（为分享/市场） |
| **生命周期** | 系统管组件启停 | app `serve` 长跑子进程，ziva 状态机（installed→enabled→running→stopped）+ 崩溃重启 | ✅ 轻量采纳 |
| **退出码 / stderr / env** | Unix 约定 | app 遵守退出码 + stderr 报错；ziva 注入 `ZIVA_APP_DATA` / scope | ✅ 立规范，零机制成本 |
| **Intent（声明式能力/解耦）** | app 声明能力、可替换、解耦调用 | **用 NL 能力描述（APP.md）+ LLM 解析**，不要 Android 的 action/MIME 机器 | 🔧 原则采纳、机器拒绝；组合复杂时才上显式能力标签 |
| **ContentProvider** | 标准 query/URI 数据共享 | CLI 的 `export`/`query` + agent 居间已覆盖 | ❌ 拒绝 |
| **四类组件**（Activity/Service/...） | GUI app 结构切分 | CLI app = `serve`(UI) + 命令 + 后台(用 automation) | ❌ 拒绝 |
| **rigid intent bus / 全员内核沙箱** | — | LLM 解析取代 intent bus；沙箱只给不可信档 | ❌ 拒绝 |

**ziva 原生优势（不是借鉴，要用足）**：对话 = 通用 UI（不需 Activity 体系）、LLM = 灵活解析器（取代死板 intent）、`agent.run` = 共享智能（app 更薄）、app 可由 ziva 代码生成。

> 一句话：**CLI-first 让 Unix 的"管道组合 + 统一 I/O + 退出码/env 约定"几乎免费拿到（最大赢家）；权限/沙箱走"两档信任 + CLI 难沙箱故不可信走 MCP/容器"；签名安装为分享后做；Android 的组件/ContentProvider 在 CLI-first 下更不需要。**

---

## 1. App 是什么

一个 app 是**一个遵循约定的代码项目**（不是运行时被某个引擎拼装出来的），由**人或 ziva 写代码**来创建和迭代。

每个 app 包含：

| 组成 | 是否必须 | 说明 |
|---|---|---|
| **UI** | 必须 | app 自带的界面，一个正常的 web 应用 |
| **CLI 或 MCP（二选一）** | 必须 | 程序化 / agent 接口，提供"不经过 UI 操作 app"的能力 |
| **manifest** | 必须 | 描述 app 的元数据（id、name、ui、cli/mcp、runtime 等） |
| **复用 ziva agent runtime** | 可选 | app 调用 ziva 的 agent runtime 实现自己的智能逻辑 |

两条硬约定：

1. **app 必须自带 UI**。不存在"ziva 替 app 生成一个通用面板"。UI 是 app 自己的代码。
2. **CLI 和 MCP 能力等价，二选一**。app 按自身情况挑一个提供，不需要都做。

---

## 2. App 的三个面

```
                 ┌──────── app 共享 core（数据 + 业务逻辑）────────┐
                 │                                                  │
        Web UI（给人）            CLI 或 MCP（给 agent / 程序化）
     正常 web 应用：前端+后端+存储     不经过 UI 操作 app 的接口
```

- **UI（给人）**：正常 web 应用。**展示为主 + 人工兜底**。它**不走 CLI**，是 app 自己的前后端。
- **CLI / MCP（给 agent，二选一）**：提供"不经过 UI 操作 app"的能力。
  - **CLI**：一个 shell 命令/二进制。**不合成工具**——它就是个 shell 程序，agent 用 ziva 已有的 shell 工具直接调用（如 `mmx ...`、`goal-tracker ...`），靠 **APP.md**（像 skill 一样加载）知道它存在、怎么用。
  - **MCP**：一个 MCP server。ziva 连接它，server 暴露的工具**原生**进 tool registry。
  - 二者**能力等价**（都能让 agent 不经过 UI 操作 app），机制不同：CLI 走 shell + APP.md（像 skill 一样加载）发现；MCP 走连接 + 原生工具。两边都不"合成"假工具。
- **agent runtime 复用（给 app 自己）**：app 内部可调用 ziva 的 agent runtime 做推理/生成（见 §7）。

---

## 3. 交互模型

- **主路径是对话**：用户 ↔ ziva agent ↔ app（agent 通过 app 的 CLI/MCP 操作它）。
- **UI 的定位是「展示 + 人工兜底」**：平时 agent 通过对话把 app 用起来，UI 主要用来给用户看结果；用户也可以直接在 UI 上手动操作（兜底）。
- app 的智能（如周报总结、内容生成）由**复用 ziva agent runtime** 实现，而不是 app 自己塞一套 LLM 栈。

---

## 4. App 的注册（让 agent 能用上 app）

app 通过它的程序化接口（CLI 或 MCP）让 agent 操作。两种接口、ziva 的接法不同，但**能力等价**：

| app 提供 | ziva 怎么接 | agent 怎么用 |
|---|---|---|
| **MCP** | ziva 连接它的 MCP server | server 暴露的工具**原生**进 tool registry，agent 像调普通工具一样调 |
| **CLI** | **不需要特殊注册**——CLI 就是个 shell 命令 | agent 用 **ziva 已有的 shell 工具**直接跑（`mmx ...`、`goal-tracker ...`）；靠 **APP.md** 知道这个 CLI 存在、怎么用 |

> **关键：CLI app 不合成工具。** `mmx`、`goal-tracker` 这种 CLI 就是个 shell 程序，ziva 已经有 shell 工具，agent 直接调用即可——不需要把它的命令"合成"成 `mmx.xxx` 工具。**CLI 的发现靠 APP.md（像 skill 一样加载），执行靠 shell。**

**「CLI = MCP」指能力等价**（都能让 agent 不经过 UI 操作 app），不是机制相同：MCP = 连接 + 原生工具；CLI = shell 调用 + APP.md 发现。两边都不"合成"假工具——MCP 工具是 server 真实暴露的，CLI 调用是 shell 真实执行的。

注册/接入的边界：
- **CLI 位置**：先查 PATH、再查 bundle 的 `bin/`。
- **信任/审批**：shell CLI = 任意代码，等同 plugin；首次调用走 ziva 的 approval（分享来的尤其）。
- **UI 怎么开**：manifest 的 `ui.url`，ziva 用已有浏览器（pinned-tab）打开。

---

## 5. ziva 的角色（作为 OS）

| 角色 | 职责 |
|---|---|
| **对话 agent** | 用户聊天 → agent 通过 app 的 CLI/MCP 操作 app |
| **app runtime** | 加载 apps（`load_apps`，**渐进式**：启动只建索引——app registry + 每个 app 的 `APP.md` 注册成 skill stub；MCP 连接 / UI 启动 / APP.md 全文**按需激活**）；每个 app 独立存储 `~/.ziva/apps/<id>/data/` |
| **agent runtime 复用** | 把 ziva 的 agent runtime（scoped）提供给 app 调用 |
| **app 生成** | ziva 写代码来创建或扩展 app（见 §6） |

---

## 6. App 的生成与扩展 = 写代码

- **ziva 生成 app** = 按本文约定**脚手架出一个代码项目**（含 UI、CLI/MCP、manifest）。
- **ziva 扩展 app**（如"给目标追踪加个周报页"）= **编辑那段代码**。

这两件事本质都是**编码**——ziva 本来就会写代码（goal-tracker 就是它写的）。**不是运行时机制，更不是 auto-UI。**

> ⚠️ 概念区分：
> - **auto-UI（已否决）**：ziva 在运行时根据 schema 生成一个通用面板替代 app 的 UI。被否决，因为 app 必须自带 UI。
> - **ziva 生成 app（保留）**：ziva 一次性写出完整 app 的代码（含它自己的 UI），产物是一个正常 app。这是代码生成，跟 auto-UI 是两回事。

推论：框架该做的不是"自动 UI"，而是**定一套清晰、好写、人和 ziva 都能照着写的 app 项目约定**（见 §8）。ziva 作为写代码的 agent，自然就能生成和扩展 app。

---

## 7. agent runtime 复用（app → ziva）

app 可以复用 ziva 当前的 agent runtime 来实现自己的逻辑（而不是自带 LLM/keys/工具栈）。

- **`agent.run`（scoped、tagged sub-agent）**：app 请求 ziva 跑一轮 agent。
  - 入：`{prompt, tools?, context?, model?, agent_id?}`
  - 出：`{result}`
  - **scoped**：只挂 **app 声明的工具集**（复用 ziva sub-agent 的 `_allowed_tools`），app 没法借 agent 去跑 shell/读写文件。
  - **隔离靠 scope + 标记，不是"不持久化"**：sub-agent 的消息会落盘并打 `is_subagent` 标记，**和用户主会话分开存、分开显示**，不混进去——但它**能持久化**。
  - **一次性 或 持久**：不传 `agent_id` = 一次性（跑完即返）；传 `agent_id` = 复用/新建一个 scoped、tagged 的持久 agent，跨调用保留上下文（适合"有记忆"的 agent：调研、copilot）。
- **入口**（任选）：
  - HTTP：`POST /api/agent/run`（app HTTP 打 ziva）
  - CLI：`ziva agent run "..."`（app shell 调）
  - 底下同一个 runtime，任何语言的 app 都能借到智能。

> 闭环例子：goal-tracker 每周 `ziva agent run "总结本周打卡并给建议" --tools goal_tracker` → scoped agent 读自己的数据 → 写总结 → 存回 → UI 展示。app 自身一行 LLM 代码都没有。

### 工作流型 app（app 内部多次调用 agent）

用 §0 的 Linux 类比：一个"含 agent 调用的工作流"app = **一个会发很多 `agent.run` 系统调用的用户态程序**。对 ziva 而言它和普通 app **没区别**——ziva 只看到一连串 syscall 请求，**工作流本身是 app 的私有逻辑，ziva 不感知，也不需要新抽象**。（类比：Linux 内核对一个会 fork 很多子进程、发很多 I/O 的程序，和对普通程序一样，只看到 syscall 流。）

- **不是"两层 agent"，而是"一个内核、多个调用者"**：对话 agent（shell）触发 app 的某个操作（如 `run_research(topic)`）；app 的工作流在自己的用户态代码里，按需发起多个 `agent.run`——每个是一个 scoped 子进程（需要"有记忆"的步骤可传 `agent_id`，让该 agent 跨步/跨调用保留上下文）。
- **工作流引擎是 app 自带的**（图 / 脚本 / 流水线，app 自己挑）；ziva 只提供 `agent.run` syscall。manifest 照常 `runtime.uses_agent: true` + scoped 工具。
- **scope = 权限**：对话 agent 是用户全权限的 shell；app 内部 agent 是受限子进程（只挂 app 声明的工具）。ziva 按调用者强制。
- **进度与结果由 app 自己的 UI 展示**（节点状态、流式输出）；`agent.run` 支持流式返回事件，app 转发给自己的 UI。**长任务**由 app 自己管（返回 job id / 异步写 `data/`）。
- **组合 = IPC**：工作流的 agent 若被 scoped 进别的 app 的工具（或本 app 自己的工具，如 `knowledge.query`），就能跨进程/跨 app 调用；注意限制递归深度。

> 可选后续：若很多 app 都需要相似的编排（并行 / 分支 / 验证循环），ziva 可再加一个"工作流 syscall"（基于 `agent.run` 的引擎）供复用——但**不是抽象的前提**，app 现在自己编排就能跑。

---

## 8. 项目结构与 manifest 约定

```
~/.ziva/apps/<app-id>/
  manifest.yaml        # 元数据 + 接口声明（loader 读）
  APP.md               # app 的完整说明（做什么 / 怎么操作：CLI 命令·MCP 用法·UI；像 skill 一样渐进式加载）
  ui/                  # app 自带 UI（web 应用：前端 + 后端）
  cli/  或  mcp/        # 二选一：CLI 或 MCP server
  data/                # 这个 app 的实例数据（隔离、独立）
```

### manifest.yaml

```yaml
id: app.goal_tracker          # 必须含命名空间分隔符
name: 目标追踪
version: 0.1.0
description: 记录和可视化目标、每日打卡、进度。

ui:
  url: http://localhost:7531   # app 自带 UI 的地址（ziva 用浏览器打开）

# 程序化接口二选一 ─────────────
cli:                           # 选 CLI 时
  cmd: goal-tracker            # shell 命令（PATH 或 bundle/bin/）
# mcp:                          # 选 MCP 时
#   transport: stdio | http
#   cmd: goal-tracker-mcp       # 或 url: http://...

runtime:
  uses_agent: true             # 这个 app 会复用 ziva agent runtime
  agent_tools: [goal_tracker.*]# app 触发的 agent 允许用的工具（scoped）

permissions:
  agent: [run]                 # 允许 agent 操作；首次可走 approval
```

### APP.md：app 的完整说明（像 skill 一样加载）

**每个 app 都有一份 `APP.md`**——介绍这个 app 的全部信息：它是干什么的、怎么操作（CLI 的命令参考 / MCP 的用法 / UI 在哪）、使用示例、注意事项。它就是 app 的"说明书"，给人看、也给 agent 看。

`APP.md` **以 skill 的方式渐进式加载**：启动时只把名字 + 一句摘要放进索引；agent 真要用这个 app（或用户打开它）时，才加载 `APP.md` 全文。一次会话里没碰到的 app，全文不进 context。

对不同接口的作用：
- **CLI app**：`APP.md` 是 agent 的**命令参考**——CLI 不进 tool registry，agent 靠 `APP.md` 知道有哪些命令、怎么调，再用 ziva 的 shell 工具执行。**没有"合成工具"这一步**（`mmx`、`goal-tracker` 就是 shell 程序，直接跑）。
- **MCP app**：工具由 server 连上后原生暴露；`APP.md` 提供**上下文与用法指引**（这个 app 适合做什么、典型工作流），帮 agent 决定何时用。

示例（CLI app 的 APP.md 片段）：

```markdown
# goal-tracker
目标追踪。常用命令：
- `goal-tracker add --title "..." --due YYYY-MM-DD`   新建目标
- `goal-tracker log --task 阅读 --value 30`           记录今日进度
- `goal-tracker list`                                  列出目标
需要细节可直接 `goal-tracker --help`。
```

> CLI 自带的 `--help`（可选机器可读的 `--help-json`）是给人、给 `APP.md` 作者、给 agent 现场查的，**不是 ziva 用来合成工具的输入**。

### App 的加载（渐进式）与存储路径

app 和 skill/tool 一样是**一等公民**：有固定的安装位置、自己的加载器、独立的存储路径。

**安装位置**（用户安装的 app 放这里，代码 + 数据在一起）：

```
~/.ziva/apps/<app-id>/
  manifest.yaml        # 声明（loader 读）
  APP.md               # app 的完整说明（像 skill 一样加载）
  ui/                  # 自带 UI
  cli/ 或 mcp/         # 程序化接口
  data/                # ← 这个 app 的实例数据（状态 / run 历史）；隔离、独立
```

> 类比：skill 在 `plugins/skills/` 由 `load_plugins` 加载；app 在 `~/.ziva/apps/` 由 `load_apps` 加载。区别：app 有每实例 `data/`，skill 没有。

**加载器 `load_apps`（渐进式，类比 skill）**：app 不在启动时全部加载，而是**索引常驻、按需激活**。

**索引层**（启动时，eager，便宜）——`load_apps` 只做这些：
- 扫描 `~/.ziva/apps/*/manifest.yaml`；
- 登记进 **app registry**（id / name / version / ui_url / type）；
- 每个 app：把它的 `APP.md` 注册成一条 **skill stub**（名字 + 摘要，不是全文）进 skill 系统；MCP app 额外记一条"待连接"条目（**先不连**）。

**激活层**（按需，lazy，用到才付代价）：
- `APP.md` 被触发 → 加载全文（agent 了解这个 app 怎么操作；CLI app 据此用 shell 调）；
- **MCP app**：agent 第一次要用 / 用户打开 → 连接 server → 工具进 capability registry；
- **UI**：用户打开 app → 起 `serve` 后端 → 开 URL；关掉就停；
- **data/**：app 第一次写时确保目录。

> 类比 skill：启动时只列 skill 名字/描述，全文是触发时才进 context。app 同理——**一次会话里大多数 app 根本碰不到，不为它们付连接 / 启动 / 加载全文的代价。**

**存储路径**：`~/.ziva/apps/<id>/data/` 是该 app 的**私有数据目录**——CLI 往这里写、UI 后端读写这里。每个 app 互相隔离，不与 ziva 的 sessions/automations 等混在一起；好处：好备份、好清理、**升级 app 代码不丢实例数据**。

---

## 9. 案例映射（同一抽象的不同实例）

| app | UI（自带） | CLI/MCP（agent 用） | 复用 agent runtime |
|---|---|---|---|
| **任务管理** | 目标/进度看板 | `add / log / remind / list` | 周报总结 |
| **股票助手** | K 线/走势图 | `quote / history / watchlist / news` | 新闻摘要 |
| **类 Dify 平台** | 节点编辑器 | `flow new / add-node / connect / run`（图能纯命令搭建/编辑/运行） | 每个 LLM/检索/工具节点 |
| **AIGC 平台** | 视频画布/时间线 | `aigc new-canvas / add-clip / render` | 生成节点 |

共同点：**对话是主路径，UI 是展示+兜底，agent 经 CLI/MCP 操作，需要智能时复用 ziva agent runtime。** 平台级 app（Dify/AIGC）只是 UI 更重、回调 runtime 更多，抽象不变。

---

## 10. 范围与后续

**本文定义（已对齐）：**
- app 的概念（自带 UI + CLI|MCP + manifest + 可复用 agent runtime）
- 三个面、交互模型、注册机制、生成=写代码

**后续工作（未在本文敲定，分阶段）：**
1. 内核：`agent.run`（scoped，HTTP + CLI 入口）。
2. app runtime：**索引层**（扫 manifest → app registry + 每个 app 的 `APP.md` 注册成 skill stub）+ **激活层按需**（APP.md 全文 / MCP 连接 / UI 启动，用到才加载）。
3. APP.md / manifest 规范的正式实现与校验。
4. 打包 / 分享 / 云端部署（bundle 分发、marketplace、remote 部署）。
5. 安全：分享 app 的沙箱与签名。

**落地顺序建议：**
① 内核 `agent.run` → ② app runtime + 注册加载 → ③ 用「任务管理」做第一个验证 app（跑通 对话→CLI/MCP→UI 闭环）→ ④ 平台级 app（类 Dify / AIGC）验证重型 UI + 深度回调 runtime。
