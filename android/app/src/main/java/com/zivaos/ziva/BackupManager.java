package com.zivaos.ziva;

import android.content.Context;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * Zip backup of the public data dir into /sdcard/Download. Restores keep the
 * ten newest archives. Deliberately zip (not tar) — java.util.zip ships with
 * the platform and the archive is for humans/other devices, not for in-app
 * restore round-trips.
 */
public final class BackupManager {
    private BackupManager() {}

    public static File backupToDownload() throws Exception {
        File data = Constants.publicDataDir();
        if (!data.exists()) throw new IllegalStateException("数据目录不存在：" + data);
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        File out = new File(Environment_Download(), "ziva-backup-" + stamp + ".zip");
        try (ZipOutputStream zos = new ZipOutputStream(new FileOutputStream(out))) {
            zipTree(data, data, zos);
        }
        pruneOldBackups(10);
        return out;
    }

    private static File Environment_Download() {
        File dl = new File("/sdcard/Download");
        if (!dl.exists()) dl.mkdirs();
        return dl;
    }

    private static void zipTree(File root, File f, ZipOutputStream zos) throws Exception {
        File[] kids = f.listFiles();
        if (kids != null) {
            for (File k : kids) zipTree(root, k, zos);
            return;
        }
        if (!f.isFile()) return;
        String rel = root.toPath().relativize(f.toPath()).toString().replace('\\', '/');
        try (FileInputStream in = new FileInputStream(f)) {
            zos.putNextEntry(new ZipEntry(rel));
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) zos.write(buf, 0, n);
            zos.closeEntry();
        }
    }

    private static void pruneOldBackups(int keep) {
        File[] zips = Environment_Download().listFiles((d, n) -> n.startsWith("ziva-backup-") && n.endsWith(".zip"));
        if (zips == null || zips.length <= keep) return;
        java.util.Arrays.sort(zips, (a, b) -> Long.compare(b.lastModified(), a.lastModified()));
        for (int i = keep; i < zips.length; i++) zips[i].delete();
    }
}
