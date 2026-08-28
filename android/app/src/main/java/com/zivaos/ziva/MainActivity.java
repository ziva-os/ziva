package com.zivaos.ziva;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
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
 * Deliberately NOT impersonating Electron: the frontend checks
 * `window.electronAPI` to pick its shell mode, and shimming it would route
 * browser tabs down the WebContentsView path (undefined on Android → blank
 * tabs). With no shim the frontend runs its web mode — the mobile-adapted
 * chat UI, browser tabs as iframe + /api/proxy, clipboard via the
 * execCommand fallback. Native abilities live on the `ZivaAndroid` bridge
 * (see AndroidBridge) and the ⋮ menu.
 */
public class MainActivity extends Activity {
    private WebView webview;
    private TextView bootStatus;
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
