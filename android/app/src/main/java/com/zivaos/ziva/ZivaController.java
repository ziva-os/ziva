package com.zivaos.ziva;

import android.content.Context;
import java.io.BufferedReader;
import java.io.File;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.List;

/**
 * Business core: extract → verify → start/stop backend → health.
 * Kept deliberately process-focused; UI classes call into this singleton.
 */
public final class ZivaController {
    private static volatile ZivaController sInstance;
    private static final Object LOCK = new Object();

    private Process backendProc;
    private Thread logPump;
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
        try {
            List<String> cmd = ProotBootstrap.backendCommand(ctx);
            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            backendProc = pb.start();
            startedAt = System.currentTimeMillis();
            lastError = "";
            pumpLogs(backendProc, ctx);
            return true;
        } catch (Exception e) {
            lastError = "启动失败: " + e;
            return false;
        }
    }

    public synchronized void stopBackend() {
        Process p = backendProc;
        backendProc = null;
        if (p == null) return;
        // destroy() signals the direct child (proot); its --kill-on-exit then
        // tears the whole guest process tree down with it. Note: we cannot
        // use java.lang.Process.pid() here — that API only exists from
        // Android 15 (API 35), we support down to 26.
        p.destroy();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()))) {
            while (r.readLine() != null) { /* drain */ }
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

    private void pumpLogs(Process proc, Context ctx) {
        logPump = new Thread(() -> {
            File fallback = new File(ctx.getFilesDir(), "ziva-android.log");
            try (BufferedReader r = new BufferedReader(new InputStreamReader(proc.getInputStream()))) {
                String line;
                java.io.FileWriter fw;
                try {
                    File log = logFile();
                    if (log.getParentFile() != null) log.getParentFile().mkdirs();
                    fw = new java.io.FileWriter(log, true);          // public dir (no grant → throws)
                } catch (Exception noPublic) {
                    fw = new java.io.FileWriter(fallback, true);     // app-private fallback
                }
                try (java.io.BufferedWriter bw = new java.io.BufferedWriter(fw)) {
                    while ((line = r.readLine()) != null) {
                        bw.write(line);
                        bw.newLine();
                    }
                }
            } catch (Exception ignored) {}
        }, "ziva-log-pump");
        logPump.setDaemon(true);
        logPump.start();
    }

    /** Public log path (needs "All files access"); shown in Diagnostics. */
    public static File logFile() {
        return new File("/sdcard/Documents/zivadata/ziva-android.log");
    }

    private static void deleteTree(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteTree(k);
        f.delete();
    }
}
