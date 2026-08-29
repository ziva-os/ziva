package com.zivaos.ziva;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Map;

/**
 * Android implementation of the desktop "browser shell" backend: each web tab
 * is a real Chromium WebView stacked inside {@code webContainer} (positioned
 * over the Ziva WebView by the renderer's browserSetArea rect, exactly like
 * the main-process WebContentsViews on Electron).
 *
 * Exposed to the page as the {@code ZivaBrowser} JS bridge; MainActivity
 * injects an electronAPI shim that wraps these calls in the API surface
 * web/src/browser-shell.ts already speaks. Web tabs target real websites —
 * not the /api/proxy iframe fallback.
 *
 * Threading: @JavascriptInterface calls arrive on a binder thread, so every
 * WebView touch is posted to the UI thread; state the renderer reads back
 * (createTab id, listTabs JSON) comes from shadow copies maintained by the
 * client callbacks (which DO run on the UI thread).
 */
public class WebTabManager {
    private final Activity activity;
    private final FrameLayout container;   // webContainer overlay
    private final WebView zivaView;        // for dispatching events into the page

    private final Map<String, WebView> tabs = new java.util.concurrent.ConcurrentHashMap<>();
    private final Map<String, String> urls = new java.util.concurrent.ConcurrentHashMap<>();
    private final Map<String, String> titles = new java.util.concurrent.ConcurrentHashMap<>();
    private String activeId;
    private int seq = 0;

    WebTabManager(Activity activity, FrameLayout container, WebView zivaView) {
        this.activity = activity;
        this.container = container;
        this.zivaView = zivaView;
    }

    // ---------------------------------------------------------------- JS API

