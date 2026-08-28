package com.zivaos.ziva;

import android.content.Context;
import android.system.Os;
import java.io.*;
import java.util.zip.GZIPInputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.Enumeration;

/**
 * Pure-Java tar / tar.gz extraction used to lay down the offline rootfs.
 * - Locates the bundle by enumerating the APK zip: aapt mangles large asset
 *   entries and AssetManager.open() cannot stream 300MB+, so the bundle is
 *   packaged as assets/offline-rootfs.bin and read straight out of the APK.
 * - Handles plain files, directories, symlinks (type 2, via Os.symlink —
 *   SELinux permits symlinks in app storage, unlike hard links) and hard
 *   links (type 1, materialised as file copies for the same reason), plus
 *   GNU LongLink ("L") and pax ("x") path overrides — venv paths regularly
 *   exceed the 100-char ustar name field.
 * - extractLenient() skips suspicious entries instead of aborting (counted
 *   in lastSkipped) so one odd file cannot brick first-run setup.
 */
public final class TarGzipExtractor {
    public int lastSkipped = 0;

    private TarGzipExtractor() {}

    /** Find the offline bundle stream from the installed APK. */
    public static InputStream openOfflineBundle(Context ctx) throws IOException {
        try (ZipFile apk = new ZipFile(ctx.getPackageCodePath())) {
            Enumeration<? extends ZipEntry> en = apk.entries();
            while (en.hasMoreElements()) {
                ZipEntry e = en.nextElement();
                String n = e.getName();
                if (n.startsWith("assets/offline-rootfs.") &&
                        (n.endsWith(".bin") || n.endsWith(".tar") || n.endsWith(".tar.gz") || n.endsWith(".tgz"))) {
                    return apk.getInputStream(e);
                }
            }
        }
        return null;
    }

    public static void extractAuto(InputStream in, File destDir, Progress cb) throws IOException {
        PushbackInputStream pin = new PushbackInputStream(new BufferedInputStream(in), 2);
        int b0 = pin.read(), b1 = pin.read();
        pin.unread(b1);
        pin.unread(b0);
        boolean gzip = b0 == 0x1f && b1 == 0x8b;
        InputStream src = gzip ? new GZIPInputStream(pin, 64 * 1024) : pin;
        new TarGzipExtractor().extractLenient(src, destDir, cb);
    }

