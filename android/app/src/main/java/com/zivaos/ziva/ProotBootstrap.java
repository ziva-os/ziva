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
        File loader = new File(Constants.nativeLibDir(ctx), "libloader.so");
        File proot = new File(Constants.nativeLibDir(ctx), "libproot.so");
        File dataDir = ensure(Constants.publicDataDir());
        File workspace = ensure(Constants.workspaceDir());
        File appPrivateData = ensure(new File(ctx.getFilesDir(), "ziva-data"));

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
        cmd.add("PROOT_L2S_DIR=/.l2s");
        cmd.add("PROOT_LOADER=" + loader.getAbsolutePath());
        cmd.add("/bin/bash");
        cmd.add("-c");
        cmd.add("exec " + Constants.GUEST_VENV_PY + " -m ziva.app.cli desktop serve"
                + " --host 127.0.0.1 --port " + Constants.BACKEND_PORT
                + " --workspace " + Constants.GUEST_WORKSPACE);
        return cmd;
    }

    static boolean dataDirCanWrite(File d) {
        if (!d.exists() && !d.mkdirs()) return false;
        return d.canWrite();
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
        if (!new File(rootfs, "bin/bash").exists()) issues.add("rootfs 不完整（缺 bin/bash）: " + rootfs);
        if (!new File(rootfs, Constants.GUEST_VENV_PY.substring(1)).exists())
            issues.add("rootfs 缺 Python venv: " + Constants.GUEST_VENV_PY);
        return issues;
    }
}
