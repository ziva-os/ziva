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
    // Bound on the MAIN thread in start() — under the device's OOM/LMK
    // storms a fresh daemon thread's early log writes have proven
    // unreliable (three rounds of "listening but zero [cdp] lines" all
    // happened while the backend was being SIGKILLed every 60s). The bind
    // itself is microseconds, so doing it synchronously in Application
    // onCreate puts the outcome in the log with the same reliability as
    // the [proc] banner, and a bound socket keeps accepting even if the
    // accept thread later dies and has to be revived.
    private static volatile ServerSocket listener;
    // Total accepted connections — surfaced in status() so the startup
    // banner distinguishes "accept never ran" from "accepted but upstream
    // unresponsive".
    private static int conns = 0;
    // One-line human-readable state, set wherever the bridge state changes.
    // Surfaced inside the backend-starting banner (the ONLY log line proven
    // to always reach the exported log) — see ZivaController.startBackend.
    private static volatile String status = "not started";

    /** Current bridge state for the startup banner. */
    public static String status() { return status; }

    /**
     * True socket name, discovered at first use from /proc/net/unix —
     * "webview_devtools_remote_<pid>" is a GUESS; some WebView builds
     * name the socket differently, and a wrong name means pipe()
     * forwards into a black hole (curl sees connect-then-timeout,
     * rc=28, exactly what the device logs show).
     */
    private static volatile String discoveredSock;

    private static String findDevtoolsSocket() {
        String guessed = "webview_devtools_remote_" + android.os.Process.myPid();
        try (java.io.BufferedReader r = new java.io.BufferedReader(
                new java.io.FileReader("/proc/net/unix"))) {
            String line;
            String found = null;
            while ((line = r.readLine()) != null) {
                int at = line.indexOf(" devtools_remote");
                int wv = line.indexOf(" webview_devtools_remote");
                String name = null;
                if (wv >= 0) name = line.substring(wv + 1).trim();
                else if (at >= 0) name = line.substring(at + 1).trim();
                if (name != null && !name.isEmpty()) {
                    if (name.equals(guessed)) return name;   // exact guess wins
                    if (found == null || name.startsWith("webview")) found = name;
                }
            }
            if (found != null) return found;
        } catch (Throwable ignored) {
        }
        return guessed;
    }

    public static synchronized void start() {
        if (started) return;
        started = true;
        // Process-wide switch; must precede WebView creation. Enables the
        // devtools socket for the main UI WebView and every tab WebView.
        WebView.setWebContentsDebuggingEnabled(true);
        String sockName = "webview_devtools_remote_" + android.os.Process.myPid();
        try {
            // Literal IPv4 loopback — NOT getLoopbackAddress(), which
            // returns ::1 on IPv6-capable devices, invisible to the
            // guest's curl 127.0.0.1 probe.
            ServerSocket ss = new ServerSocket(PORT, 50,
                    InetAddress.getByName("127.0.0.1"));
            listener = ss;
            status = "listening on 127.0.0.1:" + PORT + " -> " + sockName;
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] bridge listening on 127.0.0.1:" + PORT + " -> " + sockName);
        } catch (Throwable t) {
            status = "down: " + t.getClass().getName()
                    + (t.getMessage() != null ? ": " + t.getMessage() : "");
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] bridge down: " + status);
            return;
        }
        Thread t = new Thread(DevtoolsBridge::acceptLoop, "devtools-bridge");
        t.setDaemon(true);
        t.start();
    }

    /** Accept loop with unbounded Throwable-safe retries. Never exits. */
    private static void acceptLoop() {
        String sockName = "webview_devtools_remote_" + android.os.Process.myPid();
        while (true) {
            try {
                ServerSocket ss = listener;
                if (ss == null || ss.isClosed()) {
                    ss = new ServerSocket(PORT, 50,
                            InetAddress.getByName("127.0.0.1"));
                    // Wake accept() every second: the thread can never sit
                    // wedged in a blocking accept, and status() keeps
                    // moving even when no client is around.
                    ss.setSoTimeout(1000);
                    listener = ss;
                    status = "re-listening on 127.0.0.1:" + PORT;
                    ZivaController.appendProcLog(ZivaController.logFile(),
                            "[cdp] bridge re-listening on 127.0.0.1:" + PORT);
                }
                while (true) {
                    try {
                        Socket in = ss.accept();
                        new Thread(() -> pipe(in, sockName), "cdp-conn").start();
                    } catch (java.net.SocketTimeoutException ste) {
                        // Heartbeat tick — refresh status so a banner read
                        // shows the accept loop is alive.
                        status = "listening (accept alive); conns=" + conns
                                + " -> " + (discoveredSock != null ? discoveredSock : sockName);
                    }
                }
            } catch (Throwable t) {
                // NEVER let this thread die: appendProcLog itself can throw
                // under OOM (fd/heap exhaustion), and an unguarded throw
                // here would fly out of the loop and kill the accept thread
                // — after which every future connection just piles up in
                // the backlog and times out (rc=28) until the app restarts.
                try {
                    status = "accept error: " + t.getClass().getName();
                    ZivaController.appendProcLog(ZivaController.logFile(),
                            "[cdp] accept loop error: " + t.getClass().getName()
                                    + (t.getMessage() != null ? ": " + t.getMessage() : ""));
                } catch (Throwable ignored) {
                }
            }
            try {
                Thread.sleep(2000);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    /** One client connection: fresh LocalSocket per connection, bidirectional pump. */
    private static void pipe(Socket in, String sockName) {
        // Re-discover per connection (cheap /proc read) and track activity
        // in status() — the banner reports conns=N so the next log tells
        // apart "accept never ran" (conns=0) from "accepted but upstream
        // dead" (conns>0).
        String target = discoveredSock != null ? discoveredSock : findDevtoolsSocket();
        discoveredSock = target;
        int n;
        synchronized (DevtoolsBridge.class) {
            conns++;
            n = conns;
        }
        status = "listening; conns=" + n + " -> " + target;
        LocalSocket ls = new LocalSocket();
        try {
            // connect with a deadline: LocalSocket.connect has no timeout
            // parameter and can wedge indefinitely if the upstream's listen
            // backlog is saturated — a wedged connect leaves the client
            // hanging with no bytes, which is exactly probe rc=28.
            final LocalSocketAddress addr = new LocalSocketAddress(target,
                    LocalSocketAddress.Namespace.ABSTRACT);
            final LocalSocket s = ls;
            Thread connector = new Thread(() -> {
                try {
                    s.connect(addr);
                } catch (Throwable ignored) {
                }
            }, "cdp-conn-upstream");
            connector.setDaemon(true);
            connector.start();
            connector.join(2000);
            if (!ls.isConnected()) {
                throw new java.io.IOException("upstream connect timeout: " + target);
            }
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
            // THE root cause of every rc=28 to date: this used to be
            // ls.getOutputStream(), which pumps upstream's RESPONSE back
            // into upstream itself — a data black hole where the client
            // gets zero bytes and times out. It must be the CLIENT side.
            final OutputStream toClient = in.getOutputStream();
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
