# 前置依赖与安装

本页详细介绍使用 Ziva 所需的全部前置组件，按重要程度排序。

---

## 一、系统要求

| 项目 | 最低版本 | 备注 |
|------|---------|------|
| macOS | 13.0+ (Ventura) | Apple Silicon (M1/M2/M3/M4) 完整支持；Intel Mac 可用但语音输入等部分功能需自行调整 |
| Python | 3.10+ | 桌面打包要求 **3.11**（PyInstaller spec 硬依赖） |
| Node.js | 18+ | 推荐 20+ |
| Xcode CLI Tools | 最新 | `xcode-select --install` |

---

## 二、必装组件

### 1. Python 与 uv（包管理）

```bash
# 安装 Python 3.11（推荐用 pyenv 或 Homebrew）
brew install python@3.11

# 安装 uv（比 pip 快 10-100 倍，强烈推荐）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Node.js 与 npm

```bash
# 推荐用 nvm 管理
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
nvm install 20
nvm use 20
```

### 3. Google Chrome（浏览器自动化必备）

Ziva 的内置浏览器自动化依赖 Chrome DevTools Protocol (CDP)。

```bash
brew install --cask google-chrome
```

安装后确认版本 ≥ 120：
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --version
```

### 4. npx（随 Node.js 安装）

用于运行 chrome-devtools-mcp。验证：
```bash
npx --version
```

---

## 三、必装 MCP 服务

### Chrome DevTools MCP（强烈推荐）

Ziva 内置浏览器的核心能力来源。允许 AI 控制 Chrome 进行页面导航、点击、截图、网络分析等操作。

**安装方式**：无需单独安装，Ziva 配置中已默认启用。它通过 `npx -y chrome-devtools-mcp` 自动运行。

**配置确认**（`~/.ziva/config.yaml`）：
```yaml
mcp:
  enabled: true
  servers:
    chrome-devtools:
      type: local
      command: npx -y chrome-devtools-mcp --browserUrl http://127.0.0.1:9222
      environment: {}
      enabled: true
```

**工作原理**：Ziva 启动时会自动以远程调试模式启动 Chrome（端口 9222），chrome-devtools-mcp 连接到该实例。

> ⚠️ 如果你系统上已有 Chrome 在运行，需要先关闭，否则调试端口可能无法绑定。

---

## 四、推荐安装（增强体验）

### 1. uvx（MCP 工具运行器）

随 uv 一起安装。用于运行 MiniMax MCP 等基于 Python 的 MCP 服务。

```bash
# 验证
uvx --version
```

### 2. mlx-whisper（语音输入，仅 Apple Silicon）

Ziva 支持本地语音转文字，首次使用自动下载模型（~461 MB）。

```bash
pip install mlx-whisper
```

配置中已默认启用：
```yaml
stt:
  model: mlx-community/whisper-small-mlx
```

### 3. Git（代码操作）

macOS 自带，确认版本：
```bash
git --version
```

### 4. ripgrep（高速搜索）

Ziva 的 `grep` 工具底层使用 ripgrep，比传统 grep 快 10-100 倍。

```bash
brew install ripgrep
```

---

## 五、完整安装顺序（从零开始）

```bash
# 0. 安装 Xcode CLI Tools
xcode-select --install

# 1. 安装 Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 安装核心依赖
brew install python@3.11 node ripgrep git
brew install --cask google-chrome

# 3. 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 4. 克隆并安装 Ziva
git clone https://github.com/ziva-os/ziva.git
cd ziva
uv pip install -e ".[all]"

# 5. 安装前端
cd web && npm install && cd ..

# 6. 安装 Electron
cd electron && npm install && cd ..

# 7. 启动
cd electron && npm start
```

---

## 六、环境变量（可选）

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `ZIVA_CDP_PORT` | Chrome 远程调试端口 | 9222 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 系统代理 | 无 |
| `ELECTRON_MIRROR` | Electron 下载镜像（国内推荐） | 无 |

国内用户可在安装 Electron 依赖时设置镜像：
```bash
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
```
