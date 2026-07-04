# Ziva Code Architecture

Ziva has evolved from a simple backend runtime into a highly modular and extensible AI agent ecosystem. The codebase is organized into four distinct but interconnected pillars:

## 1. Ziva Python SDK (`src/ziva`)
The core reasoning and orchestration engine, built as a standalone, pip-installable Python package (`ziva`).
- **`runtime.py`**: The central dispatcher. Manages the Model-Tool-Model conversational loop, handles token compaction, applies context limits, and routes capability requests.
- **`adapters/`**: Bridges to language model APIs (e.g., Anthropic, OpenAI) and the Model Context Protocol (MCP) clients.
- **`capabilities/`**: Defines the `CapabilityRegistry` which handles the dynamic loading of skills, tools, memory backends, and permission policies.
- **`transports/`**: Communication interfaces for the runtime. Currently houses the `desktop_api` (HTTP/SSE server) which serves the local desktop client.

## 2. Web Frontend (`web/`)
A modern, component-driven UI built with Vite and TypeScript.
- **`browser-shell.ts`**: Implements the desktop "Browser Layout" where Ziva operates as a persistent, fixed leftmost tab alongside native web views.
- **`right-panel.ts`**: Renders the Xterm.js terminal view and the workspace file tree.
- **`sse.ts`**: Connects to the backend `desktop_api` Server-Sent Events stream for real-time, low-latency UI updates without aggressive polling.

## 3. Desktop Native App (`electron/`)
The native shell wrapping the Web UI and the Python Backend.
- **`main.ts`**: The Electron main process. It spawns the Python backend (bundled via PyInstaller), creates `WebContentsView` instances for the tabbed browser interface, and manages system tray/menu integrations.
- **`cdp-bridge.ts`**: Leverages the Chrome DevTools Protocol (CDP) to seamlessly proxy navigations, synchronize tab titles, and expose advanced browser automation capabilities to the Ziva agent.
- **`ziva-backend.spec`**: The PyInstaller spec file used to compile the `src/ziva` SDK into a standalone executable.

## 4. Plugins Ecosystem (`plugins/`)
A completely decoupled set of tools and skills loaded dynamically at runtime.
- Since the Ziva SDK only ships with the loader mechanism (`src/ziva/plugins/loader.py`), business logic tools (like `apply_patch`, `web_search`) reside here and are injected into the runtime via their `manifest.yaml` definitions.
