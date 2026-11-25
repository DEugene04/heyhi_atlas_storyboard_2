"""The start frame must keep the character and background pixel-exact and the
green panel a flat key colour — the video model builds on it."""
import io

import numpy as np
import pytest
from PIL import Image

from app.utils.chroma_composite import detect_green_rect
from app.utils.start_frame import (
    GREEN_RGB, StartFrameError, compose_start_frame, cut_out_character,
    verify_start_frame,
)


def _background(w=640, h=360):
    # A gradient: any resample, tint or lighting change would show up in a diff.
    x = np.linspace(20, 200, w, dtype=np.uint8)[None, :].repeat(h, 0)
    y = np.linspace(30, 180, h, dtype=np.uint8)[:, None].repeat(w, 1)
    return Image.fromarray(np.dstack([x, y, (x // 2 + y // 2).astype(np.uint8)]))


def _character(w=120, h=300):
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[20:, 15:105] = (200, 60, 40, 255)        # opaque body
    return Image.fromarray(arr)


def test_frame_keeps_background_and_draws_flat_green_panel():
    bg = _background()
    frame = compose_start_frame(_character(), bg)
    verify_start_frame(frame, bg)                # raises on any violation
    out = np.asarray(Image.open(io.BytesIO(frame.png)).convert("RGB"))
    x, y, w, h = frame.panel
    assert (out[y:y + h, x:x + w] == GREEN_RGB).all()
    assert x > frame.character_box[0] + frame.character_box[2]   # panel is right of the character


def test_character_stands_left_bottom_flush():
    bg = _background()
    frame = compose_start_frame(_character(), bg)
    cx, cy, cw, ch = frame.character_box
    assert cy + ch == bg.size[1]
    assert cx + cw <= 0.42 * bg.size[0] + 1


def test_downstream_chroma_detector_finds_the_panel():
    import cv2
    # a warm, non-green room: the detector keys on hue, so a green room would confuse it
    bg = Image.new("RGB", (1280, 720), (150, 110, 90))
    frame = compose_start_frame(_character(), bg)
    bgr = cv2.cvtColor(np.asarray(Image.open(io.BytesIO(frame.png)).convert("RGB")), cv2.COLOR_RGB2BGR)
    fx, fy, fw, fh = detect_green_rect(bgr)
    px, py, pw, ph = frame.panel
    assert (fx, fy, fw, fh) == (px, py, pw, ph)


def test_verify_rejects_a_tampered_background():
    bg = _background()
    frame = compose_start_frame(_character(), bg)
    other = np.asarray(bg).copy()
    other[0, 0] = (255, 255, 255)                # a pixel far from character and panel
    with pytest.raises(StartFrameError, match="background changed"):
        verify_start_frame(frame, Image.fromarray(other))


def test_rejects_portrait_background_and_empty_mask():
    with pytest.raises(StartFrameError, match="landscape"):
        compose_start_frame(_character(), _background(360, 640))
    with pytest.raises(StartFrameError, match="empty mask"):
        compose_start_frame(Image.new("RGBA", (50, 50), (0, 0, 0, 0)), _background())


def test_cut_out_keeps_existing_alpha_without_matting(tmp_path):
    path = tmp_path / "c.png"
    _character().save(path)
    cut = cut_out_character(path)               # real alpha -> rembg not needed
    assert cut.getchannel("A").getextrema() == (0, 255)


def test_portrait_panel_is_tall_clear_of_the_character_and_verifies():
    bg = _background()
    portrait = compose_start_frame(_character(), bg, "portrait")
    landscape = compose_start_frame(_character(), bg, "landscape")
    px, py, pw, ph = portrait.panel
    assert abs(pw / ph - 9 / 16) < 0.02
    lx, ly, lw, lh = landscape.panel
    assert abs(lw / lh - 16 / 9) < 0.02
    assert portrait.size == landscape.size == bg.size           # the frame stays landscape
    assert portrait.character_box == landscape.character_box    # same character either way
    verify_start_frame(portrait, bg)
    verify_start_frame(landscape, bg)


def test_slide_orientation_and_unknown_orientation():
    from app.utils.start_frame import slide_orientation
    assert slide_orientation((300, 500)) == "portrait"
    assert slide_orientation((500, 300)) == "landscape"
    assert slide_orientation((400, 400)) == "landscape"
    with pytest.raises(StartFrameError, match="orientation"):
        compose_start_frame(_character(), _background(), "diagonal")
