#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Building Ziva Desktop App ==="
echo "Project root: $PROJECT_ROOT"
echo ""

# 1. Build frontend
echo ">>> Building frontend..."
cd "$PROJECT_ROOT/web"
if [ ! -d "node_modules" ]; then
  npm install
fi
npm run build

# 2. Copy frontend assets to Python static dir (for PyInstaller)
echo ">>> Copying frontend assets..."
STATIC_DIR="$PROJECT_ROOT/src/ziva_runtime/transports/desktop_api/static"
rm -rf "$STATIC_DIR/assets"
mkdir -p "$STATIC_DIR/assets"
cp -r "$PROJECT_ROOT/web/dist/"* "$STATIC_DIR/"

# 3. Build Python backend with PyInstaller
echo ">>> Building Python backend..."
cd "$PROJECT_ROOT/electron"
if ! command -v pyinstaller &> /dev/null; then
  echo "Installing pyinstaller..."
  pip install pyinstaller
fi
pyinstaller ziva-backend.spec --clean --noconfirm

# 4. Build Electron app
echo ">>> Building Electron app..."
if [ ! -d "node_modules" ]; then
  npm install
fi
npm run build

echo ""
echo "=== Build complete ==="
echo ""
echo "To test locally:  cd electron && npm run start"
echo "To package:       cd electron && npm run pack"
echo "To distribute:    cd electron && npm run dist"
