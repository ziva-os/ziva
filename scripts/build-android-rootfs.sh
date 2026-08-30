#!/usr/bin/env bash
# Build the offline Ubuntu 24.04 arm64 rootfs for the Ziva Android APK.
#
# Must run on a native arm64 Linux host as root (CI: ubuntu-24.04-arm runner,
# which provisions the chroot natively — NEVER through qemu emulation).
# Output: android/app/src/main/assets/offline-rootfs.bin (tar.gz renamed —
# aapt silently untars .tar.gz asset entries, breaking the bundle).
#
# Usage: sudo bash scripts/build-android-rootfs.sh [repo_dir]
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK="$(mktemp -d /tmp/ziva-rootfs.XXXXXX)"
ROOTFS="$WORK/rootfs"
MIRROR="${MIRROR:-https://mirrors.tuna.tsinghua.edu.cn}"
# The base image ships no CA certificates, so the very first apt round must
# use plain HTTP; ca-certificates is installed in that round.
APT_MIRROR="${APT_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn}"
GH_MIRROR="${GH_MIRROR:-https://gh-proxy.com/https://github.com}"
BASE_TARBALL="${BASE_TARBALL:-ubuntu-base-24.04.4-base-arm64.tar.gz}"

echo "==> workspace: $WORK"
mkdir -p "$ROOTFS" "$REPO_DIR/android/app/src/main/assets"

# The distro proot (1.2.x on Ubuntu 24.04) is too old to be usable; use the
# official static build. This also keeps the script privilege-free — proot -R
# provides the chroot-like view without CAP_SYS_ADMIN, so it runs the same on
# a rootful CI runner and inside an unprivileged container.
echo "==> fetching static proot"
curl -fsSL -o "$WORK/proot" "$GH_MIRROR/proot-me/proot/releases/download/v5.3.0/proot-v5.3.0-aarch64-static"
chmod +x "$WORK/proot"
PROOT="$WORK/proot -R $ROOTFS"

echo "==> downloading Ubuntu base image"
curl -fsSL -o "$WORK/base.tar.gz" "$MIRROR/ubuntu-cdimage/ubuntu-base/releases/24.04/release/$BASE_TARBALL" \
  || curl -fsSL -o "$WORK/base.tar.gz" "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/$BASE_TARBALL"
tar -xzf "$WORK/base.tar.gz" -C "$ROOTFS"

echo "==> preparing chroot"
# Public v4 resolvers: the container-embedded DNS occasionally drops queries,
# and apt treats a transient NXDOMAIN/timeout as a hard fetch failure.
printf 'nameserver 223.5.5.5\nnameserver 114.114.114.114\noptions timeout:2 attempts:3\n' > "$ROOTFS/etc/resolv.conf"

$PROOT /bin/bash -eux <<CHROOT
# arm64 base images default to ports.ubuntu.com; swap every archive to the mirror.
sed -i "s|http://archive.ubuntu.com|${APT_MIRROR}|g; s|http://security.ubuntu.com|${APT_MIRROR}|g; s|http://ports.ubuntu.com|${APT_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null \
  || sed -i "s|http://archive.ubuntu.com|${APT_MIRROR}|g; s|http://security.ubuntu.com|${APT_MIRROR}|g; s|http://ports.ubuntu.com|${APT_MIRROR}|g" /etc/apt/sources.list
export DEBIAN_FRONTEND=noninteractive
# IPv6 is unreachable on some build hosts and apt's fetcher handles v6
# failure badly — pin IPv4 for the whole chroot session.
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4
# APT::Sandbox::User=root: under proot the _apt privilege drop wedges the fetcher.
APT="apt-get -o APT::Sandbox::User=root"
\$APT update -qq
\$APT install -y -qq --no-install-recommends \\
  python3 python3-venv python3-pip git ca-certificates curl
CHROOT

