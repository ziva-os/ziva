#!/usr/bin/env bash
# Fetch the Termux proot fork (the only build with --link2symlink) and its
# shared-library dependencies, and lay them out as Android jniLibs:
#
#   libproot.so          <- proot_<ver>_aarch64.deb  usr/bin/proot
#   libloader.so         <- same deb                 usr/libexec/proot/loader
#   libtalloc.so         <- libtalloc_<ver>_aarch64.deb  usr/lib/libtalloc.so.2.4.3
#   libandroid-shmem.so  <- libandroid-shmem_<ver>_aarch64.deb
#
# proot's DT_NEEDED "libtalloc.so.2" is patched in place to "libtalloc.so"
# (same-length NUL padding) so the Android linker resolves it from jniLibs.
#
# Usage: bash scripts/fetch-proot.sh [output_dir]
set -euo pipefail

OUT="${1:-$(cd "$(dirname "$0")/.." && pwd)/android/app/src/main/jniLibs/arm64-v8a}"
POOL="https://packages.termux.dev/apt/termux-main/pool/main"
TMP="$(mktemp -d /tmp/ziva-proot.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

latest_deb() { # $1 = pool subdir; prints filename of the newest aarch64 deb
  curl -fsSL "$POOL/$1/" | grep -o 'href="[^"]*_aarch64\.deb"' \
    | sed 's/href="//; s/"$//' | sort -V | tail -1
}

for pair in "p/proot proot" "libt/libtalloc talloc" "liba/libandroid-shmem shmem"; do
  set -- $pair
  name="$(latest_deb "$1")"
  echo "==> $name"
  curl -fsSL -o "$TMP/$2.deb" "$POOL/$1/$name"
done

python3 - "$TMP" "$OUT" <<'PY'
import os, sys, tarfile

tmp, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

def unpack(deb, dest):
    """Extract data.tar.xz from a .deb without dpkg-deb (portable ar reader)."""
    data = open(deb, 'rb').read()
    off = 8  # skip "!<arch>\n"
    while off < len(data):
        hdr = data[off:off+60]
        name = hdr[0:16].decode().strip().rstrip('/')
        size = int(hdr[48:58].decode().strip())
        if name == 'data.tar.xz':
            txz = os.path.join(tmp, 'data.tar.xz')
            open(txz, 'wb').write(data[off+60:off+60+size])
            break
        off += 60 + size + (size % 2)
    else:
        raise SystemExit(f"no data.tar.xz in {deb}")
    os.makedirs(dest, exist_ok=True)
    tarfile.open(txz).extractall(dest)

def first(root, name):
    for r, _, files in os.walk(root):
        if name in files:
            return os.path.join(r, name)
    raise SystemExit(f"{name} not found under {root}")

d = {k: os.path.join(tmp, k) for k in ('proot', 'talloc', 'shmem')}
for key in d:
    unpack(os.path.join(tmp, key + '.deb'), d[key])

def emit(src, dst_name, patch=None):
    data = open(src, 'rb').read()
    if patch:
        old, new = patch
        if old not in data:
            raise SystemExit(f"expected {old!r} not found in {src}")
        assert len(old) == len(new)
        data = data.replace(old, new)
    dst = os.path.join(out, dst_name)
    open(dst, 'wb').write(data)
    os.chmod(dst, 0o755)
    print(f"    -> {dst_name} ({len(data)} bytes)")

# proot main binary; retarget libtalloc.so.2 -> libtalloc.so (NUL padded, same length)
emit(first(d['proot'], 'proot'), 'libproot.so',
     patch=(b'libtalloc.so.2\x00', b'libtalloc.so\x00\x00\x00'))
emit(first(d['proot'], 'loader'), 'libloader.so')
emit(first(d['talloc'], 'libtalloc.so.2.4.3'), 'libtalloc.so')
emit(first(d['shmem'], 'libandroid-shmem.so'), 'libandroid-shmem.so')
print(f"==> jniLibs written to {out}")
PY

ls -la "$OUT"
