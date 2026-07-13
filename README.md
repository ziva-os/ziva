# Ziva

[English](README_en.md) | 中文

Ziva 是一个可高度定制的 LLM Agent 运行时与桌面应用。它提供了一个完整的 Python SDK 用于编排模型-工具循环、上下文压缩与能力注册，并配套一个 Vite + TypeScript 网页前端和 Electron 原生桌面壳，内置浏览器自动化与 IM 桥接能力。

## 功能概览

- **模型-工具循环**：多轮执行、上下文自动压缩、动态工具 schema、MCP 客户端支持。
- **桌面应用**：Electron + Chromium 内嵌浏览器，支持侧边分屏与 CDP 自动化。
- **IM 桥接**：将飞书（Lark）或 Telegram 消息接入同一个 Agent 会话，支持图片输入。
- **扩展系统**：通过 `manifest.yaml` 动态加载 prompt、tool、skill、hook、memory 后端。
- **语音输入**：Apple Silicon 上通过 `mlx-whisper` 本地 GPU 加速转写（约 461 MB 模型，首次使用自动下载）。

## 项目结构

```
ziva
├── src/ziva/          # 核心 Python SDK（Runtime、Adapter、CapabilityRegistry）
├── web/               # 前端 UI（Vite + TypeScript）
├── electron/          # Electron 桌面壳
├── plugins/           # 动态加载的工具、技能、内存后端
├── scripts/           # 构建脚本
├── docs/              # 架构文档
└── pyproject.toml     # Python 包配置
```

## 安装

### 环境要求

- Python 3.10+
- Node.js 18+
- macOS（桌面版与语音输入当前仅完整支持 Apple Silicon；Intel Mac 可尝试但部分依赖需自行调整）

### 安装 Python SDK

使用 [`uv`](https://docs.astral.sh/uv/)（推荐）或 `pip`：

```bash
uv pip install -e .
# 或
pip install -e .
```

安装完整桌面依赖（包含 aiohttp 服务端、飞书 SDK、CLI）：

```bash
uv pip install -e ".[all]"
# 或
pip install -e ".[all]"
```

## 构建桌面版

### 1. 下载语音识别模型（可选）

桌面版支持语音输入。首次使用时会自动从 Hugging Face 下载 `whisper-small-mlx`（约 461 MB）。如果你希望提前下载或手动指定模型，可以：

```bash
# 默认模型：whisper-small-mlx
# 自动下载到 ~/.ziva/models/
```

也可以在 `.ziva/config.yaml` 中指定模型：

```yaml
stt:
  model: whisper-small-mlx
```

> 提示：打包时不需要预下载模型，桌面端首次调用 `/api/stt` 时会自动处理。若网络受限，可手动下载后放到 `~/.ziva/models/mlx-community/whisper-small-mlx/`。

### 2. 一键构建

```bash
bash scripts/build-desktop.sh
```

该脚本会：

1. 创建 `.build-venv`（Python 3.11），安装 ziva 与 PyInstaller；
2. 构建前端 `web/` → `src/ziva/transports/desktop_api/static/`；
3. 用 PyInstaller 将 Python 后端打包成 `electron/dist/ziva-backend`；
4. 编译 Electron 主进程/预加载脚本；
5. 用 `electron-builder` 生成 `.dmg` 与 `.zip`。

产物：

- `electron/dist/Ziva-1.0.0-arm64.dmg`
- `electron/dist/Ziva-1.0.0-arm64-mac.zip`
- `electron/dist/mac-arm64/Ziva.app`

首次打开未签名应用：右键 `Ziva.app` → 打开，或在终端执行：

```bash
xattr -dr com.apple.quarantine /Applications/Ziva.app
```

### 3. 开发模式启动

后端服务：

```bash
PYTHONPATH=src uv run python -m ziva.app.cli desktop serve --host 127.0.0.1 --port 4097
```

前端开发服务器：

```bash
cd web
npm install
npm run dev
```

Electron 开发：

```bash
cd electron
npm install
npm run dev
```

## 快速开始（SDK）

```python
from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage

runtime = Runtime.from_config({
    "model": {
        "name": "MiniMax-M2.7",
        "provider": "openai",
        "api_key": "YOUR_API_KEY",
        "base_url": "https://api.minimaxi.com/v1",
    },
    "prompt": {"system_prompt": "You are a helpful coding assistant."},
    "tool": {"max_rounds": 10},
    "memory": {"context_window_tokens": 200000},
    "approval": {"policy": "full-auto"},
})

result = runtime.chat([ChatMessage(role="user", content="Hello, Ziva!")])
print(result.content)
```

## 配置

Ziva 通过分层 YAML 配置运行。首次启动会在 `~/.ziva/config.yaml` 生成默认配置，也可参考仓库内的：

- `.ziva/config.yaml.example` — 示例配置
- `.ziva/config.yaml.test` — 测试用占位配置

核心配置块：

```yaml
model:
  provider: openai
  name: MiniMax-M2.7
  api_key: YOUR_API_KEY
  base_url: https://api.minimaxi.com/v1

prompt:
  system_prompt: "You are a helpful coding assistant."

tool:
  max_rounds: 10
  allow: []
  deny: []

memory:
  context_window_tokens: 200000

plugin:
  paths:
    - ./plugins
```

## 连接 IM（飞书 / Telegram）

Ziva 支持将飞书（Lark）或 Telegram 消息接入 Agent 会话。配置入口在桌面应用左下角的 **“连接手机”** 设置面板。

### 飞书（Lark）

1. 打开 [飞书开发者后台](https://open.feishu.cn/)，点击 **创建飞书智能体应用**（或创建“机器人应用”）。
2. 进入应用详情 → **凭证与基础信息**，复制 **App ID**（`cli_xxx`）和 **App Secret**。
3. 在 Ziva 桌面应用左下角点击 **“连接手机”**，选择 **飞书**，填入 App ID 与 App Secret，点击启用。
4. 在飞书会话（群聊或私聊）中向机器人发送一条任意消息。
5. 回到 Ziva 的 **连接手机** 面板，此时发送者会出现在 **“待审批”** 列表中，点击 **一键添加** 即可加入白名单。

### Telegram

1. 在 Telegram 中联系 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 并按提示创建 Bot，获得 **Bot Token**（例如 `123456:ABC-DEF...`）。
2. 在 Ziva 桌面应用左下角点击 **“连接手机”**，选择 **Telegram**，填入 Bot Token；若在国内访问，可一并填入代理 URL（如 `http://127.0.0.1:7890` 或 `socks5://...`），点击启用。
3. 在 Telegram 中向该 Bot 发送一条任意消息。
4. 回到 Ziva 的 **连接手机** 面板，此时发送者会出现在 **“待审批”** 列表中，点击 **一键添加** 即可加入白名单。

> 白名单是“fail-closed”的：空白名单会拒绝所有发送者。只有加入白名单的 ID 才能触发 Ziva。

## 默认工作区

IM 触发的会话会绑定到 **默认工作区**（决定工具执行时的 `cwd`）。留空则使用当前活跃工作区。配置入口同样在 **连接手机** 面板底部。

## 文档

- [docs/architecture.md](docs/architecture.md) — 代码架构

## 贡献

欢迎 PR 与 Issue！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE) © Ziva Team
