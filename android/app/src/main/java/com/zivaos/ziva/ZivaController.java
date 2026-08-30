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
            // Death note: log WHY the backend went away. 137 = SIGKILL
            // (system/OOM kill — nothing in our code path sends KILL to a
            // live backend any more), 143 = SIGTERM (our stopBackend /
            // service teardown), 0 = clean exit. Without this the log only
            // shows the restart banner and every kill looks identical.
            final Process proc = backendProc;
            final File procLog = logFileForTail;
            Thread reaper = new Thread(() -> {
                try {
                    int code = proc.waitFor();
                    String hint = code == 137 ? " (SIGKILL — system/OOM)"
                            : code == 143 ? " (SIGTERM — our stopBackend)"
                            : code == 0 ? " (clean exit)" : "";
                    appendProcLog(procLog, "[proc] backend exited code=" + code + hint);
                } catch (InterruptedException ignored) {
                }
            }, "ziva-reaper");
            reaper.setDaemon(true);
            reaper.start();
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
        installChromiumHelper(rootfs);
        installMcpConfigPatcher(rootfs);
    }

    /**
     * Writes a tiny idempotent patcher the backend runs before serve: it
     * rewrites a legacy chrome-devtools MCP entry (npx … --browser-url …,
     * pointing at a Mac's 9222 that no longer exists) into the on-device
     * /opt/ensure-chromium.sh wrapper — zero manual config editing on the
     * tablet. Runs guest-side so yaml parsing happens with the venv's own
     * pyyaml; config.yaml lives in the bind-mounted guest data dir.
     */
    private static void installMcpConfigPatcher(File rootfs) {
        writeGuestFile(new File(rootfs, "opt/patch-mcp-config.py"),
            "#!/usr/bin/python3\n"
            + "import sys\n"
            + "import yaml\n"
            + "\n"
            + "P = \"/root/.ziva/config.yaml\"\n"
            + "try:\n"
            + "    with open(P) as f:\n"
            + "        cfg = yaml.safe_load(f) or {}\n"
            + "except Exception:\n"
            + "    sys.exit(0)\n"
            + "changed = False\n"
            + "servers = ((cfg.get(\"mcp\") or {}).get(\"servers\")) or []\n"
            + "for srv in servers:\n"
            + "    if not isinstance(srv, dict):\n"
            + "        continue\n"
            + "    args = [str(a) for a in (srv.get(\"args\") or [])]\n"
            + "    joined = \" \".join(args)\n"
            + "    name = str(srv.get(\"name\", \"\"))\n"
            + "    if \"chrome-devtools\" not in joined and \"chrome\" not in name.lower():\n"
            + "        continue\n"
            + "    if \"chrome-devtools-mcp\" not in joined:\n"
            + "        continue  # not the bundled server; leave custom setups alone\n"
            + "    if srv.get(\"command\") == \"/bin/sh\" and \"/opt/ensure-chromium.sh\" in args:\n"
            + "        continue  # already patched\n"
            + "    srv[\"command\"] = \"/bin/sh\"\n"
            + "    srv[\"args\"] = [\"/opt/ensure-chromium.sh\"]\n"
            + "    srv.pop(\"env\", None)\n"
            + "    changed = True\n"
            + "if changed:\n"
            + "    try:\n"
            + "        with open(P, \"w\") as f:\n"
            + "            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)\n"
            + "        print(\"[patch] chrome-devtools MCP server switched to on-device chromium\")\n"
            + "    except Exception as e:\n"
            + "        print(\"[patch] rewrite failed:\", e)\n");
    }

    /**
     * The user's MCP config points chrome-devtools at /opt/ensure-chromium.sh.
     * First connects fail fast (exit 1) while the script downloads Playwright's
     * linux-arm64 Chromium in the background via the npmmirror playwright
     * mirror — chrome-for-testing (puppeteer's default) simply has no arm64
     * build, which is the only reason this browser ever needed the Mac's 9222.
     * A stale-lock + timestamp guard keeps parallel MCP connect retries from
     * forking dueling downloads.
     */
    private static void installChromiumHelper(File rootfs) {
        writeGuestFile(new File(rootfs, "opt/ensure-chromium.sh"),
            "#!/bin/sh\n"
            + "CHROME=/opt/chromium/chrome\n"
            + "LOCK=/opt/chromium/.downloading\n"
            + "TAG=\"[ensure]\"\n"
            + "mkdir -p /opt/chromium /root/.cache/ms-playwright\n"
            + "\n"
            + "download() {\n"
            + "  export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright\n"
            + "  export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright\n"
            + "  export DEBIAN_FRONTEND=noninteractive\n"
            + "  npx -y playwright@1.49.1 install --with-deps chromium --no-shell\n"
            + "  SRC=$(ls -d /root/.cache/ms-playwright/chromium-*/chrome-linux 2>/dev/null | head -n 1)\n"
            + "  if [ -n \"$SRC\" ] && [ -x \"$SRC/chrome\" ]; then\n"
            + "    ln -sf \"$SRC/chrome\" \"$CHROME\"\n"
            + "    echo \"$TAG chromium installed at $CHROME\"\n"
            + "  else\n"
            + "    echo \"$TAG chromium download FAILED (see npx output above)\"\n"
            + "  fi\n"
            + "  rm -f \"$LOCK\"\n"
            + "}\n"
            + "\n"
            + "fresh_or_running() {\n"
            + "  if [ -f \"$LOCK\" ]; then\n"
            + "    pid=$(cut -d' ' -f1 \"$LOCK\" 2>/dev/null)\n"
            + "    if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then return 0; fi\n"
            + "    started=$(cut -d' ' -f2 \"$LOCK\" 2>/dev/null)\n"
            + "    now=$(date +%s)\n"
            + "    if [ -n \"$started\" ] && [ $((now - started)) -lt 600 ]; then return 0; fi\n"
            + "    rm -f \"$LOCK\"\n"
            + "  fi\n"
            + "  return 1\n"
            + "}\n"
            + "\n"
            + "if [ \"$1\" = \"--download-only\" ]; then\n"
            + "  if [ -x \"$CHROME\" ]; then echo \"$TAG chromium already present\"; exit 0; fi\n"
            + "  if fresh_or_running; then echo \"$TAG download already running/fresh\"; exit 0; fi\n"
            + "  echo \"$$ $(date +%s)\" > \"$LOCK\"\n"
            + "  echo \"$TAG downloading linux-arm64 chromium (~250MB, domestic mirror)...\"\n"
            + "  download\n"
            + "  exit 0\n"
            + "fi\n"
            + "\n"
            + "if [ ! -x \"$CHROME\" ]; then\n"
            + "  if fresh_or_running; then exit 1; fi\n"
            + "  echo \"$$ $(date +%s)\" > \"$LOCK\"\n"
            + "  download >/opt/chromium/download.log 2>&1 &\n"
            + "  exit 1\n"
            + "fi\n"
            + "exec /usr/local/bin/chrome-devtools-mcp --executablePath \"$CHROME\" --headless \"$@\"\n");
        // Best effort: the helper needs +x for direct exec via /bin/sh anyway,
        // but a shebang'd exec keeps the config one-liner clean.
        try {
            Runtime.getRuntime().exec(new String[]{"chmod", "755",
                    new File(rootfs, "opt/ensure-chromium.sh").getAbsolutePath()});
        } catch (Exception ignored) {}
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

    /** Timestamped one-liner into the backend log — survives across app and
     *  backend lifetimes so a kill can be correlated with the banners. */
    private static void appendProcLog(File logFile, String line) {
        try {
            if (logFile.getParentFile() != null) logFile.getParentFile().mkdirs();
            String ts = new java.text.SimpleDateFormat("MM-dd HH:mm:ss")
                    .format(new java.util.Date());
            try (java.io.FileOutputStream o = new java.io.FileOutputStream(logFile, true)) {
                o.write(("[proc] " + ts + " " + line + "\n")
                        .getBytes(java.nio.charset.StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
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
