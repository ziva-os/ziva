package com.zivaos.ziva;

import android.content.Context;
import java.io.BufferedReader;
import java.io.File;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.ArrayDeque;
import java.util.List;
import java.util.Map;

/**
 * Business core: extract → verify → start/stop backend → health.
 * Kept deliberately process-focused; UI classes call into this singleton.
 */
public final class ZivaController {
    private static volatile ZivaController sInstance;
    private static final Object LOCK = new Object();

    private Process backendProc;
    /** Last ~40 backend log lines, shown on boot failure (the on-disk log is
     *  often unreachable without the All-files grant). */
    public final ArrayDeque<String> logTail = new ArrayDeque<>();
    public volatile String lastError = "";
    public volatile long startedAt = 0;

    public static ZivaController instance() {
        if (sInstance == null) {
            synchronized (LOCK) {
                if (sInstance == null) sInstance = new ZivaController();
            }
        }
        return sInstance;
    }

    private ZivaController() {}

    public boolean isExtracted(Context ctx) {
        return Constants.markerFile(ctx).exists()
                && new File(Constants.rootfsDir(ctx), "usr/bin/bash").exists();
    }

    /** Extract the offline rootfs bundle. Blocking — call from a worker thread. */
    public void extractOffline(Context ctx, TarGzipExtractor.Progress cb) throws Exception {
        if (isExtracted(ctx)) return;
        File rootfs = Constants.rootfsDir(ctx);
        // Fresh extraction over a half tree breaks startup — wipe first.
        if (rootfs.exists()) deleteTree(rootfs);
        try (InputStream in = TarGzipExtractor.openOfflineBundle(ctx)) {
            if (in == null) throw new IllegalStateException("APK 内未找到 offline-rootfs 包（本地构建请先运行 scripts/build-android-rootfs.sh 并放入 assets）");
            TarGzipExtractor.extractAuto(in, rootfs, cb);
        }
        Constants.markerFile(ctx).getParentFile().mkdirs();
        if (!Constants.markerFile(ctx).createNewFile())
            throw new IllegalStateException("无法写入解压完成标记");
    }

