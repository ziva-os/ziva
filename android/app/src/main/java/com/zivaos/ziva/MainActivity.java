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
    private TextView bootStatus;
    private WebTabManager webTabs;
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
        webview = findViewById(R.id.webview);
        bootStatus = findViewById(R.id.bootStatus);
        findViewById(R.id.menuButton).setOnClickListener(v -> showMenu());
        webTabs = new WebTabManager(this, findViewById(R.id.webContainer), webview);
        setupWebview();
        requestNotifPermissionIfNeeded();

        Intent svc = new Intent(this, ZivaService.class);
        if (android.os.Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);
        getSharedPreferences("ziva", MODE_PRIVATE).edit().putBoolean("run_on_boot", true).apply();

        bootStatus.setVisibility(View.VISIBLE);
        bootStatus.setText("正在启动 Ziva 后端…");
        waitForBackend(0);
    }

    private void setupWebview() {
        WebView.setWebContentsDebuggingEnabled(false);
        webview.getSettings().setJavaScriptEnabled(true);
        webview.getSettings().setDomStorageEnabled(true);
        webview.getSettings().setAllowFileAccess(false);
        webview.getSettings().setAllowContentAccess(false);
        webview.setWebChromeClient(new WebChromeClient());
        webview.setWebViewClient(new WebViewClient() {
            // onPageStarted runs before the page's deferred module scripts, so
            // the shim is in place before browser-shell.ts reads electronAPI.
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                injectShim(view);
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
        "})();";

    private void waitForBackend(final int attempt) {
        final int maxAttempts = 60; // 60 × 500ms = 30s
        new Thread(() -> {
            boolean ok = ZivaController.instance().httpHealthy();
            handler.post(() -> {
                if (ok) {
                    bootStatus.setVisibility(View.GONE);
                    webview.loadUrl("http://127.0.0.1:" + Constants.BACKEND_PORT + "/");
                } else if (attempt < maxAttempts) {
                    waitForBackend(attempt + 1);
                } else {
                    bootStatus.setText("后端启动失败：" + ZivaController.instance().lastError
                            + "\n菜单 → 重启后端 可重试");
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
            new Thread(() -> {
                ZivaController.instance().stopBackend();
                ZivaController.instance().startBackend(MainActivity.this);
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