    public void extractLenient(InputStream tarStream, File destDir, Progress cb) throws IOException {
        lastSkipped = 0;
        if (!destDir.exists()) destDir.mkdirs();
        DataInputStream din = new DataInputStream(new BufferedInputStream(tarStream, 128 * 1024));
        byte[] header = new byte[512];
        long total = 0;
        String pendingName = null; // from a preceding "L"/"x" entry
        while (true) {
            if (!readFully(din, header)) break;
            boolean allZero = true;
            for (byte b : header) if (b != 0) { allZero = false; break; }
            if (allZero) break;
            String name = parseString(header, 0, 100);
            String sizeStr = parseString(header, 124, 12).trim();
            String type = String.valueOf((char) (header[156] & 0xff));
            long size;
            try { size = sizeStr.isEmpty() ? 0 : Long.parseLong(sizeStr, 8); }
            catch (NumberFormatException nfe) { size = 0; }
            if (name.startsWith("./")) name = name.substring(2);

            // GNU LongLink: the entry body is the next header's real path.
            if ("L".equals(type)) {
                byte[] buf = new byte[(int) size];
                readFully(din, buf);
                int end = 0; while (end < buf.length && buf[end] != 0) end++;
                pendingName = new String(buf, 0, end, java.nio.charset.StandardCharsets.UTF_8);
                skipPadding(din, size);
                continue;
            }
            // pax extended header: keep only the path override, skip the rest.
            if ("x".equals(type)) {
                byte[] buf = new byte[(int) size];
                readFully(din, buf);
                String s = new String(buf, java.nio.charset.StandardCharsets.UTF_8);
                java.util.regex.Matcher m = java.util.regex.Pattern.compile("\\d+ path=([^\\n]+)").matcher(s);
                if (m.find()) pendingName = m.group(1);
                skipPadding(din, size);
                continue;
            }
            if (pendingName != null) {
                if (pendingName.startsWith("./")) pendingName = pendingName.substring(2);
                name = pendingName;
                pendingName = null;
            }

            File out = safeChild(destDir, name);
            if (out == null) { skip(din, size); lastSkipped++; continue; }
            total += size;
            if (cb != null) cb.onProgress(name, total);
            if ("5".equals(type) || name.endsWith("/")) { out.mkdirs(); continue; }
            if ("2".equals(type)) {
                // Symlink: link target lives in the linkname field (157..257).
                String target = parseString(header, 157, 100);
                File parent = out.getParentFile();
                if (parent != null) parent.mkdirs();
                try { Os.symlink(target, out.getAbsolutePath()); }
                catch (Exception e) { lastSkipped++; }
                skip(din, size); skipPadding(din, size); // normally 0; defensive
                continue;
            }
            if ("1".equals(type)) {
                // Hard link: materialise as a copy (SELinux forbids link(2) in
                // app storage — that limitation is exactly what proot's
                // --link2symlink papers over inside the guest, not out here).
                String target = parseString(header, 157, 100);
                File src = safeChild(destDir, target);
                if (src != null && src.isFile()) {
                    File parent = out.getParentFile();
                    if (parent != null) parent.mkdirs();
                    try (InputStream fin = new FileInputStream(src); OutputStream fout = new FileOutputStream(out)) {
                        copyN(fin, fout, src.length());
                    } catch (Exception e) { lastSkipped++; }
                } else lastSkipped++;
                skip(din, size); skipPadding(din, size); // normally 0; defensive
                continue;
            }
            if ("0".equals(type) || type.equals("\0") || type.isEmpty()) {
                File parent = out.getParentFile();
                if (parent != null) parent.mkdirs();
                try (OutputStream fout = new FileOutputStream(out)) {
                    copyN(din, fout, size);
                }
                skipPadding(din, size);
                // Preserve the executable bit for interpreter/venv binaries.
                String modeStr = parseString(header, 100, 8).trim();
                try { int mode = Integer.parseInt(modeStr, 8); Os.chmod(out.getAbsolutePath(), mode & 0777); }
                catch (Exception ignore) {}
            } else {
                skip(din, size);
                skipPadding(din, size);
                lastSkipped++;
            }
        }
    }

    public interface Progress { void onProgress(String entry, long totalBytes); }

    private static File safeChild(File destDir, String name) throws IOException {
        if (name.isEmpty() || name.contains("..")) return null;
        File f = new File(destDir, name);
        String canon = f.getCanonicalPath();
        if (!canon.startsWith(destDir.getCanonicalPath() + File.separator)
                && !canon.equals(destDir.getCanonicalPath())) return null;
        return f;
    }

    private static String parseString(byte[] h, int off, int len) {
        int end = off;
        while (end < off + len && end < h.length && h[end] != 0) end++;
        return new String(h, off, end - off, java.nio.charset.StandardCharsets.US_ASCII);
    }

    private static boolean readFully(DataInputStream in, byte[] buf) throws IOException {
        int off = 0;
        while (off < buf.length) {
            int n = in.read(buf, off, buf.length - off);
            if (n < 0) return off == 0; // truncated tail: stop
            off += n;
        }
        return true;
    }

    /** tar records are padded to 512-byte boundaries. */
    private static void skipPadding(DataInputStream in, long size) throws IOException {
        int pad = (int) ((512 - (size % 512)) % 512);
        if (pad > 0) in.readFully(new byte[pad]);
    }

    private static void copyN(InputStream in, OutputStream out, long n) throws IOException {
        byte[] buf = new byte[64 * 1024];
        long left = n;
        while (left > 0) {
            int r = in.read(buf, 0, (int) Math.min(buf.length, left));
            if (r < 0) break;
            out.write(buf, 0, r);
            left -= r;
        }
    }

    private static void skip(InputStream in, long n) throws IOException {
        long left = n;
        while (left > 0) {
            long s = in.skip(left);
            if (s <= 0) { if (in.read() < 0) break; left--; }
            else left -= s;
        }
    }
}