    /** Start the backend under proot. Idempotent. Blocking exec — worker thread. */
    public synchronized boolean startBackend(Context ctx) {
        if (backendProc != null && backendProc.isAlive()) return true;
        // A backend from a previous app lifetime that survived the kill and
        // still serves /status is adoptable — use it instead of re-spawning
        // (re-spawning would die on "address already in use").
        if (httpHealthy()) {
            startedAt = System.currentTimeMillis();
            lastError = "";
            return true;
        }
        // Otherwise the port may be held by a zombie backend from a previous
        // lifetime (holds 4097, never answers): clear it before binding.
        killStrayBackends();
        installGuestMirrors(ctx);
        try {
            List<String> cmd = ProotBootstrap.backendCommand(ctx);
            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            // Backend stdout/stderr must land DIRECTLY in a file, not in a
            // pipe drained by an app thread. When HyperOS kills the app but
            // the orphaned backend survives, a pipe with no reader fills
            // (64KB) and the next backend log line blocks the whole event
            // loop — /status stops answering, SSE drops, and the app's
            // watchdog keeps restarting it (the r17 "random interruption"
            // pattern: banners every few minutes, zero [tool] lines on
            // disk). Redirect.appendTo = O_APPEND via the kernel; the fd
            // stays valid with the app dead.
            File logTarget;
            try {
                File pub = logFile();
                if (pub.getParentFile() != null) pub.getParentFile().mkdirs();
                try (java.io.FileOutputStream t = new java.io.FileOutputStream(pub, true)) {
                    t.write('\n');
                }
                logTarget = pub;
            } catch (Exception noPublic) {
                logTarget = new File(ctx.getFilesDir(), "ziva-android.log");
            }
            pb.redirectOutput(ProcessBuilder.Redirect.appendTo(logTarget));
            final File logFileForTail = logTarget;
            // libproot.so needs libtalloc.so / libandroid-shmem.so from the
            // app's nativeLibraryDir — a forked child does NOT inherit the app
            // linker namespace, so point the classic linker at that dir too.
            Map<String, String> env = pb.environment();
            env.put("LD_LIBRARY_PATH",
                    Constants.nativeLibDir(ctx).getAbsolutePath()
                            + ":" + env.getOrDefault("LD_LIBRARY_PATH", ""));
            // PROOT_TMP_DIR / PROOT_L2S_DIR / PROOT_LOADER are read by proot
            // itself on the HOST side (from its own environ), NOT from the
            // guest's `env -i`. The Termux fork defaults PROOT_TMP_DIR to
            // /data/data/com.termux/... which doesn't exist in our app —
            // without this proot can't build its glue rootfs and dies before
            // exec'ing anything (verified on device, HyperOS).
            File prootTmp = new File(ctx.getFilesDir(), "linux/tmp");
            if (!prootTmp.exists()) prootTmp.mkdirs();
            env.put("PROOT_TMP_DIR", prootTmp.getAbsolutePath());
            env.put("PROOT_L2S_DIR", new File(ctx.getFilesDir(), "linux/l2s").getAbsolutePath());
            env.put("PROOT_LOADER", new File(Constants.nativeLibDir(ctx), "libloader.so").getAbsolutePath());
            backendProc = pb.start();
            startedAt = System.currentTimeMillis();
            lastError = "";
            // Pipe-drain path is gone (see redirect above); the pump thread
            // only existed to shuttle pipe bytes into the log file.
            // Surface early exits (linker refusals die within ~1s) in
            // lastError so boot-failure UI on device names the actual cause.
            Thread check = new Thread(() -> {
                try { Thread.sleep(2500); } catch (InterruptedException ignored) { return; }
                Process p = backendProc;
                if (p != null && !p.isAlive()) {
                    StringBuilder sb = new StringBuilder("后端进程提前退出 (code=" + p.exitValue() + ")");
                    String tail = readLastLines(logFileForTail, 40);
                    if (!tail.isEmpty()) sb.append("\n日志尾部:\n").append(tail);
                    lastError = sb.toString();
                }
            }, "ziva-exit-check");
            check.setDaemon(true);
            check.start();
            return true;
        } catch (Exception e) {
            lastError = "启动失败: " + e;
            return false;
        }
    }

