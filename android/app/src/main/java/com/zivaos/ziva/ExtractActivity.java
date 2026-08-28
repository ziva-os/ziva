package com.zivaos.ziva;

import android.app.Activity;
import android.os.Bundle;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import java.util.concurrent.atomic.AtomicBoolean;

/** First-run offline extraction screen. Mandatory gate before the main UI. */
public class ExtractActivity extends Activity {
    private final AtomicBoolean running = new AtomicBoolean(false);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_extract);
        ProgressBar bar = findViewById(R.id.extractProgress);
        TextView detail = findViewById(R.id.extractDetail);

        if (ZivaController.instance().isExtracted(this)) {
            goMain();
            return;
        }
        if (!running.compareAndSet(false, true)) return;

        new Thread(() -> {
            final long[] lastReport = {0};
            try {
                ZivaController.instance().extractOffline(this, (entry, total) -> {
                    long now = System.currentTimeMillis();
                    if (now - lastReport[0] < 200) return; // throttle UI updates
                    lastReport[0] = now;
                    runOnUiThread(() -> {
                        // Indeterminate-ish: we don't know total size up front,
                        // so show downloaded MB.
                        bar.setIndeterminate(false);
                        bar.setProgress((int) Math.min(100, total / (1024 * 1024)));
                        detail.setText(String.format("%.0f MB 已解压 · %s", total / 1048576.0, shortName(entry)));
                    });
                });
                runOnUiThread(this::goMain);
            } catch (Exception e) {
                runOnUiThread(() -> {
                    Toast.makeText(this, "解压失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                    detail.setText("失败：" + e.getMessage());
                    bar.setIndeterminate(false);
                });
            } finally {
                running.set(false);
            }
        }, "ziva-extract").start();
    }

    private static String shortName(String entry) {
        int i = entry.lastIndexOf('/');
        return i >= 0 ? entry.substring(i + 1) : entry;
    }

    private void goMain() {
        // Must land with the extraction check passing, or Main would loop back here.
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }
}
