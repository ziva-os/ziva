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
import android.widget.TextView;
import android.widget.Toast;
import java.io.File;
import java.util.List;

/**
 * Thin shell: boots the backend, then loads its web UI in a WebView.
 *
 * An `electronAPI` compatibility shim is injected so the existing web
 * frontend gets native clipboard/restart/theme without a single frontend
 * change; every Electron API we do not shim stays `undefined` and the
 * frontend's optional-call fallbacks handle it (attachments fall back to
 * HTTP upload, the browser tab falls back to the iframe proxy).
 */
public class MainActivity extends Activity {
    private WebView webview;
    private TextView bootStatus;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean injected = false;

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
        setupWebview();

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
            @Override public void onPageFinished(WebView v, String url) { injectShim(v); }
        });
        webview.addJavascriptInterface(new AndroidBridge(), "ZivaAndroid");
    }

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

    /** Inject the electronAPI shim once per page load (after the page's own scripts may run). */
    private void injectShim(WebView v) {
        if (injected) return;
        injected = true;
        String shim =
            "(function(){" +
            "  if (window.__zivaShimmed) return; window.__zivaShimmed = true;" +
            "  var br = window.ZivaAndroid || {};" +
            "  window.electronAPI = {" +
            "    isElectron: function(){ return Promise.resolve(true); }," +
            "    copyText: function(t){ return Promise.resolve(br.copyText(String(t))); }," +
            "    setTheme: function(){}, " +
            "    restartZiva: function(){ br.restartBackend(); return Promise.resolve(true); }," +
            "    openExternal: function(u){ br.openExternal(String(u)); return Promise.resolve(true); }" +
            "  };" +
            "})();";
        v.evaluateJavascript(shim, null);
    }

    private void showMenu() {
        String[] items = {"重启后端", "运行自检", "备份数据"};
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
        if (webview != null && webview.canGoBack()) webview.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        injected = false;
        super.onDestroy();
    }

    /** Bridge surface for the injected shim. All methods fire-and-forget or return primitives. */
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
