package com.zivaos.ziva;

import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Loopback command bridge (127.0.0.1:3090) — the agent inside the rootfs calls
 * these endpoints to use host device capabilities (notifications, clipboard…).
 * Every request must carry ?token= matching the token file.
 *
 * Binding is explicitly IPv4 127.0.0.1: InetAddress.getLoopbackAddress() may
 * return ::1 on Android while rootfs clients dial IPv4 — a loopback-only
 * listener on ::1 silently never receives them (an inherited DSHA lesson).
 * Android has no com.sun.net.httpserver, hence the hand-rolled GET parser.
 */
public class HttpShellService {
    private ServerSocket serverSocket;
    private Thread acceptThread;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private String token = "";
    private Context appContext;

    public volatile String lastError = "";

    public synchronized void start(Context ctx) {
        if (!running.compareAndSet(false, true)) return;
        this.appContext = ctx.getApplicationContext();
        try {
            token = readOrCreateToken(ctx);
            // Dual-stack: primary IPv4, plus [::1] for clients resolving localhost.
            serverSocket = new ServerSocket(Constants.BRIDGE_PORT, 8, InetAddress.getByName("127.0.0.1"));
            lastError = "";
            acceptThread = new Thread(this::acceptLoop, "ziva-bridge");
            acceptThread.setDaemon(true);
            acceptThread.start();
        } catch (Exception e) {
            lastError = String.valueOf(e);
            running.set(false);
        }
    }

    public synchronized void stop() {
        running.set(false);
        try { if (serverSocket != null) serverSocket.close(); } catch (Exception ignored) {}
        serverSocket = null;
    }

    public boolean isRunning() { return running.get(); }

    private void acceptLoop() {
        while (running.get()) {
            try {
                Socket s = serverSocket.accept();
                handle(s);
            } catch (Exception e) {
                if (running.get()) { /* transient */ }
            }
        }
    }

    private void handle(Socket s) {
        Thread t = new Thread(() -> {
            try (Socket sock = s; BufferedReader in = new BufferedReader(
                    new InputStreamReader(sock.getInputStream(), StandardCharsets.US_ASCII))) {
                String requestLine = in.readLine();
                if (requestLine == null) return;
                String[] parts = requestLine.split(" ");
                if (parts.length < 2) return;
                String pathAndQuery = parts[1];
                String path = pathAndQuery;
                String query = "";
                int qm = pathAndQuery.indexOf('?');
                if (qm >= 0) { path = pathAndQuery.substring(0, qm); query = pathAndQuery.substring(qm + 1); }
                // Drain headers (unused, but the client blocks until a full request is read).
                String line;
                while ((line = in.readLine()) != null && !line.isEmpty()) { /* skip */ }

                if (!query.contains("token=" + token)) {
                    respond(sock, 401, "{\"result\":\"unauthorized\"}");
                    return;
                }
                switch (path) {
                    case "/app/notify": {
                        notifyUser(getParam(query, "title", "Ziva"), getParam(query, "text", ""));
                        respond(sock, 200, "{\"result\":\"notified\"}");
                        break;
                    }
                    case "/app/clip": {
                        String text = getParam(query, "text", null);
                        if (text == null) respond(sock, 200, "{\"result\":\"" + escapeJson(getClipboard()) + "\"}");
                        else { setClipboard(text); respond(sock, 200, "{\"result\":\"copied\"}"); }
                        break;
                    }
                    case "/app/vibrate": {
                        android.os.Vibrator v = (android.os.Vibrator) appContext.getSystemService(Context.VIBRATOR_SERVICE);
                        if (v != null) v.vibrate(120);
                        respond(sock, 200, "{\"result\":\"vibrated\"}");
                        break;
                    }
                    case "/app/device": {
                        respond(sock, 200, "{\"model\":\"" + escapeJson(Build.MODEL) + "\",\"android\":\""
                                + Build.VERSION.RELEASE + "\",\"app\":\"ziva-android\"}");
                        break;
                    }
                    case "/health":
                        respond(sock, 200, "{\"result\":\"ok\"}");
                        break;
                    default:
                        respond(sock, 404, "{\"result\":\"unknown endpoint\"}");
                }
            } catch (Exception ignored) {
            }
        }, "ziva-bridge-req");
        t.setDaemon(true);
        t.start();
    }

