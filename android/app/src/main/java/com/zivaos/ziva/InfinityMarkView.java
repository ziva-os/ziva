package com.zivaos.ziva;

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PathMeasure;
import android.graphics.Shader;
import android.util.AttributeSet;
import android.view.View;
import android.view.animation.AccelerateDecelerateInterpolator;

/**
 * The Mac desktop app's boot splash mark (electron/main.ts loading screen):
 * an outlined infinity loop stroked with a #8ab4f8→#cfe0ff gradient at 55%
 * opacity, with a glowing dot (#dbeaff) traveling the loop via animateMotion
 * (2.2s/lap) while the whole mark breathes (scale 1→1.03, opacity .92→1,
 * 2.8s ease-in-out). Ported 1:1 — same 120×50 design space, same timings.
 */
public class InfinityMarkView extends View {

    // electron/main.ts: the loop centerline shared by the stroke and the
    // animateMotion dot.
    private static final String LOOP_PATH =
        "M60,25.2 C62.4,22.7 64.3,20.4 66.7,18.2 C69,15.9 71.2,13.7 74.1,11.7 "
      + "C77.1,9.7 80.8,7.4 84.4,6.3 C87.9,5.3 91.7,4.7 95.4,5.5 C99,6.3 103.3,7.9 106.1,11.1 "
      + "C108.8,14.4 111.5,20.7 111.7,25.2 C111.9,29.7 109.7,34.8 107.2,38 C104.7,41.3 100.6,43.6 96.8,44.6 "
      + "C93,45.6 88,44.9 84.4,44.1 C80.8,43.2 77.9,41.3 75.1,39.4 C72.3,37.6 70,35.5 67.5,33.1 "
      + "C65,30.7 62.4,27.7 60,25.2 C57.7,22.7 55.7,20.4 53.4,18.2 C51,15.9 48.9,13.7 45.9,11.7 "
      + "C43,9.7 39.2,7.4 35.7,6.3 C32.1,5.3 28.3,4.7 24.7,5.5 C21.1,6.3 16.7,7.9 14,11.1 "
      + "C11.2,14.4 8.5,20.7 8.3,25.2 C8.1,29.7 10.4,34.8 12.9,38 C15.4,41.3 19.5,43.6 23.3,44.6 "
      + "C27.1,45.6 32,44.9 35.7,44.1 C39.3,43.2 42.1,41.3 44.9,39.4 C47.7,37.6 50,35.5 52.5,33.1 "
      + "C55,30.7 57.7,27.7 60,25.2 Z";

    private static final float VIEW_W = 120f;
    private static final float VIEW_H = 50f;
    private static final long LAP_MILLIS = 2200;      // animateMotion dur
    private static final long BREATHE_MILLIS = 2800;  // breathe cycle

    private final Path mLoop = parsePath(LOOP_PATH);
    private final PathMeasure mMeasure = new PathMeasure(mLoop, false);
    private final float[] mDotPos = new float[2];

    private final Paint mStroke = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint mDot = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint mDotHalo = new Paint(Paint.ANTI_ALIAS_FLAG);

    private final ValueAnimator mLap = ValueAnimator.ofFloat(0f, 1f);
    private final ValueAnimator mBreathe = ValueAnimator.ofFloat(0f, 1f);
    private float mLapT = 0f;
    private float mBreatheT = 0f;

    public InfinityMarkView(Context context) {
        this(context, null);
    }

