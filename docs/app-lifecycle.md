# App 生命周期管理（设计文档）

> 状态：设计文档（2026-07 讨论）。定义 ziva 如何像 OS 的服务管理器（systemd / launchd）一样管理 app 的 start/stop/restart/status/自启/重启。
> 关联：`docs/apps.md`（app 抽象）、`Runtime.build_app_index`（声明层）。

---

## 0. 目标

app 的 `serve` 是个**长驻进程（守护进程）**。ziva 作为 OS，需要一个**服务管理器**来管理这些进程：起、停、重启、查状态、开机自启、崩溃重启、看日志、一键打开。本文定义这套机制，吸收 Linux（systemd）/ macOS（launchd）/ Unix（进程监督、pidfile、信号）的经验。

---

## 1. 核心原则：声明层 vs 运行态层（systemd 精髓）

systemd 把 **unit 文件（声明）** 和 **runtime state（运行态）** 分开。ziva 照搬：

| 层 | 是什么 | 来源 | 无状态？ | OS 对应 |
|---|---|---|---|---|
| **声明层** | app 是什么、怎么 serve、自启/重启策略 | `build_app_index()` 扫 `manifest.yaml` | ✅ 每次重扫 | systemd **unit 文件** |
| **运行态层** | pid、running/stopped、uptime、restart 次数 | **AppManager**（内存 + pidfile） | ❌ 有状态 | systemd **runtime state** |
| **status 视图** | 声明 ∪ 运行态 | 两者合并 | — | `systemctl status` |

**关键：AppManager 读 `build_app_index` 知道"有哪些 app + 怎么起"，自己只管"谁在跑"。不重复扫 manifest。**

### 1.1 app 只提供 `serve`，不实现 start/stop/restart

这是最重要的分工（同 systemd：service 不实现自己的 start/stop）：

| 谁 | 职责 | OS 对应 |
|---|---|---|
| **app** | 只提供 **`serve`** 子命令（长驻 UI 后端） | unit 文件的 `ExecStart=...` |
| **ziva AppManager** | start=spawn `serve`、stop=信号、restart、track pid、日志、自启、崩重启 | **systemd**（服务管理器） |

app 的 CLI 只有它自己的功能命令（如 `goal-tracker` 的 `add/log/list/serve`）。**没有** `start/stop/restart`——那些是 ziva 在 app 的 `serve` 进程外面套的管理层。

---

## 2. id 模型

`ziva app <cmd> <id>` 里的 `<id>` = app 的 **kebab-case 短 id** = **目录名**（= `cli.cmd` = APP.md `name`）。

```
ziva app start goal-tracker      # 不是 app.goal-tracker
ziva app stop  aigc-canvas
```

区分两层 id（C1 契约）：
- **`<id>`（handle，给 CLI/agent/manager 用）**：`goal-tracker` ← 目录名。
- **`manifest.id`（命名空间形式，loader 内部用）**：`app.goal-tracker`。

`build_app_index()` 应把短 id（dir name）作为主键暴露出来（当前返回的 `id` 是 `app.x`，需补一个短 id 字段，或 manager 直接按 dir name 索引）。

---

## 3. 声明层：manifest 补 serve 声明（unit 增强）

现有 `cli.cmd` + `ui.url`（含端口）已够推导"怎么起"。补两个策略字段：

```yaml
# ~/.ziva/apps/<id>/manifest.yaml
cli: {cmd: goal-tracker}
ui:  {url: http://localhost:7531}     # 端口从这里 parse
serve:
  autostart: false          # 开机自启（systemd enable / launchd RunAtLoad）
  restart: on-failure       # none | always | on-failure（systemd Restart=）
  # cmd: 可选，覆盖默认 "<cli.cmd> serve --port <port>"（非标准 serve 才用）
```

`build_app_index()` 在返回里加：`port`（parse 自 ui.url）、`autostart`、`restart`、`serve_cmd`（默认 `<cli.cmd> serve --port <port>`，或 manifest 覆盖）。仍是**纯声明、无状态**。

---

## 4. 运行态层：AppManager

### 4.1 状态

```
内存: _running = { id: {pid, port, status, started_at, restart_count, pgid} }
盘:   ~/.ziva/apps/<id>/app.pid         ← pidfile（跨重启恢复，Unix 传统）
       ~/.ziva/apps/<id>/logs/serve.log ← stdout/stderr（journal 等价）
```

### 4.2 操作（Unix 词汇）

- **`start(id)`**：从 `build_app_index` 拿声明 → spawn `serve_cmd`，**backend 当父进程**（Unix supervision：父进程才能可靠感知子进程死亡）→ 起在**独立进程组**（能对整树发信号）→ 重定向 stdout/stderr 到 `serve.log` → 写 `app.pid` → 记内存。**幂等**：已在跑就 no-op（desired-state）。端口被占明确报错。
- **`stop(id)`**：`SIGTERM` 进程组 → 等待（如 10s）→ 超时 `SIGKILL`（systemd TimeoutStopSec + SIGKILL 兜底）→ 清内存 + pidfile。
- **`restart(id)`**：stop + start。
- **`status(id)`**：`build_app_index` 的声明 ∪ `_running[id]` 运行态 → `{声明字段..., running, pid, port, uptime, restart_count}`。
- **`is_running(id)`**：`os.kill(pid, 0)` / poll 检活。

### 4.3 生命周期（backend = systemd，常驻管理器）

