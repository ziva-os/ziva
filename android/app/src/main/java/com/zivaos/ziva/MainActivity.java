package com.zivaos.ziva;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.io.File;
import java.util.List;

/**
 * Thin shell: boots the backend, then loads its web UI in a WebView.
 *
 * The page gets a full `electronAPI` shim (injected on every page start, so
 * it precedes the frontend's module-level mode check): the browser-shell UI
 * takes its "Electron" branch and drives {@link WebTabManager} — one real
 * Chromium WebView per tab, positioned over this page by browserSetArea —
 * instead of the iframe/proxy fallback. Native extras (clipboard, restart,
 * open-external) live on the `ZivaAndroid` bridge and the ⋮ menu.
 */
public class MainActivity extends Activity {
    private WebView webview;
    private android.view.View bootOverlay;
    private TextView bootStatus;
    private WebTabManager webTabs;
    private boolean shimRetried = false;
    private android.webkit.ValueCallback<android.net.Uri[]> fileChooserCb;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (!ZivaController.instance().isExtracted(this)) {
            startActivity(new Intent(this, ExtractActivity.class));
            finish();
            return;
        }
        setContentView(R.layout.activity_main);
        // CDP bridge for chrome-devtools-mcp — before any WebView exists so
        // the debugging switch covers every WebView this process creates.
        DevtoolsBridge.start();
        webview = findViewById(R.id.webview);
        bootOverlay = findViewById(R.id.bootOverlay);
        bootStatus = findViewById(R.id.bootStatus);
        findViewById(R.id.menuButton).setOnClickListener(v -> showMenu());
        webTabs = new WebTabManager(this, findViewById(R.id.webContainer), webview);
        setupWebview();
        requestNotifPermissionIfNeeded();

        Intent svc = new Intent(this, ZivaService.class);
        if (android.os.Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);
        getSharedPreferences("ziva", MODE_PRIVATE).edit().putBoolean("run_on_boot", true).apply();

