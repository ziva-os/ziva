package com.zivaos.ziva;

import android.app.Application;
import android.app.NotificationChannel;
import android.app.NotificationManager;

/** App-level singletons: notification channels + the 3090 bridge. */
public class ZivaApp extends Application {
    private static final HttpShellService BRIDGE = new HttpShellService();

    public static HttpShellService bridge() { return BRIDGE; }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel("ziva-bridge") == null) {
            NotificationChannel ch = new NotificationChannel("ziva-bridge", "Ziva 桥", NotificationManager.IMPORTANCE_DEFAULT);
            nm.createNotificationChannel(ch);
        }
        // Process-lifetime bridge: keep it independent of ZivaService restarts
        // so a service restart never leaves the rootfs agent with a dead bridge.
        BRIDGE.start(this);
    }
}
