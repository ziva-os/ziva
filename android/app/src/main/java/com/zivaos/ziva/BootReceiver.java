package com.zivaos.ziva;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After reboot, bring the backend back up if the user left it running. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        boolean wasRunning = context.getSharedPreferences("ziva", Context.MODE_PRIVATE)
                .getBoolean("run_on_boot", false);
        if (!wasRunning) return;
        Intent svc = new Intent(context, ZivaService.class);
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(svc);
        } else {
            context.startService(svc);
        }
    }
}