        bootOverlay.setVisibility(View.VISIBLE);
        bootStatus.setText("正在启动 Ziva 后端…");
        // One dialog per pass: the all-files sheet takes priority; the
        // battery-whitelist ask waits for the next cold start if all-files
        // just showed.
        if (!maybeRequestAllFiles()) maybeRequestBatteryWhitelist();
        waitForBackend(0);
        if (getIntent().getBooleanExtra("open_menu", false)) showMenu();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getBooleanExtra("open_menu", false)) showMenu();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != 1002) {  // composer file chooser
            super.onActivityResult(requestCode, resultCode, data);
            return;
        }
        final android.webkit.ValueCallback<Uri[]> cb = fileChooserCb;
        fileChooserCb = null;
        if (cb == null) {
            // Result arrived but our WebView callback is gone — the
            // activity was recreated (or the page reloaded) mid-pick.
            ZivaController.instance().appendLog(this,
                    "[attach] picker result code=" + resultCode
                            + " DROPPED: no callback (activity recreated mid-pick?)");
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            ZivaController.instance().appendLog(this, "[attach] picker canceled (code=" + resultCode + ")");
            cb.onReceiveValue(null);
            return;
        }
        // Do NOT use FileChooserParams.parseResult(): on HyperOS the picker
        // returns RESULT_OK but parseResult sees no URIs (files=0). Parse
        // both channels ourselves and log the raw shape.
        Uri single = data.getData();
        ClipData clip = data.getClipData();
        ZivaController.instance().appendLog(this,
                "[attach] raw result: data=" + (single == null ? "null" : single.toString())
                        + " clipItems=" + (clip == null ? "0" : String.valueOf(clip.getItemCount())));
        java.util.List<Uri> uris = new java.util.ArrayList<>();
        if (single != null) uris.add(single);
        if (clip != null) {
            for (int i = 0; i < clip.getItemCount(); i++) {
                Uri u = clip.getItemAt(i).getUri();
                if (u != null) uris.add(u);
            }
        }
        if (uris.isEmpty()) {
            ZivaController.instance().appendLog(this,
                    "[attach] picker returned OK but NO uris in data/clipData");
            cb.onReceiveValue(null);
            return;
        }
        // Materialize content:// URIs into our cache as file:// — WebView
        // reads file:// directly, sidestepping every OEM content-URI
        // materialization quirk. Copy off the main thread (files can be big).
        final Uri[] picked = uris.toArray(new Uri[0]);
        new Thread(() -> {
            StringBuilder errs = new StringBuilder();
            Uri[] out = materializeToCache(picked, errs);
            final String errLine = errs.length() == 0 ? null : errs.toString();
            runOnUiThread(() -> {
                ZivaController.instance().appendLog(this,
                        "[attach] delivering " + out.length + " file(s) to webview"
                                + (errLine == null ? "" : " (errors: " + errLine + ")"));
                cb.onReceiveValue(out);
            });
        }, "attach-materialize").start();
    }

    /** Copy content:// URIs into cache/attach/, return file:// URIs. */
    private Uri[] materializeToCache(Uri[] uris, StringBuilder errs) {
        File dir = new File(getCacheDir(), "attach");
        if (!dir.exists()) dir.mkdirs();
        Uri[] out = new Uri[uris.length];
        for (int i = 0; i < uris.length; i++) {
            Uri u = uris[i];
            if ("file".equals(u.getScheme())) { out[i] = u; continue; }
            try {
                String name = queryDisplayName(u);
                if (name == null || name.isEmpty()) name = "attach-" + System.currentTimeMillis() + "-" + i;
                name = name.replaceAll("[/\\\\]", "_");
                File dst = new File(dir, name);
                java.io.InputStream in = getContentResolver().openInputStream(u);
                java.io.OutputStream os = new java.io.FileOutputStream(dst);
                byte[] buf = new byte[65536];
                int r;
                long total = 0;
                while ((r = in.read(buf)) > 0) { os.write(buf, 0, r); total += r; }
                in.close();
                os.close();
                out[i] = Uri.fromFile(dst);
                if (uris.length == 1)
                    ZivaController.instance().appendLog(this,
                            "[attach] materialized " + name + " (" + total + " bytes)");
            } catch (Exception e) {
                errs.append(u.getLastPathSegment()).append(": ").append(e.getMessage());
                out[i] = u; // fall back to the content URI; WebView may still cope
            }
        }
        return out;
    }

    private String queryDisplayName(Uri uri) {
        try (android.database.Cursor c = getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) {
                    String n = c.getString(idx);
                    if (n != null && !n.isEmpty()) return n;
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /**
     * MANAGE_EXTERNAL_STORAGE cannot be requested via the runtime-permission
     * dialog — the system only offers a jump to the settings toggle. Ask
     * once, up front, with the trade-off spelled out (no grant = app-private
     * data dir, wiped on uninstall); the backend boots either way.
     */
    /** @return true when the all-files dialog was shown (so callers can
     *  avoid stacking a second dialog on top of it in the same pass). */
    private boolean maybeRequestAllFiles() {
        if (android.os.Build.VERSION.SDK_INT < 30
                || android.os.Environment.isExternalStorageManager()) return false;
        if (getSharedPreferences("ziva", MODE_PRIVATE).getBoolean("all_files_asked", false)) return false;
        getSharedPreferences("ziva", MODE_PRIVATE).edit().putBoolean("all_files_asked", true).apply();
        new android.app.AlertDialog.Builder(this)
                .setTitle("授权「所有文件访问」")
                .setMessage("开启后，会话数据保存在 /sdcard/Documents/zivadata，卸载重装不丢失。\n\n"
                        + "不开启也能正常使用，数据将保存在应用私有目录（卸载即清除）。\n\n"
                        + "该权限需在系统设置中手动开启。")
                .setPositiveButton("去设置", (d, w) -> {
                    try {
                        startActivity(new Intent(
                                android.provider.Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                android.net.Uri.parse("package:" + getPackageName())));
                    } catch (Exception e) {
                        startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                    }
                })
                .setNegativeButton("暂不", null)
                .show();
        return true;
    }

    /**
     * Battery-optimization exemption: without it HyperOS's background manager
     * freezes/kills the backend whenever the app leaves the foreground — the
     * "switch away and it's gone" half of the process-death problem (the
     * kernel global-OOM half is separate and not addressable without root).
     * Ask once, pref-guarded; users who deny can still grant later via
     * Settings → 省电策略 → 无限制.
     */
    private void maybeRequestBatteryWhitelist() {
        android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        if (getSharedPreferences("ziva", MODE_PRIVATE).getBoolean("battery_whitelist_asked", false)) return;
        getSharedPreferences("ziva", MODE_PRIVATE).edit().putBoolean("battery_whitelist_asked", true).apply();
        new android.app.AlertDialog.Builder(this)
                .setTitle("允许 Ziva 后台常驻？")
                .setMessage("开启「忽略电池优化」后，系统不会在切后台/锁屏时冻结或清理 Ziva 的后端进程——长任务、自动化和会话连接不再因切出应用而中断。\n\n"
                        + "代价是后台耗电略增，建议开启。")
                .setPositiveButton("允许", (d, w) -> {
                    try {
                        @SuppressWarnings("BatteryLife")
                        Intent i = new Intent(
                                android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                android.net.Uri.parse("package:" + getPackageName()));
                        startActivity(i);
                    } catch (Exception ignored) {
                        // Some OEM builds drop the direct-request action; the
                        // manual path (省电策略 → 无限制) still works.
                    }
                })
                .setNegativeButton("暂不", null)
                .show();
    }

    private void setupWebview() {
        // Keep remote debugging available: chrome://inspect lets us look at
        // the frontend when the on-device screen stays black.
        WebView.setWebContentsDebuggingEnabled(true);
        webview.getSettings().setJavaScriptEnabled(true);
        webview.getSettings().setDomStorageEnabled(true);
        webview.getSettings().setAllowFileAccess(false);
        webview.getSettings().setAllowContentAccess(false);
        // Desktop layout on a tablet (see Constants.DESKTOP_UA).
        webview.getSettings().setUserAgentString(Constants.DESKTOP_UA);
        webview.getSettings().setUseWideViewPort(true);
        webview.getSettings().setLoadWithOverviewMode(true);
        // Fixed text zoom: system font scale inflates measurements and
        // diverges from the desktop UI the user compares against.
        webview.getSettings().setTextZoom(100);
        webview.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onConsoleMessage(android.webkit.ConsoleMessage m) {
                if (m.messageLevel() != android.webkit.ConsoleMessage.MessageLevel.DEBUG)
                    ZivaController.instance().appendLog(MainActivity.this,
                            "[web:" + m.messageLevel() + "] " + m.message()
                                    + " @" + m.sourceId() + ":" + m.lineNumber());
                return true;
            }
            // <input type=file> in the composer (+ button): without this
            // override the tap silently does nothing in a WebView.
            //
            // We deliberately do NOT use params.createIntent(): with no
            // accept attribute it emits ACTION_GET_CONTENT, which on HyperOS
            // resolves to the vendor file manager whose content:// URIs the
            // WebView fails to materialize — silently (no JS change event,
            // no error). ACTION_OPEN_DOCUMENT forces the standard SAF
            // DocumentsUI whose URIs WebView handles reliably, and it lets
            // the user pick ANY file type.
            @Override public boolean onShowFileChooser(WebView view,
                    android.webkit.ValueCallback<android.net.Uri[]> callback,
                    WebChromeClient.FileChooserParams params) {
                if (fileChooserCb != null) fileChooserCb.onReceiveValue(null);
                fileChooserCb = callback;
                try {
                    Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                    if ((params.getMode() & WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) != 0)
                        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    // Cloud files materializing inside WebView never finishes;
                    // restrict to locally-available documents.
                    intent.putExtra(Intent.EXTRA_LOCAL_ONLY, true);
                    ZivaController.instance().appendLog(MainActivity.this,
                            "[attach] chooser open (ACTION_OPEN_DOCUMENT */*)");
                    startActivityForResult(intent, 1002);
                    return true;
                } catch (Exception e) {
                    // No SAF resolver on this device — fall back to whatever
                    // the WebView built for us.
                    try {
                        ZivaController.instance().appendLog(MainActivity.this,
                                "[attach] OPEN_DOCUMENT failed (" + e + "), falling back to createIntent");
                        startActivityForResult(params.createIntent(), 1002);
                        return true;
                    } catch (Exception e2) {
                        ZivaController.instance().appendLog(MainActivity.this,
                                "[attach] chooser start FAILED: " + e2);
                        fileChooserCb = null;
                        return false;
                    }
                }
            }
        });
        webview.setWebViewClient(new WebViewClient() {
            // onPageStarted runs before the page's deferred module scripts, so
            // the shim is in place before browser-shell.ts reads electronAPI.
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                injectShim(view);
            }
            // The early injection may be dropped if no JS context exists yet
            // (observed as a black page: HTML loaded, frontend died on the
            // missing electronAPI). Re-inject — the shim is idempotent — and
            // if it is STILL missing, reload once: after reload the injection
            // lands with a live JS context before the frontend boots.
            @Override
            public void onPageFinished(WebView view, String url) {
                injectShim(view);
                ZivaController.instance().appendLog(MainActivity.this, "[web] page finished: " + url);
                // The floating ⋮ overlaps the composer's send area on phones
                // once the real UI is up — get it out of the way. The failure
                // banner below brings it back when it's actually needed.
                findViewById(R.id.menuButton).setVisibility(View.GONE);
                view.evaluateJavascript("(window.electronAPI ? 'ok' : 'missing')", v -> {
                    if ("\"missing\"".equals(v) && !shimRetried) {
                        shimRetried = true;
                        ZivaController.instance().appendLog(MainActivity.this,
                                "[web] electronAPI missing after load — reloading once");
                        view.reload();
                    }
                });
            }
            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest req,
                                        android.webkit.WebResourceError err) {
                if (req.isForMainFrame())
                    ZivaController.instance().appendLog(MainActivity.this,
                            "[web] main-frame error: " + req.getUrl() + " " + err.getDescription());
            }
        });
        webview.addJavascriptInterface(new AndroidBridge(), "ZivaAndroid");
        webview.addJavascriptInterface(webTabs, "ZivaBrowser");
    }

    /** Full electronAPI shim: browser-shell drives WebTabManager via ZivaBrowser. */
    private void injectShim(WebView v) {
        v.evaluateJavascript(SHIM_JS, null);
    }

    private static final String SHIM_JS =
        "(function(){" +
        "  if (window.__zivaShimmed) return; window.__zivaShimmed = true;" +
        "  var br = window.ZivaBrowser, zt = window.ZivaAndroid || {};" +
        "  if (!br) return;" +
        "  var H = {};" +
        "  window.__zivaBrowserDispatch = function(type, payload) {" +
        "    var hs = H[type] || [];" +
        "    for (var i = 0; i < hs.length; i++) { try { hs[i](payload); } catch (e) {} }" +
        "  };" +
        "  function reg(t){ return function(cb){ (H[t] = H[t] || []).push(cb); }; }" +
        "  window.electronAPI = {" +
        "    isElectron: function(){ return Promise.resolve(true); }," +
        "    copyText: function(t){ try { return Promise.resolve(!!zt.copyText(String(t))); } catch(e){ return Promise.resolve(false); } }," +
        "    setTheme: function(){}," +
        "    restartZiva: function(){ try { zt.restartBackend(); } catch(e){} return Promise.resolve(true); }," +
        "    openExternal: function(u){ try { zt.openExternal(String(u)); } catch(e){} return Promise.resolve(true); }," +
        "    onBrowserSelection: function(){}," +
        "    browserListTabs: function(){ try { return Promise.resolve(JSON.parse(br.listTabs() || '[]')); } catch(e){ return Promise.resolve([]); } }," +
        "    browserCreateTab: function(url){ try { return Promise.resolve(br.createTab(url || '')); } catch(e){ return Promise.resolve(''); } }," +
        "    browserShowTab: function(id){ try { br.showTab(String(id)); } catch(e){} }," +
        "    browserHideTabs: function(){ try { br.hideTabs(); } catch(e){} }," +
        "    browserCloseTab: function(id){ try { br.closeTab(String(id)); } catch(e){} }," +
        "    browserNavigate: function(id, u){ try { br.navigate(String(id), String(u)); } catch(e){} }," +
        "    browserNav: function(id, k){ try { br.nav(String(id), String(k)); } catch(e){} }," +
        "    browserSetArea: function(r){ try { br.setArea(r.x|0, r.y|0, r.width|0, r.height|0); } catch(e){} }," +
        "    onBrowserNewTab: reg('newTab')," +
        "    onBrowserTabCreated: reg('tabCreated')," +
        "    onBrowserTabClosed: reg('tabClosed')," +
        "    onBrowserNav: reg('nav')," +
        "    onBrowserTitle: reg('title')" +
        "  };" +
        // Android WebView has no usable mic-capture pipeline for the composer
        // voice input — hide the button (desktop keeps it).
        "  var st = document.createElement('style');" +
        "  st.textContent = '.pane-btn-mic{display:none !important}';" +
        "  (document.body || document.documentElement).appendChild(st);" +
        "})();";

    private void waitForBackend(final int attempt) {
        // REAL wall-clock budget. First boot includes the SYNCHRONOUS
        // chromium download + apt dependency install (several minutes on
        // purpose — chat stays locked out until the box is fully ready),
        // so the budget is generous; the banner still self-recovers when
        // the backend eventually answers.
        final int maxAttempts = 900; // ~11 min of 750ms polls
        new Thread(() -> {
            if (isDestroyed() || isFinishing()) return;
            try { Thread.sleep(750); } catch (InterruptedException e) { return; }
            boolean ok = ZivaController.instance().httpHealthy();
            // Early-exit detection fills lastError within ~2.5s of launch;
            // don't keep polling a backend that already died.
            boolean failedFast = !ok && !ZivaController.instance().isAlive()
                    && !ZivaController.instance().lastError.isEmpty();
            handler.post(() -> {
                if (ok) {
                    bootOverlay.setVisibility(View.GONE);
                    webview.loadUrl("http://127.0.0.1:" + Constants.BACKEND_PORT + "/");
                } else {
                    // Show the banner (once the budget is spent or the process
                    // died fast) but KEEP polling: when the backend eventually
                    // comes up — cold start, watchdog restart, manual restart —
                    // the UI recovers by itself. While the process is alive and
                    // within budget, show live first-boot prep progress
                    // (extraction → chromium download) instead of silence.
                    if (failedFast || attempt >= maxAttempts) {
                        findViewById(R.id.menuButton).setVisibility(View.VISIBLE);
                        bootStatus.setText("后端启动失败 [" + BuildConfig.VERSION_NAME + "]："
                                + ZivaController.instance().lastError
                                + "\n菜单 → 重启后端 可重试");
                    } else {
                        bootOverlay.setVisibility(View.VISIBLE);
                        String prep = "首次准备中…（" + attempt * 3 / 4 + "s）";
                        File dlLog = new File(Constants.publicDataDir(), "chromium-download.log");
                        if (dlLog.exists()) {
                            long n = dlLog.length();
                            if (n > 0) prep = "首次准备中：浏览器组件下载中…（日志 " + n / 1024 + " KB）";
                        }
                        bootStatus.setText(prep);
                    }
                    waitForBackend(attempt + 1);
                }
            });
        }).start();
    }

    /** API 33+: without the grant the foreground-service and bridge
     *  notifications silently never show. */
    private void requestNotifPermissionIfNeeded() {
        if (android.os.Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                        != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1001);
        }
    }

    private void showMenu() {
        String[] items = {getString(R.string.menu_restart_backend),
                getString(R.string.menu_diagnostics), getString(R.string.menu_backup)};
        new android.app.AlertDialog.Builder(this)
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        new AndroidBridge().restartBackend();
                    } else if (which == 1) {
                        new Thread(() -> {
                            List<String> lines = Diagnostics.run(this);
                            runOnUiThread(() -> new android.app.AlertDialog.Builder(this)
                                    .setTitle("自检结果")
                                    .setItems(lines.toArray(new String[0]), null)
                                    .setPositiveButton("好", null)
                                    .show());
                        }).start();
                    } else {
                        new Thread(() -> {
                            try {
                                File f = BackupManager.backupToDownload();
                                runOnUiThread(() -> Toast.makeText(this,
                                        "已备份到 " + f.getAbsolutePath(), Toast.LENGTH_LONG).show());
                            } catch (Exception e) {
                                runOnUiThread(() -> Toast.makeText(this,
                                        "备份失败：" + e.getMessage(), Toast.LENGTH_LONG).show());
                            }
                        }).start();
                    }
                })
                .show();
    }

    @Override
    public void onBackPressed() {
        // Active web tab consumes back for its own history first.
        if (webTabs != null && webTabs.onBackPressed()) return;
        if (webview != null && webview.canGoBack()) webview.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
    }

    /** Bridge surface for the ZivaAndroid JS bridge. All methods fire-and-forget or return primitives. */
    public class AndroidBridge {
        @JavascriptInterface
        public boolean copyText(String text) {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                cm.setPrimaryClip(ClipData.newPlainText("ziva", text));
                return true;
            } catch (Exception e) { return false; }
        }

        @JavascriptInterface
        public void restartBackend() {
            // Reset the boot UI and RE-ENTER the wait loop: without this the
            // failure banner stays forever even after a successful restart
            // (backend healthy per Diagnostics, but the webview never loads).
            runOnUiThread(() -> {
                bootOverlay.setVisibility(View.VISIBLE);
                bootStatus.setText("正在重启 Ziva 后端…");
            });
            new Thread(() -> {
                ZivaController.instance().stopBackend();
                ZivaController.instance().startBackend(MainActivity.this);
                waitForBackend(0);
            }).start();
            runOnUiThread(() -> Toast.makeText(MainActivity.this, "重启后端…", Toast.LENGTH_SHORT).show());
        }

        @JavascriptInterface
        public void openExternal(String url) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } catch (Exception ignored) {}
        }

        @JavascriptInterface
        public void toast(String text) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, text, Toast.LENGTH_SHORT).show());
        }
    }
}
