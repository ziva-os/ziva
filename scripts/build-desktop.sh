#!/bin/bash
# 打包 Ziva macOS 桌面客户端（前端 + PyInstaller 后端 + Electron dmg/zip）
# 用法: ./scripts/build-mac.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="$PROJECT_ROOT/web"
ELECTRON_DIR="$PROJECT_ROOT/electron"

# 国内镜像: 避免从 github 下载 Electron 二进制时 TLS 断
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/

# 设置 chrome-devtools-mcp 默认的 CDP 端口
export ZIVA_CDP_PORT=9222

# 用专用 venv 打包，隔离全局环境——避免全局 miniconda 里的 transformers /
# modelscope 等 ML 包被 PyInstaller 扫进来（它们的 hook 还会卡 tokenizers
# 版本）。venv 只装 ziva 的 pyproject 依赖 + pyinstaller。
VENV="$PROJECT_ROOT/.build-venv"
if [ ! -d "$VENV" ]; then
  echo "=== 0/4 创建专用打包 venv (.build-venv) — 首次较慢 ==="
  python3 -m venv "$VENV"
  PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
  "$VENV/bin/pip" install --upgrade pip $PIP_MIRROR
  "$VENV/bin/pip" install -e "$PROJECT_ROOT" $PIP_MIRROR   # ziva + pyproject 依赖(含传递)
  "$VENV/bin/pip" install pyinstaller $PIP_MIRROR
fi
PYI="$VENV/bin/pyinstaller"

echo ""
echo "=== 1/4 前端 build (web → src/.../static) ==="
cd "$WEB_DIR"
[ -d node_modules ] || npm install
npm run build

echo ""
echo "=== 2/4 PyInstaller 打 Python 后端 (ziva-backend) — 专用 venv ==="
cd "$ELECTRON_DIR"
[ -d node_modules ] || npm install
"$PYI" ziva-backend.spec --clean --noconfirm

echo ""
echo "=== 3/4 Electron tsc (main/preload/cdp-bridge) ==="
npm run build

echo ""
echo "=== 4/4 electron-builder 打 dmg + zip ==="
npx electron-builder

echo ""
echo "=== 完成 ✅ ==="
echo "dmg:   $ELECTRON_DIR/dist/Ziva-1.0.0-arm64.dmg"
echo "zip:   $ELECTRON_DIR/dist/Ziva-1.0.0-arm64-mac.zip"
echo "app:   $ELECTRON_DIR/dist/mac-arm64/Ziva.app"
echo ""
echo "未签名 — 首次打开: 右键 Ziva.app → 打开"
echo "     或: xattr -dr com.apple.quarantine /Applications/Ziva.app"
