"""Chroma-key green-panel detection and slide composite.

Extracted from bench/test_video_kieai.py so both the bench script and the
pdf_review pipeline share the same logic. All functions are stateless and
operate on file paths or numpy arrays.

Requirements: opencv-python-headless, numpy.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import cv2
import numpy as np

CHROMA_HUE_RANGE = (30, 90)
CHROMA_SAT_MIN = 30
CHROMA_VAL_MIN = 30


def detect_green_rect(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find the bounding box of the largest green region in *frame* (BGR).

    Returns (x, y, w, h) in pixel coordinates, or None if nothing found.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([CHROMA_HUE_RANGE[0], CHROMA_SAT_MIN, CHROMA_VAL_MIN])
    hi = np.array([CHROMA_HUE_RANGE[1], 255, 255])
    mask = cv2.inRange(hsv, lo, hi)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    return cv2.boundingRect(biggest)


def _soft_green_mask(frame_bgr: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    """Build a soft (float 0-1) chroma-key mask *only inside rect*."""
    h, w = frame_bgr.shape[:2]
    rx, ry, rw, rh = rect

    crop = frame_bgr[ry:ry + rh, rx:rx + rw]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).astype(np.float32)

    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    hue_lo, hue_hi = CHROMA_HUE_RANGE
    hue_mid = (hue_lo + hue_hi) / 2.0
    hue_half = (hue_hi - hue_lo) / 2.0

    hue_score = np.clip(1.0 - np.abs(hue - hue_mid) / hue_half, 0, 1)
    sat_score = np.clip((sat - CHROMA_SAT_MIN) / max(80, 255 - CHROMA_SAT_MIN), 0, 1)
    val_score = np.clip((val - CHROMA_VAL_MIN) / max(80, 255 - CHROMA_VAL_MIN), 0, 1)

    alpha_crop = (hue_score * sat_score * val_score).astype(np.float32)
    alpha_crop = np.clip((alpha_crop - 0.05) / (0.25 - 0.05), 0, 1)
    alpha_crop = alpha_crop ** 0.35

    dilate_k = max(3, (min(rw, rh) // 80) | 1)
    dilate_el = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_k, dilate_k))
    alpha_crop = cv2.dilate(alpha_crop, dilate_el, iterations=2)

    ksize = max(3, (min(rw, rh) // 30) | 1)
    alpha_crop = cv2.GaussianBlur(alpha_crop, (ksize, ksize), 0)

    mask = np.zeros((h, w), dtype=np.float32)
    mask[ry:ry + rh, rx:rx + rw] = alpha_crop
    return mask


def _despill_green(frame_bgr: np.ndarray, rect: tuple[int, int, int, int], band: int = 40) -> np.ndarray:
    """Suppress green spill in a band around the edges of *rect*."""
    h, w = frame_bgr.shape[:2]
    rx, ry, rw, rh = rect

    x0 = max(rx - band, 0)
    y0 = max(ry - band, 0)
    x1 = min(rx + rw + band, w)
    y1 = min(ry + rh + band, h)

    roi = frame_bgr[y0:y1, x0:x1].astype(np.float32)
    b, g, r = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]

    ys = np.arange(y0, y1)[:, None].astype(np.float32)
    xs = np.arange(x0, x1)[None, :].astype(np.float32)
    dx = np.maximum(rx - xs, 0) + np.maximum(xs - (rx + rw), 0)
    dy = np.maximum(ry - ys, 0) + np.maximum(ys - (ry + rh), 0)
    dist = np.sqrt(dx * dx + dy * dy)

    inside_x = np.clip(xs - rx, 0, rw).astype(np.float32)
    inside_y = np.clip(ys - ry, 0, rh).astype(np.float32)
    in_dist = np.minimum(np.minimum(inside_x, rw - inside_x),
                         np.minimum(inside_y, rh - inside_y))
    is_inside = (xs >= rx) & (xs < rx + rw) & (ys >= ry) & (ys < ry + rh)
    edge_dist = np.where(is_inside, in_dist, dist)

    strength = np.clip(1.0 - edge_dist / band, 0, 1).astype(np.float32)

    avg_rb = (r + b) * 0.5
    excess = np.maximum(g - avg_rb, 0)
    g_new = g - excess * strength
    roi[:, :, 1] = g_new
    frame_bgr[y0:y1, x0:x1] = np.clip(roi, 0, 255).astype(np.uint8)
    return frame_bgr


def composite_video(
    video_path: Path,
    slide_path: Path,
    out_path: Path,
    bg_path: Path | None = None,
) -> Path | None:
    """Chroma-key the green panel and composite the slide image into it.

    If *bg_path* is provided it fills the area behind the slide inside the
    green rect (matching test_video_kieai.py behaviour). Otherwise a neutral
    dark fill is used so gaps around the slide aren't black.

    Returns *out_path* on success, None if no green rect was detected.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    ret, first_frame = cap.read()
    if not ret:
        raise RuntimeError("Video has no frames")
    rect = detect_green_rect(first_frame)
    if rect is None:
        cap.release()
        return None
    rx, ry, rw, rh = rect

    # --- prepare the replacement patch (bg + slide) for the rect region ---
    if bg_path and bg_path.is_file():
        bg_full = cv2.imread(str(bg_path))
        if bg_full is None:
            raise RuntimeError(f"Cannot read background: {bg_path}")
        bgh, bgw = bg_full.shape[:2]
        bg_scale = max(frame_w / bgw, frame_h / bgh)
        scaled_w = max(int(bgw * bg_scale + 0.5), frame_w)
        scaled_h = max(int(bgh * bg_scale + 0.5), frame_h)
        bg_scaled = cv2.resize(bg_full, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
        crop_x = (scaled_w - frame_w) // 2
        crop_y = (scaled_h - frame_h) // 2
        bg_frame = bg_scaled[crop_y:crop_y + frame_h, crop_x:crop_x + frame_w]
        bg_patch = bg_frame[ry:ry + rh, rx:rx + rw]
    else:
        bg_patch = np.full((rh, rw, 3), (30, 30, 35), dtype=np.uint8)

    slide = cv2.imread(str(slide_path))
    if slide is None:
        raise RuntimeError(f"Cannot read slide: {slide_path}")
    sh, sw = slide.shape[:2]
    scale = min(rw / sw, rh / sh) * 0.90
    new_sw, new_sh = int(sw * scale), int(sh * scale)
    slide_resized = cv2.resize(slide, (new_sw, new_sh), interpolation=cv2.INTER_AREA)

    new_sw = min(new_sw, rw)
    new_sh = min(new_sh, rh)
    slide_resized = slide_resized[:new_sh, :new_sw]
    pad_x = (rw - new_sw) // 2
    pad_y = (rh - new_sh) // 2
    replacement = bg_patch.copy()
    replacement[pad_y:pad_y + new_sh, pad_x:pad_x + new_sw] = slide_resized

    # --- write composited frames (no audio) ---
    tmp_noaudio = out_path.with_name(out_path.stem + "_noaudio" + out_path.suffix)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_noaudio), fourcc, fps, (frame_w, frame_h))

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        alpha = _soft_green_mask(frame, rect)
        composite = frame.copy()
        a3 = alpha[ry:ry + rh, rx:rx + rw, np.newaxis]
        roi = composite[ry:ry + rh, rx:rx + rw]
        composite[ry:ry + rh, rx:rx + rw] = (
            a3 * replacement.astype(np.float32)
            + (1.0 - a3) * roi.astype(np.float32)
        ).astype(np.uint8)
        _despill_green(composite, rect, band=30)
        writer.write(composite)

    cap.release()
    writer.release()

    # --- mux original audio back in via ffmpeg ---
    ffmpeg_env = os.environ.copy()
    x265_cellar = Path("/opt/homebrew/Cellar/x265")
    if x265_cellar.is_dir():
        lib_dirs = [str(p / "lib") for p in x265_cellar.iterdir() if p.is_dir()]
        existing = ffmpeg_env.get("DYLD_LIBRARY_PATH", "")
        ffmpeg_env["DYLD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))

    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(tmp_noaudio),
            "-i", str(video_path),
            "-c:v", "copy", "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0?",
            "-shortest", str(out_path),
        ], check=True, capture_output=True, timeout=120, env=ffmpeg_env)
        tmp_noaudio.unlink(missing_ok=True)
    except FileNotFoundError:
        tmp_noaudio.rename(out_path)
    except subprocess.CalledProcessError:
        tmp_noaudio.rename(out_path)

    return out_path
