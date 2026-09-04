package com.zivaos.ziva;

import android.app.Activity;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Desktop-parity CDP bridge (protocol-aware).
 *
 * Architecture mirrors electron/cdp-bridge.ts: the bridge TERMINATES the
 * browser-level protocol instead of dumb-piping it, because Android
 * WebView's devtools server lacks browser-level target management — the
 * exact failure on device: "Protocol error (Target.createTarget): Not
 * supported", after which the agent's only visible target was the Ziva
 * UI itself (http://127.0.0.1:4097) and navigate_page drove the chat
 * surface to weibo.
 *
 * Contract:
 *   HTTP  /json/version      — answered by the bridge (upstream body,
 *                              webSocketDebuggerUrl repointed here)
 *   HTTP  /json/list, /json  — answered by the bridge: upstream list with
 *                              the Ziva UI target filtered out and ws URLs
 *                              repointed here
 *   WS    /devtools/browser* — terminated here, frame-level:
 *         Target.createTarget   -> WebTabManager.createTab, diff the new
 *                                  WebView's real targetId, answer with it,
 *                                  notify the renderer (tabCreated) so the
 *                                  tab strip + browserSetArea take over
 *         Target.closeTarget    -> closeTab + renderer tabClosed
 *         Target.activateTarget -> showTab
 *         Target.getTargets     -> synthesized, Ziva UI filtered
 *         everything else       -> forwarded byte-for-byte (upstream handles
 *                                  setAutoAttach, attachToTarget, Page and
 *                                  Runtime traffic - proven on device)
 *         upstream->client      -> events for the Ziva UI target dropped,
 *                                  everything else forwarded untouched
 *   WS    /devtools/page/*    -> dumb pipe (upstream serves these natively)
 *   anything else             -> dumb pipe
 *
 * Threading: one thread per direction per connection, same as before.
 * All bridge bookkeeping uses ConcurrentHashMap / volatile; renderer calls
 * hop to the UI thread via the attached activity.
 */
public final class DevtoolsBridge {
    private static final int PORT = 9222;

    // --- wiring set by MainActivity -------------------------------------
    private static volatile WebTabManager webTabs;
    private static volatile Activity uiActivity;

    // targetId -> tabId for tabs we know about (agent-created and
    // user-created alike — closeTarget must work for both, like desktop).
    private static final Map<String, String> targetToTab = new ConcurrentHashMap<>();
    // Ziva UI target ids (url starts with the backend base): never exposed.
    private static final Set<String> mainUiTargets = ConcurrentHashMap.newKeySet();
    // Cached upstream /devtools/browser path (uuid is per-boot random).
    private static volatile String upstreamBrowserPath;

    private static volatile boolean started = false;
    private static volatile ServerSocket listener;
    private static int conns = 0;
    private static volatile String status = "not started";

    public static String status() { return status; }

    /** MainActivity wires the tab manager + UI thread hop. */
    public static void attach(WebTabManager tabs, Activity activity) {
        webTabs = tabs;
        uiActivity = activity;
    }

    private static String backendBase() {
        return "http://127.0.0.1:" + Constants.BACKEND_PORT;
    }

    // ------------------------------------------------------------------ start

    public static synchronized void start() {
        if (started) return;
        started = true;
        WebView.setWebContentsDebuggingEnabled(true);
        String sockName = "webview_devtools_remote_" + android.os.Process.myPid();
        try {
            ServerSocket ss = new ServerSocket(PORT, 50,
                    InetAddress.getByName("127.0.0.1"));
            ss.setSoTimeout(1000);
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

    private static void acceptLoop() {
        String sockName = "webview_devtools_remote_" + android.os.Process.myPid();
        while (true) {
            try {
                ServerSocket ss = listener;
                if (ss == null || ss.isClosed()) {
                    ss = new ServerSocket(PORT, 50,
                            InetAddress.getByName("127.0.0.1"));
                    ss.setSoTimeout(1000);
                    listener = ss;
                    status = "re-listening on 127.0.0.1:" + PORT;
                    ZivaController.appendProcLog(ZivaController.logFile(),
                            "[cdp] bridge re-listening on 127.0.0.1:" + PORT);
                }
                while (true) {
                    try {
                        Socket in = ss.accept();
                        new Thread(() -> handle(in, sockName), "cdp-conn").start();
                    } catch (java.net.SocketTimeoutException ste) {
                        status = "listening (accept alive); conns=" + conns
                                + " -> " + sockName;
                    }
                }
            } catch (Throwable t) {
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

    // -------------------------------------------------------------- discovery

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
                    if (name.equals(guessed)) return name;
                    if (found == null || name.startsWith("webview")) found = name;
                }
            }
            if (found != null) return found;
        } catch (Throwable ignored) {
        }
        return guessed;
    }

    // ------------------------------------------------------------- connection

    private static void handle(Socket in, String sockName) {
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

            // Read the client's first request head, then route.
            byte[] head = readHttpHead(in.getInputStream());
            String headStr = new String(head, StandardCharsets.UTF_8);
            String path = firstPath(headStr);
            refreshMainUiTargetsSilently();

            if (path.startsWith("/json/version")) {
                answerJson(in, upstreamVersionJson());
            } else if (path.startsWith("/json/list") || path.equals("/json")
                    || path.startsWith("/json")) {
                answerJson(in, upstreamListFiltered());
            } else if (path.startsWith("/devtools/browser")) {
                bridgeBrowserSession(in, ls, headStr);
                return; // browser session owns both sockets until death
            } else {
                // /devtools/page/* and anything else: dumb pipe. Rewrite is
                // unnecessary — upstream serves these paths natively.
                dumbPipe(in, ls, head);
            }
        } catch (Throwable t) {
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] upstream not ready: " + t);
        } finally {
            try { ls.close(); } catch (Throwable ignored) {}
            try { in.close(); } catch (Throwable ignored) {}
        }
    }

    // --------------------------------------------------------------- HTTP end

    /** Read bytes up to and including the blank line of an HTTP request head. */
    private static byte[] readHttpHead(InputStream is) throws java.io.IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int last4 = 0;
        while (true) {
            int b = is.read();
            if (b < 0) break;
            buf.write(b);
            last4 = ((last4 << 8) | b) & 0xffffffff;
            if (last4 == 0x0d0a0d0a) break; // \r\n\r\n
            if (buf.size() > 65536) break;
        }
        return buf.toByteArray();
    }

    private static String firstPath(String head) {
        int sp = head.indexOf(' ');
        int sp2 = sp < 0 ? -1 : head.indexOf(' ', sp + 1);
        if (sp <= 0 || sp2 <= sp) return "/";
        return head.substring(sp + 1, sp2).split("\\?")[0];
    }

    private static void answerJson(Socket in, String body) throws java.io.IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        String resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + "Content-Length: " + b.length + "\r\nConnection: close\r\n\r\n";
        OutputStream os = in.getOutputStream();
        os.write(resp.getBytes(StandardCharsets.UTF_8));
        os.write(b);
        os.flush();
    }

    /** Open a short-lived HTTP GET to the upstream devtools socket. */
    private static String queryUpstream(String path) throws java.io.IOException {
        String target = discoveredSock != null ? discoveredSock : findDevtoolsSocket();
        LocalSocket s = new LocalSocket();
        try {
            s.connect(new LocalSocketAddress(target,
                    LocalSocketAddress.Namespace.ABSTRACT));
            OutputStream os = s.getOutputStream();
            os.write(("GET " + path + " HTTP/1.1\r\nHost: localhost\r\n"
                    + "Connection: close\r\n\r\n").getBytes(StandardCharsets.UTF_8));
            os.flush();
            InputStream is = s.getInputStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[16384];
            int n;
            while ((n = is.read(b)) > 0) buf.write(b, 0, n);
            String raw = new String(buf.toByteArray(), StandardCharsets.UTF_8);
            int split = raw.indexOf("\r\n\r\n");
            String head = split >= 0 ? raw.substring(0, split) : raw;
            String body = split >= 0 ? raw.substring(split + 4) : "";
            // Honor Content-Length when present (chunked is not used by the
            // WebView devtools handler, but truncate defensively anyway).
            for (String line : head.split("\r\n")) {
                if (line.toLowerCase().startsWith("content-length:")) {
                    int len = Integer.parseInt(line.substring(15).trim());
                    if (len < body.length()) body = body.substring(0, len);
                    break;
                }
            }
            return body;
        } finally {
            try { s.close(); } catch (Throwable ignored) {}
        }
    }

    private static String upstreamVersionJson() {
        try {
            String body = queryUpstream("/json/version");
            JSONObject o = new JSONObject(body);
            o.put("webSocketDebuggerUrl",
                    "ws://127.0.0.1:" + PORT + "/devtools/browser");
            String p = ""; // remember the upstream browser path for upgrades
            String orig = new JSONObject(body).optString("webSocketDebuggerUrl", "");
            int i = orig.indexOf("/devtools/browser");
            if (i >= 0) p = orig.substring(i);
            if (!p.isEmpty()) upstreamBrowserPath = p;
            return o.toString();
        } catch (Throwable t) {
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] version query failed: " + t);
            return "{}";
        }
    }

    /** Upstream /json/list as a JSONArray with the Ziva UI target removed
     *  and ws URLs repointed at this bridge. Also refreshes mainUiTargets. */
    private static JSONArray upstreamListFiltered() throws java.io.IOException {
        JSONArray arr = new JSONArray(queryUpstream("/json/list"));
        JSONArray out = new JSONArray();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o == null) continue;
            String url = o.optString("url", "");
            String id = o.optString("id", "");
            if (url.startsWith(backendBase()) || mainUiTargets.contains(id)) {
                mainUiTargets.add(id);
                continue;
            }
            o.put("webSocketDebuggerUrl",
                    "ws://127.0.0.1:" + PORT + "/devtools/page/" + id);
            out.put(o);
        }
        return out;
    }

    private static void refreshMainUiTargetsSilently() {
        try {
            JSONArray arr = new JSONArray(queryUpstream("/json/list"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                if (o.optString("url", "").startsWith(backendBase())) {
                    mainUiTargets.add(o.optString("id", ""));
                }
            }
        } catch (Throwable ignored) {
        }
    }

    // -------------------------------------------------------- browser session

    /**
     * Frame-level bridge for the /devtools/browser WebSocket. The client's
     * upgrade request is rewritten to the upstream browser path and sent
     * upstream; the 101 flows back through the response pump untouched.
     * Client->upstream frames are inspected; intercepted methods never reach
     * upstream. Upstream->client frames are inspected; Ziva-UI events are
     * dropped, everything else forwarded byte-for-byte.
     */
    private static void bridgeBrowserSession(Socket in, LocalSocket ls, String clientHead) {
        try {
            if (upstreamBrowserPath == null) upstreamVersionJson(); // learn the uuid
            String upPath = upstreamBrowserPath != null ? upstreamBrowserPath
                    : "/devtools/browser";
            // Rewrite the request line onto the upstream path; keep every
            // other header (incl. Sec-WebSocket-Key) so upstream's 101
            // Accept matches what the client will verify.
            int eol = clientHead.indexOf("\r\n");
            String rest = eol >= 0 ? clientHead.substring(eol) : "";
            String upReq = "GET " + upPath + " HTTP/1.1" + rest;
            ls.getOutputStream().write(upReq.getBytes(StandardCharsets.UTF_8));
            ls.getOutputStream().flush();

            // Relay upstream's HTTP response head (the 101) BYTE-FOR-BYTE
            // before any frame parsing — it is plain HTTP text, not a frame.
            InputStream uis = ls.getInputStream();
            ByteArrayOutputStream resp = new ByteArrayOutputStream();
            int last4 = 0;
            while (true) {
                int b = uis.read();
                if (b < 0) break;
                resp.write(b);
                last4 = ((last4 << 8) | b) & 0xffffffff;
                if (last4 == 0x0d0a0d0a) break;
                if (resp.size() > 65536) break;
            }
            in.getOutputStream().write(resp.toByteArray());
            in.getOutputStream().flush();
            if (!resp.toString(StandardCharsets.UTF_8.name()).contains(" 101 ")) {
                // Upstream refused the upgrade (bad path etc.) — the error
                // response has been relayed; nothing left to bridge.
                ZivaController.appendProcLog(ZivaController.logFile(),
                        "[cdp] browser upgrade refused upstream");
                return;
            }
        } catch (Throwable t) {
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] browser upgrade failed: " + t);
            return;
        }

        final Socket client = in;
        final LocalSocket upstream = ls;

        // client -> upstream (inspection + interception)
        Thread c2u = new Thread(() -> {
            try {
                InputStream is = client.getInputStream();
                OutputStream os = upstream.getOutputStream();
                while (true) {
                    WsFrame f = WsFrame.read(is);
                    if (f == null) break;
                    if (!f.handled) {
                        if (!handleBrowserCommand(client, f, os)) {
                            os.write(f.raw); // forward untouched
                            os.flush();
                        }
                        // handled == intercepted: swallowed, response synthesized
                    } else {
                        os.write(f.raw); // control/continuation frames pass
                        os.flush();
                    }
                }
            } catch (Throwable ignored) {
            } finally {
                try { upstream.close(); } catch (Throwable ignored) {}
                try { client.close(); } catch (Throwable ignored) {}
            }
        }, "cdp-c2u");
        c2u.setDaemon(true);
        c2u.start();

        // upstream -> client (drop Ziva-UI target events, forward the rest)
        try {
            InputStream is = upstream.getInputStream();
            OutputStream os = client.getOutputStream();
            while (true) {
                WsFrame f = WsFrame.read(is);
                if (f == null) break;
                if (f.handled || !dropIfMainUiEvent(f)) {
                    os.write(f.raw);
                    os.flush();
                }
            }
        } catch (Throwable ignored) {
        } finally {
            try { upstream.close(); } catch (Throwable ignored) {}
            try { client.close(); } catch (Throwable ignored) {}
        }
    }

    /** @return true if the frame was an intercepted browser-level command. */
    private static boolean handleBrowserCommand(Socket client, WsFrame f, OutputStream up) {
        JSONObject msg;
        try {
            msg = new JSONObject(new String(f.payload, StandardCharsets.UTF_8));
        } catch (Throwable t) {
            return false; // not JSON — forward untouched
        }
        String method = msg.optString("method", "");
        if (msg.opt("id") == null || method.isEmpty()) return false;

        if ("Target.getBrowserContexts".equals(method)) {
            // WebView has no browser contexts; puppeteer expects the call to
            // succeed (the desktop bridge answers the same way).
            long id = msg.optLong("id", -1);
            WsFrame.sendText(client, "{\"id\":" + id
                    + ",\"result\":{\"browserContextIds\":[]}}");
            return true;
        }
        if ("Target.createTarget".equals(method)) {
            final long id = msg.optLong("id", -1);
            final String url = msg.optJSONObject("params") == null ? "about:blank"
                    : msg.optJSONObject("params").optString("url", "about:blank");
            Thread w = new Thread(() -> handleCreateTarget(client, id, url), "cdp-create");
            w.setDaemon(true);
            w.start();
            return true;
        }
        if ("Target.closeTarget".equals(method)) {
            long id = msg.optLong("id", -1);
            String tid = msg.optJSONObject("params") == null ? null
                    : msg.optJSONObject("params").optString("targetId", null);
            boolean ok = closeTabByTarget(tid);
            if (ok) {
                WsFrame.sendText(client, "{\"id\":" + id + ",\"result\":{}}");
            } else {
                sendError(client, id, "closeTarget: unknown target " + tid);
            }
            return true;
        }
        if ("Target.activateTarget".equals(method)) {
            long id = msg.optLong("id", -1);
            String tid = msg.optJSONObject("params") == null ? null
                    : msg.optJSONObject("params").optString("targetId", null);
            WebTabManager tabs = webTabs;
            String tabId = tabForTarget(tid);
            if (tabs != null && tabId != null) tabs.showTab(tabId);
            WsFrame.sendText(client, "{\"id\":" + id + ",\"result\":{}}");
            return true;
        }
        if ("Target.getTargets".equals(method)) {
            long id = msg.optLong("id", -1);
            try {
                JSONArray list = upstreamListFiltered();
                JSONArray infos = new JSONArray();
                for (int i = 0; i < list.length(); i++) {
                    JSONObject o = list.getJSONObject(i);
                    infos.put(new JSONObject()
                            .put("targetId", o.optString("id", ""))
                            .put("type", o.optString("type", "page"))
                            .put("title", o.optString("title", ""))
                            .put("url", o.optString("url", ""))
                            .put("attached", false));
                }
                WsFrame.sendText(client, new JSONObject()
                        .put("id", id)
                        .put("result", new JSONObject().put("targetInfos", infos))
                        .toString());
            } catch (Throwable t) {
                sendError(client, id, String.valueOf(t));
            }
            return true;
        }
        return false; // forward to upstream untouched
    }

    /** Error response with proper JSON escaping (exception text may quote). */
    private static void sendError(Socket client, long id, String message) {
        try {
            WsFrame.sendText(client, new JSONObject()
                    .put("id", id)
                    .put("error", new JSONObject()
                            .put("code", -32000).put("message", message))
                    .toString());
        } catch (Throwable ignored) {
        }
    }

    private static void handleCreateTarget(Socket client, long id, String url) {
        try {
            WebTabManager tabs = webTabs;
            if (tabs == null) throw new IllegalStateException("WebTabManager not attached");

            Set<String> before = new HashSet<>(currentTargetIds());

            String tabId = tabs.createTab(url);
            dispatchRenderer("tabCreated", new JSONObject()
                    .put("id", tabId).put("url", url));

            // The WebView is built on the UI thread; poll until its real
            // target shows up in the upstream list.
            String tid = null;
            long deadline = System.currentTimeMillis() + 6000;
            while (System.currentTimeMillis() < deadline) {
                for (String t : currentTargetIds()) {
                    if (!before.contains(t) && !mainUiTargets.contains(t)) {
                        tid = t;
                        break;
                    }
                }
                if (tid != null) break;
                try { Thread.sleep(200); } catch (InterruptedException e) { break; }
            }
            if (tid == null) throw new IllegalStateException("tab target never appeared");

            targetToTab.put(tid, tabId);
            WsFrame.sendText(client, new JSONObject()
                    .put("id", id)
                    .put("result", new JSONObject().put("targetId", tid))
                    .toString());
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] createTarget url=" + url + " tab=" + tabId + " target=" + tid);
        } catch (Throwable t) {
            sendError(client, id, "createTarget: " + t);
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] createTarget failed: " + t);
        }
    }

    /** Non-Ziva-UI target ids currently known upstream. */
    private static java.util.List<String> currentTargetIds() {
        java.util.ArrayList<String> ids = new java.util.ArrayList<>();
        try {
            JSONArray arr = new JSONArray(queryUpstream("/json/list"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                String u = o.optString("url", "");
                String id = o.optString("id", "");
                if (u.startsWith(backendBase())) {
                    mainUiTargets.add(id);
                    continue;
                }
                ids.add(id);
                // Learn tabId by URL for user-opened tabs so closeTarget
                // works on them too (agent tabs come via targetToTab).
                WebTabManager tabs = webTabs;
                if (tabs != null && !targetToTab.containsKey(id) && !u.isEmpty()
                        && !"about:blank".equals(u)) {
                    String tabId = tabs.tabIdForUrl(u);
                    if (tabId != null) targetToTab.put(id, tabId);
                }
            }
        } catch (Throwable ignored) {
        }
        return ids;
    }

    private static boolean closeTabByTarget(String tid) {
        if (tid == null) return false;
        String tabId = targetToTab.remove(tid);
        WebTabManager tabs = webTabs;
        if (tabId == null || tabs == null) return false;
        tabs.closeTab(tabId);
        dispatchRenderer("tabClosed", new JSONObject().put("id", tabId));
        ZivaController.appendProcLog(ZivaController.logFile(),
                "[cdp] closeTarget target=" + tid + " tab=" + tabId);
        return true;
    }

    private static String tabForTarget(String tid) {
        return tid == null ? null : targetToTab.get(tid);
    }

    /** Push a main->renderer event into the Ziva page (same shape as
     *  WebTabManager.emit: __zivaBrowserDispatch(type, payload)). */
    private static void dispatchRenderer(String type, JSONObject payload) {
        Activity a = uiActivity;
        if (a == null) return;
        final String js = "window.__zivaBrowserDispatch&&window.__zivaBrowserDispatch("
                + JSONObject.quote(type) + "," + payload + ")";
        a.runOnUiThread(() -> {
            try {
                WebTabManager tabs = webTabs;
                if (tabs != null) tabs.evaluateInZiva(js);
            } catch (Throwable ignored) {
            }
        });
    }

    /** @return true (and drops) when the frame is a target event about the
     *          Ziva UI target. */
    private static boolean dropIfMainUiEvent(WsFrame f) {
        try {
            JSONObject msg = new JSONObject(new String(f.payload, StandardCharsets.UTF_8));
            String method = msg.optString("method", "");
            if (!method.startsWith("Target.")) return false;
            JSONObject params = msg.optJSONObject("params");
            if (params == null) return false;
            JSONObject info = params.optJSONObject("targetInfo");
            if (info != null) {
                String url = info.optString("url", "");
                if (url.startsWith(backendBase())) {
                    mainUiTargets.add(info.optString("targetId", ""));
                    return true;
                }
                return mainUiTargets.contains(info.optString("targetId", ""));
            }
            String tid = params.optString("targetId", "");
            return !tid.isEmpty() && mainUiTargets.contains(tid);
        } catch (Throwable t) {
            return false;
        }
    }

    // ------------------------------------------------------------- dumb pipe

    private static void dumbPipe(Socket in, LocalSocket ls, byte[] head) {
        try {
            ls.getOutputStream().write(head); // replay the request head first
            ls.getOutputStream().flush();
        } catch (Throwable ignored) {
            return;
        }
        try {
            final InputStream fromClient = in.getInputStream();
            final OutputStream toUpstream = ls.getOutputStream();
            final InputStream fromUpstream = ls.getInputStream();
            final OutputStream toClient = in.getOutputStream();
            Thread up = new Thread(() -> pump(fromClient, toUpstream));
            up.setDaemon(true);
            up.start();
            pump(fromUpstream, toClient);
        } catch (Throwable t) {
            ZivaController.appendProcLog(ZivaController.logFile(),
                    "[cdp] pipe error: " + t);
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

    // -------------------------------------------------------------- websocket

    /** Minimal RFC6455 frame reader/writer. `handled` marks control or
     *  continuation frames that must be forwarded without JSON inspection. */
    private static final class WsFrame {
        final int opcode;
        final byte[] payload;
        final byte[] raw;      // exact bytes as read (masking preserved)
        final boolean handled; // true = pass-through without inspection

        private WsFrame(int opcode, byte[] payload, byte[] raw, boolean handled) {
            this.opcode = opcode;
            this.payload = payload;
            this.raw = raw;
            this.handled = handled;
        }

        static WsFrame read(InputStream is) throws java.io.IOException {
            int b0 = is.read();
            if (b0 < 0) return null;
            int b1 = is.read();
            if (b1 < 0) return null;
            boolean masked = (b1 & 0x80) != 0;
            int len = b1 & 0x7f;
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            head.write(b0);
            head.write(b1);
            if (len == 126) {
                int h = is.read(), l = is.read();
                if (h < 0 || l < 0) return null;
                head.write(h); head.write(l);
                len = (h << 8) | l;
            } else if (len == 127) {
                len = 0;
                for (int i = 0; i < 8; i++) {
                    int b = is.read();
                    if (b < 0) return null;
                    head.write(b);
                    len = (int) ((len << 8) | (b & 0xff));
                }
            }
            byte[] maskKey = null;
            if (masked) {
                maskKey = new byte[4];
                for (int i = 0; i < 4; i++) {
                    int b = is.read();
                    if (b < 0) return null;
                    head.write(b);
                    maskKey[i] = (byte) b;
                }
            }
            byte[] payload = new byte[len];
            int off = 0;
            while (off < len) {
                int n = is.read(payload, off, len - off);
                if (n < 0) return null;
                off += n;
            }
            byte[] raw = new byte[head.size() + len];
            System.arraycopy(head.toByteArray(), 0, raw, 0, head.size());
            System.arraycopy(payload, 0, raw, head.size(), len);

            int opcode = b0 & 0x0f;
            boolean fin = (b0 & 0x80) != 0;
            boolean text = opcode == 0x1;
            byte[] clear = payload;
            if (masked && maskKey != null) {
                clear = new byte[len];
                for (int i = 0; i < len; i++) clear[i] = (byte) (payload[i] ^ maskKey[i & 3]);
            }
            // Only complete, unfragmented text frames get inspected; every
            // other shape (control frames, continuations, binary) is passed
            // through untouched so we never corrupt a stream we don't grok.
            boolean inspectable = fin && text;
            return new WsFrame(opcode, clear, raw, !inspectable);
        }

        /** Send a server-style (unmasked) text frame. */
        static void sendText(Socket s, String text) {
            try {
                byte[] p = text.getBytes(StandardCharsets.UTF_8);
                OutputStream os = s.getOutputStream();
                ByteArrayOutputStream o = new ByteArrayOutputStream();
                o.write(0x81);
                if (p.length < 126) {
                    o.write(p.length);
                } else if (p.length < 65536) {
                    o.write(126);
                    o.write((p.length >> 8) & 0xff);
                    o.write(p.length & 0xff);
                } else {
                    o.write(127);
                    long L = p.length;
                    for (int i = 7; i >= 0; i--) o.write((int) ((L >> (8 * i)) & 0xff));
                }
                o.write(p);
                os.write(o.toByteArray());
                os.flush();
            } catch (Throwable ignored) {
            }
        }
    }
}
