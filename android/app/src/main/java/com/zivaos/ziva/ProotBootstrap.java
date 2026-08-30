package com.zivaos.ziva;

import android.content.Context;
import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * Builds and execs the proot command line that chroots into the Ubuntu rootfs
 * and runs the Ziva backend.
 *
 * The proot binary is the Termux fork (packaged as jniLibs/arm64-v8a/libproot.so
 * plus libloader.so) — it is the only build that carries --link2symlink, which
 * emulates link(2) as symlink→.l2s. file because SELinux forbids hard links in
 * app-private storage. Do not replace it with a stock proot build.
 */
public final class ProotBootstrap {

    /** Full argv for starting the backend. Caller spawns this via ProcessBuilder. */
    public static List<String> backendCommand(Context ctx) {
        File rootfs = Constants.rootfsDir(ctx);
        File proot = new File(Constants.nativeLibDir(ctx), "libproot.so");
        File dataDir = ensure(Constants.publicDataDir());
        File workspace = ensure(Constants.workspaceDir());
        File appPrivateData = ensure(new File(ctx.getFilesDir(), "ziva-data"));
        // proot's l2s dir must be an app-writable host path — the device root
        // ("/.l2s") is not. Created here; the env var is set host-side in
        // ZivaController.startBackend.
        ensure(new File(ctx.getFilesDir(), "linux/l2s"));

        List<String> cmd = new ArrayList<>();
        cmd.add(proot.getAbsolutePath());
        cmd.add("--kill-on-exit");
        cmd.add("-0");
        cmd.add("--link2symlink");
        cmd.add("-r"); cmd.add(rootfs.getAbsolutePath());
        cmd.add("-b"); cmd.add("/dev");
        cmd.add("-b"); cmd.add("/proc");
        cmd.add("-b"); cmd.add("/sys");
        // Hot data lives in the public dir (survives uninstall); when the
        // "All files access" grant is missing we bind an app-private dir and
        // Diagnostics tells the user what they are missing.
        cmd.add("-b"); cmd.add((dataDirCanWrite(dataDir) ? dataDir : appPrivateData).getAbsolutePath() + ":" + Constants.GUEST_DATA_DIR);
        cmd.add("-b"); cmd.add(workspace.getAbsolutePath() + ":" + Constants.GUEST_WORKSPACE);
        cmd.add("-w"); cmd.add(Constants.GUEST_WORKSPACE);
        cmd.add("/usr/bin/env");
        cmd.add("-i");
        cmd.add("HOME=/root");
        cmd.add("USER=root");
        cmd.add("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
        cmd.add("TERM=xterm-256color");
        cmd.add("LANG=C.UTF-8");
        cmd.add("PYTHONUNBUFFERED=1");
        // Run the SYSTEM python3 with PYTHONPATH pointing at both the ziva
        // sources and the venv's site-packages. This deliberately avoids
        // venv/bin/python, which is a chain of symlinks (python -> python3 ->
        // python3.12) that may not survive extraction on every device; the
        // interpreter itself and site-packages are plain files.
        cmd.add("PYTHONPATH=" + Constants.GUEST_ZIVA_SRC + "/src:" + Constants.GUEST_VENV_SITE_PACKAGES);
        // PROOT_TMP_DIR / PROOT_L2S_DIR / PROOT_LOADER are injected by
        // ZivaController into proot's HOST environment — see startBackend.
        cmd.add("/bin/bash");
        cmd.add("-c");
        // Two prep steps before serve, both best-effort but SEQUENTIAL:
        //  1. patch-mcp-config.py rewrites a legacy chrome-devtools entry
        //     (--browser-url to the Mac's 9222) into the on-device wrapper;
        //     its output goes to the backend log so patching is observable.
        //  2. ensure-chromium --download-only fetches the arm64 Chromium
        //     SYNCHRONOUSLY — deliberately not backgrounded. A backgrounded
        //     download let users into the chat while the box was still
        //     saturating IO, and the first browser tools then stalled mid-
        //     conversation (worse UX than an honest "preparing" phase up
        //     front). waitForBackend shows download progress while this
        //     runs; on download failure serve still starts (the MCP layer
        //     re-triggers the download lazily).
        cmd.add("/usr/bin/python3 /opt/patch-mcp-config.py 2>&1; "
                + "/bin/sh /opt/ensure-chromium.sh --download-only 2>&1; "
                + "exec /usr/bin/python3 -m ziva.app.cli desktop serve"
                + " --host 127.0.0.1 --port " + Constants.BACKEND_PORT
                + " --workspace " + Constants.GUEST_WORKSPACE);
        return cmd;
    }

    /**
     * Real write probe. File.canWrite() misreports on some OEM FUSE layers
     * (HyperOS: mkdirs succeeds, canWrite() is true, then the actual write
     * throws AccessDenied) — and binding such a directory into the guest as
     * /root/.ziva kills the backend on its first write. So: create a temp
     * file, write, delete.
     */
    static boolean dataDirCanWrite(File d) {
        File probe = new File(d, ".write-probe");
        try {
            if (!d.exists() && !d.mkdirs()) return false;
            try (java.io.FileOutputStream fos = new java.io.FileOutputStream(probe)) {
                fos.write(0x2a);
            }
            probe.delete();
            return true;
        } catch (Exception e) {
            probe.delete();
            return false;
        }
    }

    private static File ensure(File d) {
        if (!d.exists()) d.mkdirs();
        return d;
    }

    /** Quick sanity probe of the two native binaries and the rootfs layout. */
    public static List<String> probe(Context ctx) {
        List<String> issues = new ArrayList<>();
        File proot = new File(Constants.nativeLibDir(ctx), "libproot.so");
        File loader = new File(Constants.nativeLibDir(ctx), "libloader.so");
        File rootfs = Constants.rootfsDir(ctx);
        if (!proot.exists() || !proot.canExecute()) issues.add("proot 二进制缺失或不可执行: " + proot);
        if (!loader.exists()) issues.add("proot loader 缺失: " + loader);
        // Check the real files, not /bin/bash — /bin is a symlink into usr/bin
        // (as is /lib, /sbin on merged-usr Ubuntu), so this also implicitly
        // verifies that symlink extraction worked.
        if (!new File(rootfs, "usr/bin/bash").exists()) issues.add("rootfs 不完整（缺 usr/bin/bash）: " + rootfs);
        if (!new File(rootfs, "usr/bin/python3.12").exists())
            issues.add("rootfs 缺系统 Python: usr/bin/python3.12");
        File site = new File(rootfs, Constants.GUEST_VENV_SITE_PACKAGES.substring(1));
        if (!site.isDirectory()) issues.add("rootfs 缺 venv 依赖目录: " + Constants.GUEST_VENV_SITE_PACKAGES);
        if (!new File(rootfs, "opt/ziva-src/src/ziva/app/cli.py").exists())
            issues.add("rootfs 缺 Ziva 源码: opt/ziva-src/src/ziva/app/cli.py");
        return issues;
    }
}