# Node 22 from the official arm64 tarball. Ubuntu 24.04 ships node 18 (too
# old for current MCP servers like chrome-devtools-mcp), and NodeSource's
# apt repo can't import its GPG key under proot — a plain tarball avoids
# both. Untarred into /usr/local so node/npm land on the guest PATH.
echo "==> installing node 22 (official linux-arm64 tarball)"
NODE_V=v22.14.0
curl -fsSL "https://nodejs.org/dist/${NODE_V}/node-${NODE_V}-linux-arm64.tar.xz" -o "$WORK/node.tar.xz"
mkdir -p "$ROOTFS/usr/local"
tar -xJf "$WORK/node.tar.xz" -C "$ROOTFS/usr/local" --strip-components=1

echo "==> provisioning ziva source + venv"
mkdir -p "$ROOTFS/opt/ziva-src"
# Ship exactly what the backend needs: the python package (which already
# contains the built web frontend under static/) plus the project metadata.
cp -r "$REPO_DIR/src" "$ROOTFS/opt/ziva-src/src"
cp "$REPO_DIR/pyproject.toml" "$ROOTFS/opt/ziva-src/"
cp "$REPO_DIR/README.md" "$ROOTFS/opt/ziva-src/" 2>/dev/null || true
# Core tools/hooks (list/read_file/shell/grep/...) live in the repo's
# plugins/ tree. Runtime.create's bundled fallback looks for
# <parents[2] of runtime.py>/plugins = /opt/ziva-src/plugins — without this
# the guest registers ZERO core tools and only MCP tools show up.
cp -r "$REPO_DIR/plugins" "$ROOTFS/opt/ziva-src/plugins"

# Pre-install chrome-devtools-mcp with the HOST npm: `npm install -g` under
# proot deterministically dies with glibc "double free or corruption" on the
# arm runner (host npm is fine). Copy the installed tree into the rootfs in
# npm's global layout; a relative bin shim puts it on the guest PATH.
echo "==> pre-installing chrome-devtools-mcp (host npm)"
mkdir -p "$WORK/mcp" "$ROOTFS/usr/local/lib/node_modules" "$ROOTFS/usr/local/bin"
npm install --prefix "$WORK/mcp" --no-audit --no-fund --loglevel=error chrome-devtools-mcp
cp -a "$WORK/mcp/node_modules/." "$ROOTFS/usr/local/lib/node_modules/"
BINREL=$(node -p "const p=require('$WORK/mcp/node_modules/chrome-devtools-mcp/package.json'); const b=p.bin&&(p.bin['chrome-devtools-mcp']||Object.values(p.bin)[0]); if(!b)process.exit(1); b")
chmod +x "$ROOTFS/usr/local/lib/node_modules/chrome-devtools-mcp/$BINREL"
ln -sf "../lib/node_modules/chrome-devtools-mcp/$BINREL" "$ROOTFS/usr/local/bin/chrome-devtools-mcp"

# Bake Playwright's linux-arm64 Chromium into the rootfs. Two kernels of
# trouble shaped this: (1) on-device `apt` under proot dies mid-transaction
# because Android's seccomp returns ENOSYS for syscalls noble's dpkg needs
# (statx et al. — the device's chromium-download.log shows the unpack
# aborting halfway), and (2) node under CI proot double-frees (same glibc
# issue as `npm install -g` above), which kills playwright's download
# child. So: download the browser with HOST node straight into the rootfs
# tree, and install the (stable, pinned-playwright-on-noble) dependency
# list inside the chroot. The chrome --version smoke at the end fails the
# build loudly if the list ever drifts.
echo "==> baking Playwright chromium (linux-arm64) into rootfs"
export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
export PLAYWRIGHT_BROWSERS_PATH="$ROOTFS/opt/ms-playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" "$ROOTFS/opt/chromium"
npx -y playwright@1.49.1 install chromium --no-shell
ln -sf "$(ls -d "$ROOTFS"/opt/ms-playwright/chromium-*/chrome-linux/chrome | head -n1)" "$ROOTFS/opt/chromium/chrome"
$PROOT /bin/bash -eux <<CHROOT
export DEBIAN_FRONTEND=noninteractive
# Persist the _apt sandbox bypass for any future in-guest apt (playwright's
# --with-deps used to need it; the base-image round passed it per-command).
echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99proot-root
APT="apt-get -o APT::Sandbox::User=root"
\$APT update -qq
\$APT install -y -qq --no-install-recommends \\
  fonts-freefont-ttf fonts-ipafont-gothic fonts-liberation \\
  fonts-noto-color-emoji fonts-tlwg-loma-otf fonts-unifont fonts-wqy-zenhei \\
  libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \\
  libavahi-client3 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libfontconfig1 \\
  libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \\
  libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \\
  x11-xkb-utils xfonts-cyrillic xfonts-encodings xfonts-scalable xvfb
