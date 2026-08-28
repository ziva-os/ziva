package com.zivaos.ziva;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.webkit.WebView;
import androidx.core.app.ActivityCompat;
import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * Health checks that name what's broken, with auto-repair where safe.
 * Triggered from the app menu; returns plain-language lines.
 */
public final class Diagnostics {
    private Diagnostics() {}

    public static List<String> run(Activity activity) {
        List<String> out = new ArrayList<>();
        ZivaController ctl = ZivaController.instance();

        // 1. Storage permission for the public data dir.
        boolean allFiles = Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager();
        out.add((allFiles ? "✓" : "✗") + " 「所有文件访问」权限" + (allFiles ? "" : "（缺失 → 会话数据只能留在应用私有目录，卸载即丢。设置 → 应用 → Ziva → 权限）"));
        if (!allFiles) requestAllFiles(activity);

        // 2. Public data dir writable.
        File data = Constants.publicDataDir();
        boolean dirOk = ProotBootstrap.dataDirCanWrite(data);
        out.add((dirOk ? "✓" : "✗") + " 数据目录可写: " + data.getAbsolutePath());

        // 3. proot + rootfs integrity.
        List<String> probe = ProotBootstrap.probe(activity);
        if (probe.isEmpty()) out.add("✓ proot 与 rootfs 完整");
        else for (String issue : probe) out.add("✗ " + issue);

        // 4. Backend process + HTTP surface.
        out.add((ctl.isAlive() ? "✓" : "✗") + " 后端进程" + (ctl.isAlive() ? "运行中 (pid 存活)" : "未运行"));
        out.add((ctl.httpHealthy() ? "✓" : "✗") + " HTTP :4097 /status");

        // 5. Bridge.
        out.add((ZivaApp.bridge().isRunning() ? "✓" : "✗") + " 设备桥 :3090");

        // 6. Log file reachable.
        out.add((ZivaController.logFile().exists() ? "✓" : "ℹ") + " 日志文件 " + ZivaController.logFile().getAbsolutePath());
        return out;
    }

    private static void requestAllFiles(Activity a) {
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                Intent i = new Intent(android.provider.Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        Uri.parse("package:" + a.getPackageName()));
                a.startActivity(i);
            } catch (Exception e) {
                try {
                    a.startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                } catch (Exception ignored) {}
            }
        } else {
            ActivityCompat.requestPermissions(a, new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 9001);
        }
    }
}
