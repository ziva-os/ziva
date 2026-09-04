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
    // Covers the first-boot prep phase: rootfs extraction, then the
    // SYNCHRONOUS chromium download + apt dependency install (several
    // minutes). During all of it the backend process is alive but /status
    // doesn't answer — sustainedUnreachable must not fire here (r24: it
    // killed the backend mid-download and the boot restarted from zero).
    private static final long STARTUP_GRACE_MS = 600_000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean watchdogArmed = false;
    private long startTimestamp = 0;
    private int restartBurst = 0;
    private int unhealthyStreak = 0;

    private final Runnable watchdog = new Runnable() {
        @Override public void run() {
            try {
                boolean healthy = ZivaController.instance().httpHealthy();
                boolean procAlive = ZivaController.instance().isAlive();
                if (healthy) {
                    unhealthyStreak = 0;
                    restartBurst = 0;
                } else {
                    unhealthyStreak++;
                    boolean withinGrace = System.currentTimeMillis() - startTimestamp < STARTUP_GRACE_MS;
                    // Restart only when the process is actually gone, or when a
                    // LIVE process has been unreachable for a sustained stretch
                    // (streak ≥ 4 ≈ 60s). A busy event loop — tool execution,
                    // LLM streaming, MCP connect retries — can push a single
                    // /status probe past its 1.5s budget; killing a live-but-
                    // busy backend on that alone severed SSE mid-turn and was
                    // the r19 "tool call keeps getting interrupted" bug.
                    boolean sustainedUnreachable = unhealthyStreak >= 4;
                    // A LIVE backend can stay unreachable for minutes while
                    // it works (tool runs, LLM streaming, MCP connects all
                    // saturate the event loop past the 1.5s probe budget).
                    // Its log keeps growing the whole time (kernel-side
                    // O_APPEND). Killing on /status silence alone was the
                    // "总是中断" bug: a precise 60s kill loop (streak>=4 x
                    // 15s) that severed the agent's turn every minute via
                    // stopBackend -> killStrayBackends -> pkill -9 (the
                    // "137 = system/OOM" reading was wrong — that KILL is
                    // ours). Forgive the streak while the log has a pulse.
                    if (sustainedUnreachable
                            && ZivaController.instance().logActiveWithin(45_000)) {
                        unhealthyStreak = 0; // busy, not dead
                    } else if (!procAlive || (sustainedUnreachable && !withinGrace && restartBurst < 3)) {
                        restartBurst++;
                        unhealthyStreak = 0;
                        ZivaController.instance().stopBackend();
                        ZivaController.instance().startBackend(getApplicationContext());
                    }
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
        // Deep link: the in-page ⋮ hides once the UI is up (it overlapped the
        // composer), so the notification is the standing entry to the device
        // menu (diagnostics / restart / backup).
        open.putExtra("open_menu", true);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
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