    private void notifyUser(String title, String text) {
        NotificationManager nm = (NotificationManager) appContext.getSystemService(Context.NOTIFICATION_SERVICE);
        android.app.Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            android.app.NotificationChannel ch = new android.app.NotificationChannel("ziva-bridge", "Ziva 桥", NotificationManager.IMPORTANCE_DEFAULT);
            nm.createNotificationChannel(ch);
            b = new android.app.Notification.Builder(appContext, "ziva-bridge");
        } else {
            b = new android.app.Notification.Builder(appContext);
        }
        Intent open = new Intent(appContext, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(appContext, 1, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        b.setContentTitle(title).setContentText(text).setSmallIcon(android.R.drawable.ic_dialog_info).setContentIntent(pi);
        nm.notify(1001, b.build());
    }

    private void setClipboard(String text) {
        ClipboardManager cm = (ClipboardManager) appContext.getSystemService(Context.CLIPBOARD_SERVICE);
        cm.setPrimaryClip(ClipData.newPlainText("ziva", text));
    }

    private String getClipboard() {
        ClipboardManager cm = (ClipboardManager) appContext.getSystemService(Context.CLIPBOARD_SERVICE);
        ClipData cd = cm.getPrimaryClip();
        return cd != null && cd.getItemCount() > 0 ? String.valueOf(cd.getItemAt(0).getText()) : "";
    }

    private static String getParam(String query, String key, String def) {
        for (String kv : query.split("&")) {
            int eq = kv.indexOf('=');
            if (eq > 0 && kv.substring(0, eq).equals(key)) {
                return urlDecode(kv.substring(eq + 1));
            }
        }
        return def;
    }

    private static String urlDecode(String s) {
        try { return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8.name()); }
        catch (Exception e) { return s; }
    }

    private static String escapeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "");
    }

    private void respond(Socket s, int code, String body) throws Exception {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        OutputStream os = s.getOutputStream();
        os.write(("HTTP/1.1 " + code + " OK\r\nContent-Type: application/json\r\nContent-Length: " + b.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        os.write(b);
        os.flush();
    }

    private String readOrCreateToken(Context ctx) throws Exception {
        File dir = Constants.publicDataDir();
        File tokenFile = new File(dir, ".bridge_token");
        // The fallback must be inside the SAME dir ProotBootstrap binds to
        // /root/.ziva when the public dir is unwritable (files/ziva-data),
        // or the guest agent could never read the token.
        File privateFallback = new File(new File(ctx.getFilesDir(), "ziva-data"), ".bridge_token");
        File target = ProotBootstrap.dataDirCanWrite(dir) ? tokenFile : privateFallback;
        String existing = readTokenFile(target);
        if (existing != null) return existing;
        String t = java.util.UUID.randomUUID().toString().replace("-", "");
        // Classic IO + a second fallback: even a directory that passed the
        // write probe can still refuse this particular file (OEM quirks).
        try {
            writeTokenFile(target, t);
        } catch (Exception publicRefused) {
            writeTokenFile(privateFallback, t);
        }
        return t;
    }

    private static String readTokenFile(File f) {
        try {
            if (!f.isFile()) return null;
            byte[] b = new byte[(int) f.length()];
            try (java.io.FileInputStream fin = new java.io.FileInputStream(f)) {
                int off = 0, n;
                while (off < b.length && (n = fin.read(b, off, b.length - off)) > 0) off += n;
            }
            String t = new String(b, StandardCharsets.UTF_8).trim();
            return t.isEmpty() ? null : t;
        } catch (Exception e) {
            return null;
        }
    }

    private static void writeTokenFile(File f, String t) throws Exception {
        File parent = f.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        try (java.io.FileOutputStream fos = new java.io.FileOutputStream(f)) {
            fos.write(t.getBytes(StandardCharsets.UTF_8));
            fos.getFD().sync();
        }
    }
}
