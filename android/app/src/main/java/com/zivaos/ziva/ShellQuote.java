package com.zivaos.ziva;

/**
 * POSIX single-quote escaping — the only shell-quoking implementation in the
 * tree. Any value interpolated into a `bash -c` string must go through here
 * so spaces, quotes and `$` in user-controlled paths cannot break out.
 */
public final class ShellQuote {
    private ShellQuote() {}

    public static String singleQuote(String s) {
        if (s == null) return "''";
        return "'" + s.replace("'", "'\\''") + "'";
    }

    public static String join(String... parts) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.length; i++) {
            if (i > 0) sb.append(' ');
            sb.append(singleQuote(parts[i]));
        }
        return sb.toString();
    }
}