apt-get clean
rm -rf /var/lib/apt/lists/*
/opt/chromium/chrome --version
CHROOT

$PROOT /bin/bash -eux <<CHROOT
python3 -m venv /opt/ziva-venv
/opt/ziva-venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
  'pyyaml>=6.0.1' 'openai>=1.30.0' 'mcp>=1.0.0,<2' 'anthropic>=0.30.0' \
  'httpx>=0.27.0' 'rich>=13.0.0' 'aiohttp>=3.9.0' 'uv>=0.5.0'
/opt/ziva-venv/bin/python -c 'import aiohttp, mcp, anthropic, openai, httpx, rich, yaml; print("rootfs deps OK")'
# uvx must be on the guest PATH — the backend spawns MCP servers with the
# env -i PATH from ProotBootstrap (no /opt/ziva-venv/bin in it), and user
# MCP configs routinely use `uvx ...` as the command (spawn uvx ENOENT).
ln -sf /opt/ziva-venv/bin/uv /usr/local/bin/uv
ln -sf /opt/ziva-venv/bin/uvx /usr/local/bin/uvx
command -v uvx && uvx --version
# Node runtime + pre-installed global MCP servers so npx-based servers work
# offline on device. chrome-devtools-mcp carries no Chrome binary (there is
# no linux-arm64 Chrome) — on device it must be pointed at a reachable
# browser via --browser-url (e.g. a LAN machine's --remote-debugging-port).
command -v node && node --version && chrome-devtools-mcp --version >/dev/null 2>&1 && echo "node+mcp OK"
# Smoke: the backend must at least import and start under the rootfs python.
# Mirror the device exactly: cwd=/root (NOT /opt/ziva-src — a repo-root cwd
# puts `plugins` on sys.path implicitly and hid the namespace-package import
# failure that killed the r26 backend on device), PYTHONPATH identical to
# ProotBootstrap's (src + repo root + venv site-packages).
cd /root && PYTHONPATH=/opt/ziva-src/src:/opt/ziva-src:/opt/ziva-venv/lib/python3.12/site-packages timeout 15 /usr/bin/python3 -m ziva.app.cli desktop serve --host 127.0.0.1 --port 4097 &
SRV=\$!
for i in \$(seq 1 25); do
  sleep 1
  if curl -sf -m 2 http://127.0.0.1:4097/status >/dev/null; then echo "smoke OK"; kill \$SRV 2>/dev/null; sleep 1; pkill -9 -f ziva.app.cli 2>/dev/null; sleep 1; exit 0; fi
done
echo "smoke FAILED"; kill \$SRV 2>/dev/null; pkill -9 -f ziva.app.cli 2>/dev/null; exit 1
CHROOT

# The smoke run leaves session state in the rootfs' /root/.ziva — drop it so
# the shipped bundle carries no test data (on-device, that path is a bind
# mount over the user's real data dir anyway).
rm -rf "$ROOTFS/root/.ziva" "$ROOTFS/root/workspace"

echo "==> packaging"
# --numeric-owner: the archive is extracted as the app's uid on Android.
tar -C "$ROOTFS" --numeric-owner -czf "$WORK/offline-rootfs.tar.gz" .
mv "$WORK/offline-rootfs.tar.gz" "$REPO_DIR/android/app/src/main/assets/offline-rootfs.bin"
echo "==> done: $(du -h "$REPO_DIR/android/app/src/main/assets/offline-rootfs.bin" | cut -f1) -> android/app/src/main/assets/offline-rootfs.bin"