    public InfinityMarkView(Context context, AttributeSet attrs) {
        super(context, attrs);
        // Colors match the Mac loading screen exactly.
        mStroke.setStyle(Paint.Style.STROKE);
        mStroke.setStrokeWidth(4f); // design-space units; scaled in onSizeChanged
        mStroke.setStrokeCap(Paint.Cap.ROUND);
        mStroke.setStrokeJoin(Paint.Join.ROUND);
        mStroke.setAlpha((int) (0.55f * 255));
        mDot.setStyle(Paint.Style.FILL);
        mDot.setColor(Color.parseColor("#dbeaff"));
        mDotHalo.setStyle(Paint.Style.FILL);
        mDotHalo.setColor(Color.argb(70, 219, 234, 255)); // fake gaussian glow

        mLap.setDuration(LAP_MILLIS);
        mLap.setRepeatCount(ValueAnimator.INFINITE);
        mLap.setInterpolator(null); // linear, like SMIL animateMotion
        mLap.addUpdateListener(a -> {
            mLapT = (float) a.getAnimatedValue();
            invalidate();
        });
        mBreathe.setDuration(BREATHE_MILLIS);
        mBreathe.setRepeatCount(ValueAnimator.INFINITE);
        mBreathe.setRepeatMode(ValueAnimator.REVERSE);
        mBreathe.setInterpolator(new AccelerateDecelerateInterpolator()); // ease-in-out
        mBreathe.addUpdateListener(a -> {
            mBreatheT = (float) a.getAnimatedValue();
            invalidate();
        });
    }

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        mLap.start();
        mBreathe.start();
    }

    @Override
    protected void onDetachedFromWindow() {
        mLap.cancel();
        mBreathe.cancel();
        super.onDetachedFromWindow();
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldw, int oldh) {
        super.onSizeChanged(w, h, oldw, oldh);
        if (w <= 0 || h <= 0) return;
        // Scale the 120×50 design space into the view, letterboxing slightly.
        float scale = Math.min(w / VIEW_W, h / VIEW_H);
        mStroke.setStrokeWidth(4f * scale);
        // Mac loop gradient is horizontal-only (x1=0 y1=0 x2=1 y2=0).
        mStroke.setShader(new LinearGradient(
                0, 0, w, 0,
                Color.parseColor("#8ab4f8"), Color.parseColor("#cfe0ff"),
                Shader.TileMode.CLAMP));
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float w = getWidth(), h = getHeight();
        if (w <= 0 || h <= 0) return;
        float scale = Math.min(w / VIEW_W, h / VIEW_H);
        // breathe: scale 1→1.03, opacity .92→1 (0%,100% ↔ 50% keyframes)
        float breatheScale = 1f + 0.03f * mBreatheT;
        int breatheAlpha = (int) ((0.92f + 0.08f * mBreatheT) * 255);

        canvas.save();
        canvas.translate(w / 2f, h / 2f);
        canvas.scale(scale * breatheScale, scale * breatheScale);
        canvas.translate(-VIEW_W / 2f, -VIEW_H / 2f);

        mStroke.setAlpha((breatheAlpha * 55) / 100); // base stroke 0.55 opacity
        canvas.drawPath(mLoop, mStroke);

        // traveling glow dot
        mMeasure.getPosTan(mLapT * mMeasure.getLength(), mDotPos, null);
        mDotHalo.setAlpha((breatheAlpha * 70) / 100);
        canvas.drawCircle(mDotPos[0], mDotPos[1], 7.5f, mDotHalo);
        canvas.drawCircle(mDotPos[0], mDotPos[1], 5f, mDotHalo);
        mDot.setAlpha(breatheAlpha);
        canvas.drawCircle(mDotPos[0], mDotPos[1], 2.9f, mDot);
        canvas.restore();
    }

    /** Minimal absolute-command parser: the loop path only uses M/C/L/Z. */
    private static Path parsePath(String d) {
        Path p = new Path();
        java.util.regex.Matcher m = java.util.regex.Pattern.compile(
                "([MLCZz])|(-?\\d+(?:\\.\\d+)?)").matcher(d);
        java.util.List<String> tok = new java.util.ArrayList<>();
        while (m.find()) tok.add(m.group());
        float[] cur = new float[2];
        for (int i = 0; i < tok.size(); ) {
            String t = tok.get(i);
            if (t.equals("M")) {
                cur[0] = Float.parseFloat(tok.get(i + 1));
                cur[1] = Float.parseFloat(tok.get(i + 2));
                p.moveTo(cur[0], cur[1]);
                i += 3;
            } else if (t.equals("L")) {
                cur[0] = Float.parseFloat(tok.get(i + 1));
                cur[1] = Float.parseFloat(tok.get(i + 2));
                p.lineTo(cur[0], cur[1]);
                i += 3;
            } else if (t.equals("C")) {
                float x1 = Float.parseFloat(tok.get(i + 1));
                float y1 = Float.parseFloat(tok.get(i + 2));
                float x2 = Float.parseFloat(tok.get(i + 3));
                float y2 = Float.parseFloat(tok.get(i + 4));
                float x = Float.parseFloat(tok.get(i + 5));
                float y = Float.parseFloat(tok.get(i + 6));
                p.cubicTo(x1, y1, x2, y2, x, y);
                cur[0] = x; cur[1] = y;
                i += 7;
            } else { // Z / z
                p.close();
                i += 1;
            }
        }
        return p;
    }
}
