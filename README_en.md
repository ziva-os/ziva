# Ziva

[中文](README.md) | English

Ziva is a highly customizable LLM Agent runtime and desktop application. It provides a complete Python SDK for orchestrating model-tool loops, context compression, and capability registration, along with a Vite + TypeScript web frontend and an Electron native desktop shell with built-in browser automation and IM bridging.

## Overview

- **Model-Tool Loop**: Multi-turn execution, automatic context compaction, dynamic tool schemas, MCP client support.
- **Desktop App**: Electron + embedded Chromium browser with side-by-side panes and CDP automation.
- **IM Bridge**: Bring Feishu (Lark) or Telegram messages into the same Agent session, with image input support.
- **Extension System**: Dynamically load prompts, tools, hooks, and memory backends via `manifest.yaml`.
- **Voice Input**: Local GPU-accelerated transcription via `mlx-whisper` on Apple Silicon (about 461 MB model, downloaded automatically on first use).

## Project Structure

```
ziva
├── src/ziva/          # Core Python SDK (Runtime, Adapter, CapabilityRegistry)
├── web/               # Frontend UI (Vite + TypeScript)
├── electron/          # Electron desktop shell
├── plugins/           # Dynamically loaded tools, skills, and memory backends
├── scripts/           # Build scripts
├── docs/              # Architecture documentation
└── pyproject.toml     # Python package configuration
```

## Installation

### Requirements

- Python 3.10+
- Node.js 18+
- macOS (desktop app and voice input are currently fully supported on Apple Silicon; Intel Macs may work but some dependencies may need manual adjustment)

### Install the Python SDK

Using [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`:

```bash
uv pip install -e .
# or
pip install -e .
```

Install full desktop dependencies (includes aiohttp server, Feishu SDK, CLI):

```bash
uv pip install -e ".[all]"
# or
pip install -e ".[all]"
```

## Build the Desktop App

### 1. Download the Speech Recognition Model (Optional)

The desktop app supports voice input. The `mlx-community/whisper-small-mlx` model (about 461 MB) is downloaded automatically from Hugging Face on first use. If you want to download it ahead of time or specify a model manually:

```bash
# Default model: mlx-community/whisper-small-mlx
# Auto-downloaded to ~/.ziva/models/
```

You can also specify the model in `.ziva/config.yaml`:

```yaml
stt:
  model: mlx-community/whisper-small-mlx
```

> Note: You do not need to pre-download the model before packaging. The desktop app will handle it automatically on the first `/api/stt` call. If your network is restricted, you can download it manually and place it under `~/.ziva/models/mlx-community/whisper-small-mlx/`.

### 2. One-Command Build

```bash
bash scripts/build-desktop.sh
```

This script will:

1. Create `.build-venv` (Python 3.11), install Ziva and PyInstaller;
2. Build the frontend `web/` → `src/ziva/transports/desktop_api/static/`;
3. Use PyInstaller to package the Python backend into `electron/dist/ziva-backend`;
4. Compile the Electron main process and preload scripts;
5. Use `electron-builder` to produce `.dmg` and `.zip` artifacts.

Outputs:

- `electron/dist/Ziva-1.0.0-arm64.dmg`
- `electron/dist/Ziva-1.0.0-arm64-mac.zip`
- `electron/dist/mac-arm64/Ziva.app`

For the first launch of an unsigned app: right-click `Ziva.app` → Open, or run in the terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Ziva.app
```

### 3. Development Mode

Backend server:

```bash
PYTHONPATH=src uv run python -m ziva.app.cli desktop serve --host 127.0.0.1 --port 4097
```

Frontend dev server:

```bash
cd web
npm install
npm run dev
```

Electron development:

```bash
cd electron
npm install
npm run dev
```

## Quick Start (SDK)

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

## Configuration

Ziva runs with layered YAML configuration. On first launch it will generate a default config at `~/.ziva/config.yaml`. You can also refer to the example configs in the repo:

- `.ziva/config.yaml.example` — example configuration
- `.ziva/config.yaml.test` — test placeholder configuration

Core configuration blocks:

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

## Connect IM (Feishu / Telegram)

Ziva supports bringing Feishu (Lark) or Telegram messages into the Agent session. The configuration entry is the **“Connect Phone”** settings panel in the bottom-left corner of the desktop app.

### Feishu (Lark)

1. Open the [Feishu Developer Console](https://open.feishu.cn/) and click **Create Feishu Agent App** (or create a “Bot App”).
2. Go to the app details → **Credentials & Basic Info**, and copy the **App ID** (`cli_xxx`) and **App Secret**.
3. In the Ziva desktop app, click **“Connect Phone”** in the bottom-left, select **Feishu**, enter the App ID and App Secret, and click Enable.
4. Send any message to the bot in a Feishu chat (group or private).
5. Return to the **Connect Phone** panel in Ziva; the sender will now appear in the **Pending Approval** list. Click **Add with One Click** to add them to the allowlist.

### Telegram

1. In Telegram, contact [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts to create a bot. You will receive a **Bot Token** (e.g. `123456:ABC-DEF...`).
2. In the Ziva desktop app, click **“Connect Phone”** in the bottom-left, select **Telegram**, enter the Bot Token; if you are in a region where Telegram is blocked, you can also enter a proxy URL (e.g. `http://127.0.0.1:7890` or `socks5://...`), and click Enable.
3. Send any message to the bot in Telegram.
4. Return to the **Connect Phone** panel in Ziva; the sender will now appear in the **Pending Approval** list. Click **Add with One Click** to add them to the allowlist.

> The allowlist is fail-closed: an empty allowlist rejects all senders. Only IDs added to the allowlist can trigger Ziva.

## Default Workspace

IM-triggered sessions are bound to the **default workspace** (which determines the `cwd` for tool execution). If left empty, the current active workspace is used. The configuration entry is also at the bottom of the **Connect Phone** panel.

## Documentation

- [docs/architecture.md](docs/architecture.md) — Code architecture

## Contributing

PRs and issues are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

[MIT](LICENSE) © Ziva Team
