package com.zivaos.ziva;

import android.content.Context;
import java.io.File;

/** Single source of truth for paths, ports and versioned markers. */
public final class Constants {
    private Constants() {}

    public static final int BACKEND_PORT = 4097;
    public static final int BRIDGE_PORT = 3090;

    /** Guest paths inside the proot'd rootfs. */
    public static final String GUEST_HOME = "/root";
    public static final String GUEST_ZIVA_SRC = "/opt/ziva-src";
    public static final String GUEST_VENV_PY = "/opt/ziva-venv/bin/python";
    public static final String GUEST_DATA_DIR = "/root/.ziva";
    public static final String GUEST_WORKSPACE = "/root/workspace";

    /** Marker files — the extraction marker embeds ROOTFS_VERSION so an
     *  upgraded APK whose bundled rootfs changed re-extracts over the old one. */
    public static final int ROOTFS_VERSION = 1;
    public static final String MARKER_EXTRACTED = ".offline-extracted.v" + ROOTFS_VERSION;

    public static File rootfsDir(Context ctx) {
        return new File(ctx.getFilesDir(), "linux/ubuntu");
    }

    public static File markerFile(Context ctx) {
        return new File(ctx.getFilesDir(), "linux/" + MARKER_EXTRACTED);
    }

    /** Public data dir (survives uninstall): /sdcard/Documents/zivadata */
    public static File publicDataDir() {
        File docs = new File("/sdcard/Documents");
        return new File(docs, "zivadata");
    }

    public static File workspaceDir() {
        return new File(publicDataDir(), "workspace");
    }

    public static File nativeLibDir(Context ctx) {
        return new File(ctx.getApplicationInfo().nativeLibraryDir);
    }
}
