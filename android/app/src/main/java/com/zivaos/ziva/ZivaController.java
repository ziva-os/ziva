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
            // Adopted backends still deserve current helper scripts: the
            // patcher/ensure-chromium shims are idempotent one-file writes,
            // and skipping them here kept a STALE ensure-chromium.sh (and a
            // stale config patcher) alive until the next real spawn.
            File rootfs = Constants.rootfsDir(ctx);
            installMcpEntry(rootfs);
            installMcpConfigPatcher(rootfs);
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
            // Self-identifying build stamp: several rounds shipped while the
            // user unknowingly kept an older APK installed, and every log
            // export looked identical. Every start now states its version —
            // a log without this line, or with an old sha, is an old app.
            appendProcLog(logFileForTail, "[proc] backend starting, build="
                    + BuildConfig.VERSION_NAME
                    // Hitch the bridge state onto the one log line proven to
                    // always reach the export: appendProcLog into /sdcard has
                    // silently failed before (catch-ignored), which is how
                    // three rounds of "zero [cdp] lines" happened.
                    + "; cdp=" + DevtoolsBridge.status());
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
        installMcpEntry(rootfs);
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
            + "import shlex\n"
            + "import sys\n"
            + "import os\n"
            + "import glob\n"
            + "import yaml\n"
            + "\n"
            + "P = \"/root/.ziva/config.yaml\"\n"
            + "try:\n"
            + "    with open(P) as f:\n"
            + "        cfg = yaml.safe_load(f) or {}\n"
            + "except FileNotFoundError:\n"
            + "    print(\"[patch] no config.yaml yet; nothing to do\")\n"
            + "    sys.exit(0)\n"
            + "except Exception as e:\n"
            // A silent except here is how the r25 device kept its legacy
            // 9222 entry with nobody noticing — always say what happened.
            + "    print(\"[patch] config unreadable:\", e)\n"
            + "    sys.exit(0)\n"
            + "\n"
            + "def tokens(srv):\n"
            // Effective argv: command may itself be a list or a shlex string
            // (the runtime's _mcp_server_from_mapping accepts all three), so
            // a 9222 hiding inside a string command must not slip past.
            + "    cmd = srv.get(\"command\")\n"
            + "    out = []\n"
            + "    if isinstance(cmd, list):\n"
            + "        out += [str(c) for c in cmd]\n"
            + "    elif isinstance(cmd, str):\n"
            + "        try:\n"
            + "            out += shlex.split(cmd)\n"
            + "        except ValueError:\n"
            + "            out += cmd.split()\n"
            + "    raw = srv.get(\"args\") or []\n"
            + "    if isinstance(raw, str):\n"
            + "        try:\n"
            + "            raw = shlex.split(raw)\n"
            + "        except ValueError:\n"
            + "            raw = raw.split()\n"
            + "    out += [str(a) for a in raw]\n"
            + "    out.append(str(srv.get(\"url\") or srv.get(\"server_url\") or \"\"))\n"
            + "    return out\n"
            + "\n"
            + "changed = False\n"
            + "matched = 0\n"
            + "\n"
            // Chrome entries can hide in any shape the runtime accepts:
            // mcp.servers (list OR {name: {...}} dict) and top-level
            // mcpServers / mcp_servers (claude-style dict). Scanning only
            // mcp.servers-as-list is how a dict-form 9222 entry survived
            // every patch round.
            + "candidates = []\n"
            + "mcp = cfg.get(\"mcp\")\n"
            + "if isinstance(mcp, dict):\n"
            + "    servers = mcp.get(\"servers\")\n"
            + "    if isinstance(servers, list):\n"
            + "        candidates += [s for s in servers if isinstance(s, dict)]\n"
            + "    elif isinstance(servers, dict):\n"
            + "        candidates += [s for s in servers.values() if isinstance(s, dict)]\n"
            + "for key in (\"mcpServers\", \"mcp_servers\"):\n"
            + "    d = cfg.get(key)\n"
            + "    if isinstance(d, dict):\n"
            + "        candidates += [s for s in d.values() if isinstance(s, dict)]\n"
            + "\n"
            + "for srv in candidates:\n"
            + "    joined = \" \".join(tokens(srv))\n"
            // On this device any --browser-url / 127.0.0.1:9222 entry is the
            // legacy Mac bridge — there is no local GUI chrome to bridge to.
            + "    if (\"chrome-devtools-mcp\" not in joined\n"
            + "            and \"--browser-url\" not in joined\n"
            + "            and \"127.0.0.1:9222\" not in joined\n"
            + "            and \"/opt/ensure-chromium.sh\" not in joined):\n"
            + "        continue\n"
            + "    matched += 1\n"
            + "    if srv.get(\"command\") in (\"/bin/sh\", [\"/bin/sh\", \"/opt/ensure-chromium.sh\"]):\n"
            // On-device shape — but a stale args list must NOT survive: the
            // script execs the mcp with "$@", so a leftover
            // --browser-url=127.0.0.1:9222 from an older config silently
            // made the mcp attach to a bridge that did not exist
            // ("Failed to fetch browser WebSocket URL") instead of ever
            // launching chromium. Normalize to the unambiguous list form in
            // the same pass: a string command "/bin/sh" with args=[script,
            // ...] must not merely lose its args (that would exec a naked
            // interactive /bin/sh).
            + "        stale = srv.pop(\"args\", None)\n"
            + "        if srv.get(\"command\") != [\"/bin/sh\", \"/opt/ensure-chromium.sh\"] or stale is not None:\n"
            + "            srv[\"command\"] = [\"/bin/sh\", \"/opt/ensure-chromium.sh\"]\n"
            + "            srv[\"enabled\"] = True\n"
            + "            changed = True\n"
            + "            print(\"[patch] normalized on-device chrome entry\")\n"
            + "        continue\n"
            + "    for k in (\"env\", \"environment\", \"url\", \"server_url\", \"transport\", \"type\", \"disabled\"):\n"
            + "        srv.pop(k, None)\n"
            // List form, not string+args: a string command used to make the
            // runtime parser IGNORE the args key entirely (silent no-arg
            // launch). The parser now merges them, but the list form is
            // unambiguous on every parser version.
            + "    srv[\"command\"] = [\"/bin/sh\", \"/opt/ensure-chromium.sh\"]\n"
            + "    srv.pop(\"args\", None)\n"
            + "    srv[\"enabled\"] = True\n"
            + "    changed = True\n"
            + "\n"
            // minimax-coding-plan-mcp: two problems. (1) Legacy entries pinned
            // the SDK with --with "mcp[cli]<2.0" — since 0.0.5 the package
            // itself requires mcp>=2.0.0,<3, so the pin resolves to an
            // ancient version or fails. (2) EVERY released version prints
            // "Starting Minimax MCP server" to STDOUT, which strict JSON-RPC
            // clients (mcp 2.x) reject outright — connect dies with
            // 'Invalid JSON'. Route stdout through a line filter; stderr to
            // a log. Environment keys are preserved.
            + "for srv in candidates:\n"
            + "    joined = \" \".join(tokens(srv))\n"
            + "    if \"minimax-coding-plan-mcp\" not in joined:\n"
            + "        continue\n"
            // The baked tool env ships in the SAME APK as this patcher and its
            // stdout banner is deleted at rootfs build time (source fix) — no
            // fallback needed. Discovery is dynamic: proot -R binds the host's
            // /home into the build chroot, so a build-time HOME leak can land
            // the env under /home/<user> instead of /root. An older shim/uvx
            // form still in the config is rewritten (upgraded) to the direct
            // entrypoint.
            + "    hits = [\"/root/.local/share/uv/tools/minimax-coding-plan-mcp/bin/minimax-coding-plan-mcp\"]\n"
            + "    hits += sorted(glob.glob(\"/home/*/.local/share/uv/tools/minimax-coding-plan-mcp/bin/minimax-coding-plan-mcp\"))\n"
            + "    hits = [p for p in hits if os.path.isfile(p)]\n"
            + "    if not hits:\n"
            + "        print(\"[patch] minimax baked entry missing in rootfs; entry left unchanged\")\n"
            + "        continue\n"
            + "    tgt = hits[0]\n"
            + "    if srv.get(\"command\") == [tgt]:\n"
            + "        continue  # already on-device\n"
            + "    srv[\"command\"] = [tgt]\n"
            + "    srv.pop(\"args\", None)\n"
            + "    changed = True\n"
            + "    print(\"[patch] updated minimax-coding-plan-mcp startup (baked, banner-stripped)\")\n"
            + "\n"
            + "if matched == 0:\n"
            // No chrome entry anywhere: add the on-device one so browser
            // tools exist out of the box. Put it where the user's other
            // servers live — adding mcp.servers would SHADOW a populated
            // mcpServers dict (parse_mcp_config only falls back when
            // mcp.servers is empty).
            + "    entry = {\"command\": [\"/bin/sh\", \"/opt/ensure-chromium.sh\"]}\n"
            + "    if isinstance(cfg.get(\"mcpServers\"), dict):\n"
            + "        cfg[\"mcpServers\"][\"chrome-devtools\"] = entry\n"
            + "    elif isinstance(cfg.get(\"mcp_servers\"), dict):\n"
            + "        cfg[\"mcp_servers\"][\"chrome-devtools\"] = entry\n"
            + "    else:\n"
            + "        m = cfg.setdefault(\"mcp\", {})\n"
            + "        if not isinstance(m.get(\"servers\"), list):\n"
            + "            m[\"servers\"] = []\n"
            + "        m[\"servers\"].append(dict(entry, name=\"chrome-devtools\"))\n"
            + "    print(\"[patch] added on-device chrome-devtools server entry\")\n"
            + "    changed = True\n"
            + "\n"
            + "if changed:\n"
            + "    try:\n"
            + "        with open(P, \"w\") as f:\n"
            + "            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)\n"
            + "        print(\"[patch] config saved\")\n"
            + "    except Exception as e:\n"
            + "        print(\"[patch] rewrite failed:\", e)\n"
            + "else:\n"
            + "    print(\"[patch] ok: chrome server already on-device\")\n");
    }

    /**
     * The user's MCP config points chrome-devtools at /opt/ensure-chromium.sh
     * (the patcher rewrites every chrome-devtools-mcp shape to this path).
     * DUAL MODE: prefer the app's DevtoolsBridge on 127.0.0.1:9222 (agent
     * drives the USER'S actual tabs — desktop parity), but when the bridge
     * is not answering fall back to a LOCAL HEADLESS Chromium downloaded on
     * first use. The fallback is the pre-VNC path that was verified on real
     * devices; on the r39 test device the bridge silently never came up
     * (no [cdp] line at all) while the device OOM-killed the backend every
     * 60s — a fallback keeps browser tools usable regardless.
     */
    private static void installMcpEntry(File rootfs) {
        writeGuestFile(new File(rootfs, "opt/ensure-chromium.sh"),
            "#!/bin/sh\n"
            + "TAG=\"[ensure]\"\n"
            + "MCP=/usr/local/bin/chrome-devtools-mcp\n"
            + "LOGF=/root/.ziva/chrome-mcp.log\n"
            // Mode ledger: the backend's STDERR never reaches the Java log
            // (two rounds of blind debugging proved it), so every mode
            // decision also lands in this file and the NEXT backend start
            // replays it over the one pipe that is guaranteed to reach the
            // exported log: this script's --download-only stdout.
            + "MODELOG=/root/.ziva/ensure-mode.log\n"
            + "CHROME=/opt/chromium/chrome\n"
            + "CHROME_BIN=/opt/chromium/chrome-bin\n"
            + "LOCK=/opt/chromium/.downloading\n"
            + "mkdir -p /opt/chromium /root/.cache/ms-playwright\n"
            + "\n"
            + "fixup_wrapper() {\n"
            + "  if [ -L \"$CHROME\" ]; then\n"
            + "    T=$(readlink -f \"$CHROME\" 2>/dev/null)\n"
            + "    if [ -n \"$T\" ] && [ -x \"$T\" ]; then ln -sf \"$T\" \"$CHROME_BIN\"; fi\n"
            + "    rm -f \"$CHROME\"\n"
            + "  fi\n"
            + "  if [ ! -x \"$CHROME\" ] && [ -x \"$CHROME_BIN\" ]; then\n"
            + "    printf '#!/bin/sh\\nexec /opt/chromium/chrome-bin --no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu \"$@\"\\n' > \"$CHROME\"\n"
            + "    chmod +x \"$CHROME\"\n"
            + "  fi\n"
            + "  if [ ! -x \"$CHROME_BIN\" ]; then\n"
            + "    T=$(ls /opt/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -n 1)\n"
            + "    if [ -n \"$T\" ]; then ln -sf \"$T\" \"$CHROME_BIN\"; fi\n"
            + "  fi\n"
            + "}\n"
            + "\n"
            + "download() {\n"
            + "  export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright\n"
            + "  export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright\n"
            + "  export DEBIAN_FRONTEND=noninteractive\n"
            + "  npx -y playwright@1.49.1 install --with-deps chromium --no-shell\n"
            + "  SRC=$(ls -d /root/.cache/ms-playwright/chromium-*/chrome-linux 2>/dev/null | head -n 1)\n"
            + "  if [ -n \"$SRC\" ] && [ -x \"$SRC/chrome\" ]; then\n"
            + "    ln -sf \"$SRC/chrome\" \"$CHROME_BIN\"\n"
            + "    rm -f \"$CHROME\"\n"
            + "    fixup_wrapper\n"
            + "    echo \"$TAG chromium installed at $CHROME\"\n"
            + "  else\n"
            + "    echo \"$TAG chromium download FAILED (see npx output above)\"\n"
            + "  fi\n"
            + "  rm -f \"$LOCK\"\n"
            + "}\n"
            + "\n"
            // Shared probe: 3 x (2s curl + 1s gap). Retries because prewarm
            // can race the bridge by milliseconds; --noproxy because a
            // stray http_proxy env must never redirect a loopback check.
            + "probe_bridge() {\n"
            + "  BRIDGE=0\n"
            + "  PROBE_ERR=\"\"\n"
            + "  for probe in 1 2 3; do\n"
                       + "    OUT=$(curl -s -m 4 --noproxy '*' http://127.0.0.1:9222/json/version 2>&1)\n"
            + "    RC=$?\n"
            + "    if [ $RC -eq 0 ]; then BRIDGE=1; break; fi\n"
            + "    PROBE_ERR=\"rc=$RC ${OUT:-no output}\"\n"
            + "    [ \"$probe\" -lt 3 ] && sleep 1\n"
            + "  done\n"
            + "  [ \"$BRIDGE\" = \"1\" ] || { echo \"$(date +%s) probe-fail $PROBE_ERR\" >> \"$MODELOG\"; grep -o '[a-z_]*devtools_remote[a-zA-Z0-9_]*' /proc/net/unix 2>/dev/null | sort -u | head -5 | sed 's/^/socket-seen: /' >> \"$MODELOG\"; }\n"
            + "  [ \"$BRIDGE\" = \"1\" ]\n"
            + "}\n"
            + "\n"
            + "fresh_or_running() {\n"
            + "  if [ -f \"$LOCK\" ]; then\n"
            + "    pid=$(cut -d' ' -f1 \"$LOCK\" 2>/dev/null)\n"
            + "    if [ -n \"$pid\" ]; then\n"
            + "      if kill -0 \"$pid\" 2>/dev/null; then return 0; fi\n"
            + "      rm -f \"$LOCK\"\n"
            + "      return 1\n"
            + "    fi\n"
            + "    started=$(cut -d' ' -f2 \"$LOCK\" 2>/dev/null)\n"
            + "    now=$(date +%s)\n"
            + "    if [ -n \"$started\" ] && [ $((now - started)) -lt 120 ]; then return 0; fi\n"
            + "    rm -f \"$LOCK\"\n"
            + "  fi\n"
            + "  return 1\n"
            + "}\n"
            + "\n"
            // Pre-download directive: must NEVER fall through into the mcp
            // exec (the launcher argv would then reach mcp as unknown args).
            + "if [ \"$1\" = \"--download-only\" ]; then\n"
            + "  if [ -f \"$MODELOG\" ]; then echo \"$TAG last mcp mode: $(tail -n 3 \"$MODELOG\" | tr '\\n' '|')\"; fi\n"
            // Reap orphaned mcp node processes left by killed backends: each
            // one holds ~200MB, and on this device even ONE orphan tips
            // memory into the OOM regime that wedges everything else. Safe
            // timing: at --download-only the backend has not started, so no
            // legitimate mcp can be running yet.
            + "  if pkill -f chrome-devtools-mcp 2>/dev/null; then echo \"$TAG reaped orphaned mcp processes\"; fi\n"
            // Direct connect drives the user's REAL WebView tabs — a local
            // chromium is dead weight there. If the bridge answers, skip the
            // 250MB download entirely (this runs on every backend start via
            // ProotBootstrap, so it must stay cheap in direct mode).
            + "  if probe_bridge; then\n"
            + "    echo \"$(date +%s) direct (download skipped)\" >> \"$MODELOG\"\n"
            + "    echo \"$TAG cdp bridge up — chromium not needed in direct mode\"\n"
            + "    exit 0\n"
            + "  fi\n"
            + "  if [ -x \"$CHROME\" ]; then echo \"$TAG chromium already present\"; exit 0; fi\n"
            + "  if fresh_or_running; then echo \"$TAG download already running/fresh\"; exit 0; fi\n"
            // Background the download: this script runs BEFORE the backend
            // can start (ProotBootstrap), and a synchronous 250MB fetch
            // here blocks waitForBackend into "backend failed" — worse,
            // each OOM kill of the backend restarted the download from
            // zero. The lock now names the background subshell so
            // fresh_or_running can see it across restarts.
            + "  echo \"$TAG chromium download started in background — starting backend now\"\n"
            + "  (\n"
            + "    trap 'rm -f \"$LOCK\"' EXIT\n"
            + "    download\n"
            + "  ) >/opt/chromium/download.log 2>&1 &\n"
            + "  echo \"$! $(date +%s)\" > \"$LOCK\"\n"
            + "  exit 0\n"
            + "fi\n"
            + "\n"
            // Mode 1 — the CDP bridge: drive the user's real tabs. Probed
            // with short retries: the bridge comes up in ZivaApp.onCreate
            // but prewarm/mcp can race it by milliseconds, and one failed
            // probe used to pin the session to headless forever. The
            // decision is appended to MODELOG so the next backend start
            // replays it into the exported log. curl has a hard 2s cap per
            // probe so a half-dead bridge falls through instead of hanging
            // the MCP handshake. Diagnostics go to STDERR: stdout IS the
            // MCP stdio pipe.
            + "BRIDGE=0\n"
            + "if probe_bridge; then\n"
            + "  echo \"$(date +%s) direct\" >> \"$MODELOG\"\n"
            + "  echo \"$TAG cdp bridge up on 9222 — direct connect\" >&2\n"
            + "  exec $MCP --browser-url http://127.0.0.1:9222 --logFile $LOGF \"$@\"\n"
            + "fi\n"
            + "echo \"$(date +%s) headless (bridge not answering after 3 probes)\" >> \"$MODELOG\"\n"
            + "echo \"$TAG bridge not answering — local headless chromium\" >&2\n"
            + "\n"
            + "fixup_wrapper\n"
            + "if [ ! -x \"$CHROME\" ]; then\n"
            + "  if fresh_or_running; then exit 1; fi\n"
            // Background download: the lock must name the SUBSHELL's pid
            // ($!); \"$$\" here is the parent, which exits immediately and
            // would make a live download look stale.
            + "  download >/opt/chromium/download.log 2>&1 &\n"
            + "  echo \"$! $(date +%s)\" > \"$LOCK\"\n"
            + "  exit 1\n"
            + "fi\n"
            // NOTE: this script's stdout IS the MCP stdio pipe — never echo
            // diagnostics here; --logFile lands them in the public data dir.
            + "exec $MCP --executablePath \"$CHROME\" --headless --logFile $LOGF \"$@\"\n");
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
    static void appendProcLog(File logFile, String line) {
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