    public synchronized void stopBackend() {
        Process p = backendProc;
        backendProc = null;
        if (p != null) {
            // destroy() signals the direct child (proot); its --kill-on-exit then
            // tears the whole guest process tree down with it. Note: we cannot
            // use java.lang.Process.pid() here — that API only exists from
            // Android 15 (API 35), we support down to 26.
            p.destroy();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()))) {
                while (r.readLine() != null) { /* drain */ }
            } catch (Exception ignored) {}
        }
        // destroy() only reaches proot; if it already died (app kill) the
        // guest python survives as an orphan. Make sure everything of ours
        // is gone before the next startBackend() binds.
        killStrayBackends();
    }

    /**
     * Kill leftover guest backends from a previous app lifetime. They run
     * under our uid, hold port 4097 but never answer /status (frozen by the
     * OEM), and every restart then dies on bind. The dots are escaped so the
     * pattern cannot match this very shell command line.
     */
    private static void killStrayBackends() {
        try {
            Process k = new ProcessBuilder("/system/bin/sh", "-c",
                    "pkill -9 -f 'ziva\\.app\\.cli'; pkill -9 -f 'libproot\\.so'; exit 0")
                    .redirectErrorStream(true).start();
            k.waitFor();
            Thread.sleep(200); // let the kernel close the held sockets
        } catch (Exception ignored) {}
    }

    public boolean isAlive() {
        return backendProc != null && backendProc.isAlive();
    }

    /** True when the HTTP surface answers; 2s budget keeps the watchdog cheap. */
    public boolean httpHealthy() {
        try {
            java.net.URL u = new java.net.URL("http://127.0.0.1:" + Constants.BACKEND_PORT + "/status");
            java.net.HttpURLConnection c = (java.net.HttpURLConnection) u.openConnection();
            c.setConnectTimeout(1500);
            c.setReadTimeout(1500);
            int code = c.getResponseCode();
            c.disconnect();
            return code == 200;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * Domestic PyPI/npm mirrors, written into the EXTRACTED rootfs at every
     * backend start (idempotent, tiny). Config files — not env vars — because
     * MCP servers spawned via uvx/npx go through the mcp SDK's
     * get_default_environment() whitelist, which strips anything we inject
     * into the guest environ. /etc/uv/uv.toml and /etc/pip.conf are
     * system-level (read regardless of HOME); npx reads $HOME/.npmrc and
     * HOME=/root IS on the SDK whitelist. python-preference=system keeps uvx
     * from downloading a python-build-standalone interpreter over a bare
     * github route — the rootfs system python3 is right there.
     *
     * This patches the extracted tree, NOT the rootfs bundle, so no
     * ROOTFS_VERSION bump / re-extraction is needed for it to take effect.
     */
    private static void installGuestMirrors(Context ctx) {
        File rootfs = Constants.rootfsDir(ctx);
        if (!rootfs.isDirectory()) return;
        writeGuestFile(new File(rootfs, "etc/uv/uv.toml"),
                "python-preference = \"system\"\n"
                + "\n[[index]]\n"
                + "url = \"https://pypi.tuna.tsinghua.edu.cn/simple\"\n"
                + "default = true\n");
        writeGuestFile(new File(rootfs, "etc/pip.conf"),
                "[global]\nindex-url = https://pypi.tuna.tsinghua.edu.cn/simple\n");
        writeGuestFile(new File(rootfs, "root/.npmrc"),
                "registry=https://registry.npmmirror.com\n");
    }

    private static void writeGuestFile(File f, String content) {
        try {
            if (f.getParentFile() != null) f.getParentFile().mkdirs();
            try (java.io.FileOutputStream o = new java.io.FileOutputStream(f)) {
                o.write(content.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
            // A read-only rootfs costs the user mirror speed, not function.
        }
    }

    /** Last n lines of the backend log file, for boot-failure surfacing. */
    private static String readLastLines(File f, int n) {
        try {
            java.util.Deque<String> d = new java.util.ArrayDeque<>();
            try (BufferedReader r = new BufferedReader(new java.io.FileReader(f))) {
                String line;
                while ((line = r.readLine()) != null) {
                    d.addLast(line);
                    while (d.size() > n) d.removeFirst();
                }
            }
            return String.join("\n", d);
        } catch (Exception e) {
            return "";
        }
    }

    /** Public log path (needs "All files access"); shown in Diagnostics. */
    public static File logFile() {
        return new File("/sdcard/Documents/zivadata/ziva-android.log");
    }

    /** Append a line (webview events/console) to the same on-disk log + tail. */
    public void appendLog(Context ctx, String line) {
        synchronized (logTail) {
            logTail.addLast(line);
            while (logTail.size() > 40) logTail.removeFirst();
        }
        try {
            File log = logFile();
            if (log.getParentFile() != null) log.getParentFile().mkdirs();
        } catch (Exception ignored) {}
        try (java.io.BufferedWriter bw = new java.io.BufferedWriter(
                new java.io.FileWriter(logFile(), true))) {
            bw.write(line); bw.newLine();
        } catch (Exception ignored) {
            try (java.io.BufferedWriter bw = new java.io.BufferedWriter(
                    new java.io.FileWriter(new File(ctx.getFilesDir(), "ziva-android.log"), true))) {
                bw.write(line); bw.newLine();
            } catch (Exception ignored2) {}
        }
    }

    private static void deleteTree(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteTree(k);
        f.delete();
    }
}