    /** Create a tab (and show it, matching Electron's browserCreateTab). Returns the id synchronously. */
    @JavascriptInterface
    public synchronized String createTab(String url) {
        final String id = "wt" + (++seq);
        urls.put(id, url == null ? "" : url);
        titles.put(id, "");
        activeId = id;
        // buildWebView() MUST run on the UI thread — `new WebView()` on the
        // JS-bridge thread (no Looper) throws and crashes the whole app
        // (this was the "+" button crash). The id is returned synchronously;
        // the WebView reference lands in `tabs` a moment later.
        ui(() -> {
            final WebView w = buildWebView(id);
            tabs.put(id, w);
            container.addView(w, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            w.loadUrl(meaningful(url) ? url : "about:blank");
            showOnly(id);
        });
        return id;
    }

    @JavascriptInterface
    public synchronized void showTab(String id) {
        if (!tabs.containsKey(id)) return;
        activeId = id;
        ui(() -> { showContainer(); showOnly(id); });
    }

    /** Back on the Ziva tab: fold the whole overlay away, tabs stay alive. */
    @JavascriptInterface
    public synchronized void hideTabs() {
        activeId = null;
        ui(() -> container.setVisibility(View.GONE));
    }

    @JavascriptInterface
    public synchronized void closeTab(String id) {
        final WebView w = tabs.remove(id);
        if (w == null) return;
        urls.remove(id);
        titles.remove(id);
        if (id.equals(activeId)) activeId = null;
        ui(() -> {
            container.removeView(w);
            w.stopLoading();
            w.destroy();
            if (container.getChildCount() == 0) container.setVisibility(View.GONE);
        });
    }

    @JavascriptInterface
    public synchronized void navigate(String id, String url) {
        final WebView w = tabs.get(id);
        if (w == null || url == null || url.isEmpty()) return;
        urls.put(id, url);
        ui(() -> w.loadUrl(url));
    }

    @JavascriptInterface
    public synchronized void nav(String id, String kind) {
        final WebView w = tabs.get(id);
        if (w == null) return;
        ui(() -> {
            switch (kind == null ? "" : kind) {
                case "back": if (w.canGoBack()) w.goBack(); break;
                case "forward": if (w.canGoForward()) w.goForward(); break;
                case "reload": w.reload(); break;
            }
        });
    }

    /**
     * Position the overlay over the renderer's #browserWebArea rect. The
     * renderer reports CSS px; FrameLayout.LayoutParams/margins are PHYSICAL
     * pixels — multiply by density or the overlay lands at 1/dpr of the
     * screen (the "picture-in-picture" browser on tablets).
     */
    @JavascriptInterface
    public synchronized void setArea(int x, int y, int width, int height) {
        if (width <= 0 || height <= 0) return;
        float d = activity.getResources().getDisplayMetrics().density;
        final int px = Math.round(width * d), py = Math.round(height * d);
        final int pxx = Math.round(x * d), pyy = Math.round(y * d);
        ui(() -> {
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(px, py);
            lp.leftMargin = pxx;
            lp.topMargin = pyy;
            container.setLayoutParams(lp);
            container.setVisibility(View.VISIBLE);
        });
    }

    /** Renderer reload recovery: the live tab list as JSON (shadow state only). */
    @JavascriptInterface
    public synchronized String listTabs() {
        try {
            JSONArray arr = new JSONArray();
            for (Map.Entry<String, String> e : urls.entrySet()) {
                if ("".equals(e.getValue()) && "".equals(titles.get(e.getKey()))) continue;
                JSONObject o = new JSONObject();
                o.put("id", e.getKey());
                o.put("url", e.getValue());
                o.put("title", titles.get(e.getKey()));
                o.put("active", e.getKey().equals(activeId));
                arr.put(o);
            }
            return arr.toString();
        } catch (Exception e) {
            return "[]";
        }
    }

    // ------------------------------------------------------------- internals

    /** true if url is a real navigation target (not blank). */
    private static boolean meaningful(String url) {
        return url != null && !url.isEmpty() && !"about:blank".equals(url);
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private WebView buildWebView(final String id) {
        WebView w = new WebView(activity);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false); // target=_blank navigates in-tab
        // Desktop layout on a tablet: stock mobile UA + default viewport get
        // phone-layout pages rendered at ~1/3 width with dead whitespace.
        s.setUserAgentString(Constants.DESKTOP_UA);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        // Ignore the system font scale: it inflates text measurement inside
        // the page (chips/toolbar layouts go stale) and diverges from the
        // desktop rendering users compare against.
        s.setTextZoom(100);
        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                if (meaningful(url)) {
                    urls.put(id, url);
                    emit("nav", id, url);
                }
            }
        });
        w.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onReceivedTitle(WebView view, String title) {
                if (title != null && !title.isEmpty()) {
                    titles.put(id, title);
                    emit("title", id, title);
                }
            }
        });
        return w;
    }

    private void showOnly(String id) {
        for (Map.Entry<String, WebView> e : tabs.entrySet()) {
            e.getValue().setVisibility(e.getKey().equals(id) ? View.VISIBLE : View.GONE);
        }
    }

    private void showContainer() {
        container.setVisibility(View.VISIBLE);
    }

    private void ui(Runnable r) {
        activity.runOnUiThread(r);
    }

    /** Push a main→renderer event into the Ziva page (browser-shell shim dispatches to callbacks). */
    private void emit(String type, String id, String value) {
        ui(() -> {
            try {
                JSONObject payload = new JSONObject();
                payload.put("id", id);
                payload.put("nav".equals(type) ? "url" : "title", value);
                zivaView.evaluateJavascript(
                        "window.__zivaBrowserDispatch&&window.__zivaBrowserDispatch("
                                + JSONObject.quote(type) + "," + payload + ")", null);
            } catch (Exception ignored) {
            }
        });
    }

    /**
     * Back button: an active web tab always consumes back — history-back when
     * possible, no-op otherwise (never falls through to the Ziva page).
     */
    synchronized boolean onBackPressed() {
        if (activeId == null) return false;
        final WebView w = tabs.get(activeId);
        if (w == null) return false;
        ui(() -> { if (w.canGoBack()) w.goBack(); });
        return true;
    }
}
