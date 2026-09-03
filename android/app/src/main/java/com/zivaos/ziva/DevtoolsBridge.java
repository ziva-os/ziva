package com.zivaos.ziva;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.webkit.WebView;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * Desktop-parity CDP bridge: exposes the WebView devtools socket
 * (localabstract:webview_devtools_remote_<pid>) on 127.0.0.1:9222 so
 * chrome-devtools-mcp inside the proot guest can attach to the user's
 * REAL browser tabs with --browser-url — the same architecture as the
 * Electron desktop app's cdp-bridge.
 *
 * Pure TCP pipe: HTTP (/json/*) and WebSocket upgrades pass through
 * untouched, so every WebView tab (main UI + WebTabManager pages) shows
 * up as a CDP target, exactly like desktop tabs do.
 *
 * Loopback-only listener. Note the abstract devtools socket itself is
 * reachable by any local app on Android — same exposure the platform's
 * own debugging path has; nothing here widens it to the network.
 *
 * Reliability contract (r39): on the test device this bridge vanished
 * WITHOUT A TRACE — zero [cdp] lines while the identical appendProcLog
 * call kept writing [proc] banners into the same file. The only silent
 * path left was a Throwable (OOM under the device's 60s LMK cycle)
 * escaping catch(Exception) and killing the thread before the log line.
 * Contract now: bind on the calling thread (loopback bind is
 * microseconds — synchronous so the outcome lands in the log even if
 * the thread machinery is the thing dying), catch Throwable everywhere,
 * retry with bounded backoff forever. The bridge can no longer die
 * without writing WHY.
 */
public final class DevtoolsBridge {
    private static final int PORT = 9222;
    private static volatile boolean started = false;

    public static synchronized void start() {
        if (started) return;
        started = true;
        // Process-wide switch; must precede WebView creation. Enables the
        // devtools socket for the main UI WebView and every tab WebView.
        WebView.setWebContentsDebuggingEnabled(true);
        Thread t = new Thread(DevtoolsBridge::serveLoop, "devtools-bridge");
        t.setDaemon(true);
        t.start();
    }

    /** Accept loop with unbounded Throwable-safe retries. Never exits. */
    private static void serveLoop() {
        String sockName = "webview_devtools_remote_" + android.os.Process.myPid();
        long backoff = 2000;
        while (true) {
            try (ServerSocket ss = new ServerSocket(PORT, 50, InetAddress.getLoopbackAddress())) {
                ZivaController.appendProcLog(ZivaController.logFile(),
                        "[cdp] bridge listening on 127.0.0.1:" + PORT + " -> " + sockName);
                backoff = 2000;
                while (true) {
                    Socket in = ss.accept();
                    new Thread(() -> pipe(in, sockName), "cdp-conn").start();
                }
            } catch (Throwable t) {
                ZivaController.appendProcLog(ZivaController.logFile(),
                        "[cdp] bridge down: " + t.getClass().getName()
                                + (t.getMessage() != null ? ": " + t.getMessage() : "")
                                + " — retry in " + (backoff / 1000) + "s");
            }
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException e) {
                return;
            }
            backoff = Math.min(backoff * 2, 30000);
        }
    }

    /** One client connection: fresh LocalSocket per connection, bidirectional pump. */
    private static void pipe(Socket in, String sockName) {
        LocalSocket ls = new LocalSocket();
        try {
            ls.connect(new LocalSocketAddress(sockName,
                    LocalSocketAddress.Namespace.ABSTRACT));
        } catch (Throwable t) {
            // Devtools socket not up yet (no WebView created / debugging
            // still initializing) — drop the probe connection quietly.
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] upstream not ready: " + t);
            try { in.close(); } catch (Exception ignored) {}
            return;
        }
        try {
            final InputStream fromClient = in.getInputStream();
            final OutputStream toUpstream = ls.getOutputStream();
            final InputStream fromUpstream = ls.getInputStream();
            final OutputStream toClient = ls.getOutputStream();
            Thread up = new Thread(() -> pump(fromClient, toUpstream));
            up.setDaemon(true);
            up.start();
            pump(fromUpstream, toClient);   // hold this side on the conn thread
        } catch (Throwable t) {
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] pipe error: " + t);
            try { in.close(); } catch (Exception ignored) {}
        } finally {
            try { ls.close(); } catch (Exception ignored) {}
        }
    }

    private static void pump(InputStream in, OutputStream out) {
        byte[] buf = new byte[16384];
        try {
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                out.flush();
            }
        } catch (Exception ignored) {
        }
        try { in.close(); } catch (Exception ignored) {}
        try { out.close(); } catch (Exception ignored) {}
    }
}