- **boot**（`_on_startup`）：对 `autostart: true` 的逐个 `start`；并 **adopt**——扫各 app 的 pidfile，若 pid 还活着（backend 重启过、app serve 没死）重新纳入管理（systemd/launchd 重启后的状态恢复）。
- **quit**（`_on_cleanup`）：对所有 running app 发 SIGTERM（优雅停，和现有 backend 退出的 `killBackendTree` 一致）。
- **崩重启**：一个监督任务轮询各 pid；非主动 `stop` 的死亡 + `restart != none` → 按退避（指数 backoff，封顶）重启（systemd `Restart=on-failure`）。

---

## 5. 控制面：`ziva app` CLI + 管理 HTTP（systemctl ↔ systemd）

`ziva app` 是**控制客户端**（不自己 fork，找 backend 管）；backend 暴露 HTTP：

```
POST /api/apps/<id>/{start,stop,restart}
GET  /api/apps/<id>/status
GET  /api/apps?with_state=1            # list = 声明 ∪ 运行态
```

```
ziva app list                       # 声明 + running/stopped/pid/port/uptime
ziva app start|stop|restart <id>
ziva app status <id>
ziva app enable|disable <id>        # 持久化 autostart（systemctl enable 等价）
ziva app logs <id>                  # tail serve.log（journalctl 等价）
ziva app open <id>                  # start(若没跑) + 内部浏览器导航到 ui.url
ziva app install|uninstall <pkg>    # 装/卸（apt/brew 等价；可与上面共用 CLI）
```

> **澄清**：这里的 HTTP 是**管理通道**（systemctl↔systemd 也是 client-server）。跟"agent 发现 app"是两回事——**agent 发现 app 仍走 system prompt 注入**（`# Available Apps`）；管理 app 走这套 HTTP/CLI。

---

## 6. `open`：一步"打开 app"

`ziva app open <id>` = `start`（若没跑）+ 内部浏览器导航到 `ui.url`。macOS `open -a App` 的等价。

需要：
- 注入里**带上 `ui_url`**（当前注入只有 name+cli_cmd+desc，agent 不知道端口）——`build_app_index` 已有 `ui_url`，注入时加上。
- backend 有个"在内部浏览器开 tab"的能力（已有 pinned-tab 浏览器 + CDP 桥；`open` 走它）。

---

## 7. 吸收的 Unix/Linux 经验

✅ 吸收：
- **声明 vs 运行态分离**（unit vs runtime）—— `build_app_index` 声明，AppManager 运行态，status 合并。
- **supervisor 当父进程**（systemd/runit/s6）—— backend spawn，可靠感知死亡。
- **进程组 + 信号**（SIGTERM 优雅 / SIGKILL 强制）—— 统一控制词汇。
- **pidfile**（Unix 传统）—— 可 inspect、跨重启恢复、adopt-on-boot。
- **日志落文件**（journal）—— `ziva app logs` 可查。
- **enable ≠ start**（systemctl）—— 自启与"现在跑"分开。
- **幂等 desired-state**（start 已在跑则 no-op）。
- **Restart 策略**（on-failure/always/none）+ 退避。
- **launchd RunAtLoad / KeepAlive** → autostart / restart。

❌ 刻意避免（别过度设计）：
- systemd 依赖图（After/Wants/Requires）—— ziva app 多半独立。
- socket activation。
- cgroup 资源控制（将来做沙箱时再说）。

---

## 8. 文件 / 状态布局

```
~/.ziva/apps/<id>/
  manifest.yaml          # 声明（含 serve.autostart/restart）
  APP.md
  cli/<id>               # 含 serve 子命令
  ui/
  data/
  app.pid                # 运行态：当前 serve 的 pid（AppManager 写）
  logs/serve.log         # 运行态：serve 的 stdout/stderr
```

声明（manifest/APP.md/cli/ui/data）与运行态（app.pid/logs）同居一个 app 目录，但语义分层。

---

## 9. 安全 / 边界

- **app serve 跑在什么权限**：沿用"两档信任"（docs/apps.md 设计取向）——本地可信 app 以 ziva 身份跑；分享/不可信 app 应沙箱（容器/独立 UID），将来补。
- **restart 退避**：崩重启要有指数退避 + 封顶，避免死循环刷日志/烧 token（若 serve 调 agent.run）。
- **端口冲突**：start 时端口被占 → 明确报错（C9），不静默换端口。
- **pidfile 竞态**：adopt-on-boot 时校验 pid 确实是对应的 serve 进程（避免误杀）。

---

## 10. 实现顺序

1. manifest 补 `serve.{autostart,restart}` + `build_app_index` 多返回 `port/autostart/restart/serve_cmd`（短 id 也理顺）。
2. **AppManager**（runtime）：start/stop/restart/status/is_running + pidfile/logs + 进程组/信号 + boot 自启 + quit 停 + 崩重启（监督任务）。
3. **`ziva app` CLI** + 管理 HTTP（systemctl 客户端）。
4. **`open`**（+ 注入带 ui_url + 内部浏览器开 tab）。
5. （后续）install/uninstall、沙箱、enable 持久化覆盖。

---

## 11. 一句话

**`build_app_index` 是 app 的"unit 文件"（声明层）；AppManager 是叠在上面的运行态监督器（pid/status/日志/信号/自启/重启）；`ziva app` CLI 经 HTTP 控制它（systemctl 模式）。app 只需提供 `serve`，start/stop/restart 是 ziva 作为 OS/服务管理器的职责。id 用短 kebab（目录名）。**
