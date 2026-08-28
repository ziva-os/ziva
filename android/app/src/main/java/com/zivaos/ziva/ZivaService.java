package com.zivaos.ziva;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import androidx.core.app.NotificationCompat;

/**
 * Foreground keep-alive + watchdog. The notification is the price Android
 * charges for not freezing us; the watchdog restarts the backend if it dies
 * or stops answering /status (deep probe goes to /status only — the backend
 * serves it from memory, but a dead process fails the socket connect, which
 * is the failure mode we care about here).
 */
public class ZivaService extends Service {
    private static final String CHANNEL_ID = "ziva-running";
    private static final int NOTIF_ID = 42;
    private static final long WATCHDOG_MS = 15_000;
    private static final long STARTUP_GRACE_MS = 40_000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean watchdogArmed = false;
    private long startTimestamp = 0;
    private int restartBurst = 0;

    private final Runnable watchdog = new Runnable() {
        @Override public void run() {
            try {
                boolean healthy = ZivaController.instance().httpHealthy();
                boolean procAlive = ZivaController.instance().isAlive();
                if (!healthy) {
                    boolean withinGrace = System.currentTimeMillis() - startTimestamp < STARTUP_GRACE_MS;
                    if (!procAlive || (!withinGrace && restartBurst < 3)) {
                        restartBurst++;
                        ZivaController.instance().stopBackend();
                        ZivaController.instance().startBackend(getApplicationContext());
                    }
                } else {
                    restartBurst = 0;
                }
            } finally {
                handler.postDelayed(this, WATCHDOG_MS);
            }
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                getString(R.string.notif_channel_name), NotificationManager.IMPORTANCE_LOW);
        ch.setDescription(getString(R.string.notif_channel_desc));
        nm.createNotificationChannel(ch);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIF_ID, buildNotification());
        startTimestamp = System.currentTimeMillis();
        // Ensure the backend is running even when the service is restarted
        // standalone (e.g. START_STICKY after a kill).
        new Thread(() -> {
            if (!ZivaController.instance().isAlive()) {
                ZivaController.instance().startBackend(getApplicationContext());
            }
        }).start();
        armWatchdog();
        return START_STICKY;
    }

    private void armWatchdog() {
        if (watchdogArmed) return;
        watchdogArmed = true;
        handler.postDelayed(watchdog, WATCHDOG_MS);
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(getString(R.string.notif_running_title))
                .setContentText(getString(R.string.notif_running_text))
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(watchdog);
        watchdogArmed = false;
        ZivaController.instance().stopBackend();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
