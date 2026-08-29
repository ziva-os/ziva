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
# Node 22 via NodeSource (Ubuntu 24.04 ships node 18, too old for current
# MCP servers like chrome-devtools-mcp which require node >= 20.19).
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/node22.sh
bash /tmp/node22.sh
\$APT install -y -qq nodejs
CHROOT

echo "==> provisioning ziva source + venv"
mkdir -p "$ROOTFS/opt/ziva-src"
# Ship exactly what the backend needs: the python package (which already
# contains the built web frontend under static/) plus the project metadata.
cp -r "$REPO_DIR/src" "$ROOTFS/opt/ziva-src/src"
cp "$REPO_DIR/pyproject.toml" "$ROOTFS/opt/ziva-src/"
cp "$REPO_DIR/README.md" "$ROOTFS/opt/ziva-src/" 2>/dev/null || true

$PROOT /bin/bash -eux <<CHROOT
python3 -m venv /opt/ziva-venv
/opt/ziva-venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
  'pyyaml>=6.0.1' 'openai>=1.30.0' 'mcp>=1.0.0' 'anthropic>=0.30.0' \
  'httpx>=0.27.0' 'rich>=13.0.0' 'aiohttp>=3.9.0'
/opt/ziva-venv/bin/python -c 'import aiohttp, mcp, anthropic, openai, httpx, rich, yaml; print("rootfs deps OK")'
# Node runtime + pre-installed global MCP servers so npx-based servers work
# offline on device. chrome-devtools-mcp carries no Chrome binary (there is
# no linux-arm64 Chrome) — on device it must be pointed at a reachable
# browser via --browser-url (e.g. a LAN machine's --remote-debugging-port).
npm config set update-notifier false
npm install -g --no-audit --no-fund chrome-devtools-mcp
command -v node && node --version && npx --yes chrome-devtools-mcp --help >/dev/null 2>&1 && echo "node+mcp OK"
# Smoke: the backend must at least import and start under the rootfs python.
cd /opt/ziva-src && PYTHONPATH=/opt/ziva-src/src timeout 15 /opt/ziva-venv/bin/python -m ziva.app.cli desktop serve --host 127.0.0.1 --port 4097 &
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
