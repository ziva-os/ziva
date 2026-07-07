#!/bin/bash
# 打包 Ziva macOS 桌面客户端（前端 + PyInstaller 后端 + Electron dmg/zip）
# 用法: ./scripts/build-desktop.sh [--rebuild-venv]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="$PROJECT_ROOT/web"
ELECTRON_DIR="$PROJECT_ROOT/electron"

REBUILD_VENV=0
for arg in "$@"; do
  if [ "$arg" = "--rebuild-venv" ]; then
    REBUILD_VENV=1
  fi
done

# 国内镜像: 避免从 github 下载 Electron 二进制时 TLS 断
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/

# 设置 chrome-devtools-mcp 默认的 CDP 端口
export ZIVA_CDP_PORT=9222

# 用专用 venv 打包，隔离全局环境——避免全局 miniconda 里的 transformers /
# modelscope 等 ML 包被 PyInstaller 扫进来（它们的 hook 还会卡 tokenizers
# 版本）。venv 只装 ziva 的 pyproject 依赖 + pyinstaller。
VENV="$PROJECT_ROOT/.build-venv"

# 优先用 Python 3.11，因为 electron/ziva-backend.spec 里硬编码了
# .build-venv/lib/python3.11/site-packages 路径去复制 mlx 资源。
PYTHON_BIN=""
for py in python3.11 python3.11 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    ver=$("$py" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    if [ "$ver" = "3.11" ]; then
      PYTHON_BIN="$py"
      break
    fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "错误: 找不到 Python 3.11。PyInstaller spec 硬依赖 3.11 路径。"
  echo "请安装 Python 3.11 后再试。"
  exit 1
fi
echo "=== 使用 Python: $PYTHON_BIN ($($PYTHON_BIN -c 'import sys; print(sys.executable)')) ==="

if [ "$REBUILD_VENV" -eq 1 ] && [ -d "$VENV" ]; then
  echo "=== 删除旧 venv (按 --rebuild-venv 请求) ==="
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "=== 0/4 创建专用打包 venv (.build-venv) — 首次较慢 ==="
  "$PYTHON_BIN" -m venv "$VENV"
  PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"
  "$VENV/bin/pip" install --upgrade pip $PIP_MIRROR
  "$VENV/bin/pip" install -e "$PROJECT_ROOT" $PIP_MIRROR   # ziva + pyproject 依赖(含传递)
  "$VENV/bin/pip" install pyinstaller $PIP_MIRROR
fi
PYI="$VENV/bin/pyinstaller"

# 清理旧的 PyInstaller / electron-builder 产物，避免旧插件或旧二进制混入新包
#（electron-builder 会自动覆盖同名文件，但 dmg/zip 不会，容易让人混淆）。
echo "=== 清理旧产物 ==="
rm -rf "$ELECTRON_DIR/dist/mac-arm64" \
       "$ELECTRON_DIR/dist/Ziva-1.0.0-arm64.dmg" \
       "$ELECTRON_DIR/dist/Ziva-1.0.0-arm64-mac.zip" \
       "$ELECTRON_DIR/dist/Ziva-1.0.0-arm64.dmg.blockmap" \
       "$ELECTRON_DIR/dist/Ziva-1.0.0-arm64-mac.zip.blockmap" \
       "$ELECTRON_DIR/dist/builder-effective-config.yaml" \
       "$ELECTRON_DIR/dist/builder-debug.yml" 2>/dev/null || true

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
