# Ziva

Ziva is a highly modular, codex-like AI agent runtime and desktop application. 
It features a full-fledged Python SDK for agent orchestration, a Vite+React web frontend, and an Electron native desktop shell capable of side-by-side browser automation.

## Project Structure

- **`src/ziva`**: The core Python SDK. Handles Model-Tool loops, token compaction, MCP clients, and capability registries.
- **`web/`**: The frontend UI. Implements the chat layout, SSE real-time streaming, and terminal/file tree views.
- **`electron/`**: The desktop shell. Manages IPC, CDP browser integration, and bundles the Python backend via PyInstaller.
- **`plugins/`**: Dynamically loaded external tools, skills, and memory backends.
- **`docs/`**: Technical documentation (see `docs/architecture.md`).

## Quick Start

### Installation

Use `uv` (or `pip`) to install the core SDK and its dependencies:

```bash
uv pip install -e .
```

### Running the Application

**1. Run the Desktop API (Backend Server)**
```bash
PYTHONPATH=src uv run python -m ziva.app.cli desktop serve --host 127.0.0.1 --port 4097
```

**2. Run the Electron Desktop App**
```bash
cd electron
npm install
npm run dev
```

## Features
- **Unified Extension API**: Dynamically load `prompt`, `tool`, `skill`, `hook`, and `memory` from `manifest.yaml` definitions.
- **Desktop Browser Integration**: A seamless side-by-side Ziva agent + Chromium tabbed browser experience.
- **Robust Model Loop**: Multi-turn execution, context auto-compaction, and dynamic tool schemas.

## Configuration
Ziva's runtime is fully configurable via layered YAML files, allowing strict validation and merge capabilities across workspaces and global settings.
