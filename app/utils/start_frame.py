"""Starting-frame composer for the MiniMax H3 green-screen flow.

Builds the single reference frame the video model starts from: the presenter on
the left, the room behind them, and a flat chroma-key green rectangle on the right.

This is deliberately NOT an image-model call. A generative model re-draws what it
is given, so the face drifts and the lighting shifts. Here the frame is assembled
from pixels:

  * the background file is the canvas, at its own resolution, and is only changed
    where the presenter and the green rectangle cover it;
  * the presenter keeps the RGB of the character file, with a cut-out alpha mask
    and a single resize to fit the frame;
  * the rectangle is exactly GREEN_RGB with hard edges and no anti-aliasing.

`verify_start_frame` re-reads the result and checks those guarantees.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

# One flat, fully saturated key colour. Inside the hue/sat/val window that
# app.utils.chroma_composite keys on, so the downstream composite finds it.
GREEN_RGB = (0, 255, 0)

# Layout, as fractions of the frame. The character stands in the left zone, the
# panel takes the right; the gap between them keeps the two from ever touching.
CHARACTER_ZONE_RIGHT = 0.42     # character's bounding box stays left of this
CHARACTER_HEIGHT = 0.96         # character's bounding-box height, bottom-flush
CHARACTER_CENTER_X = 0.22       # preferred horizontal centre of the character
PANEL_LEFT = 0.46
PANEL_RIGHT = 0.96
PANEL_ASPECT = 16 / 9           # a slide crop is landscape
PANEL_MAX_HEIGHT = 0.80
PANEL_CENTER_Y = 0.46           # slightly above centre, where a screen would hang

# The cut-out edge is pulled in by this many pixels (at source size) so the
# light studio backdrop the character was generated on never rims the subject.
EDGE_ERODE_PX = 2
EDGE_FEATHER_PX = 1.0
ALPHA_MIN_BBOX = 8              # alpha above this counts as "character"


class StartFrameError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartFrame:
    png: bytes
    size: tuple[int, int]                       # (w, h)
    panel: tuple[int, int, int, int]            # green rectangle (x, y, w, h)
    character_box: tuple[int, int, int, int]    # pasted character bbox (x, y, w, h)


def cut_out_character(path: Path | str) -> Image.Image:
    """Return the character as RGBA with its studio backdrop removed.

    A file that already carries a real alpha channel (library characters) is used
    as it is; an opaque one (generated characters) is matted with rembg.
    """
    img = Image.open(path)
    img.load()
    if img.mode in ("RGBA", "LA", "P") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        if np.asarray(rgba.getchannel("A")).min() < 250:
            return rgba
    rgb = img.convert("RGB")
    from rembg import new_session, remove
    cut = remove(rgb, session=new_session("u2net"), alpha_matting=True)
    if not isinstance(cut, Image.Image):
        raise StartFrameError("rembg returned no image")
    return cut.convert("RGBA")


def _refine_alpha(alpha: Image.Image) -> Image.Image:
    """Erode and lightly feather the mask so no backdrop halo survives."""
    if EDGE_ERODE_PX:
        alpha = alpha.filter(ImageFilter.MinFilter(2 * EDGE_ERODE_PX + 1))
    if EDGE_FEATHER_PX:
        alpha = alpha.filter(ImageFilter.GaussianBlur(EDGE_FEATHER_PX))
    return alpha


def compose_start_frame(character: Image.Image, background: Image.Image) -> StartFrame:
    """Paste `character` (RGBA) onto `background`, draw the green panel right."""
    bg = background.convert("RGB")
    w, h = bg.size
    if w < h:
        raise StartFrameError(
            f"the green-screen layout needs a landscape background, got {w}x{h}")

    alpha = _refine_alpha(character.getchannel("A"))
    bbox = alpha.point(lambda v: 255 if v > ALPHA_MIN_BBOX else 0).getbbox()
    if bbox is None:
        raise StartFrameError("no character found in the cut-out (empty mask)")
    subject = character.convert("RGB").crop(bbox)
    mask = alpha.crop(bbox)

    # One resize, uniform: fit the height, then shrink if the width spills over.
    sw, sh = subject.size
    scale = min(CHARACTER_HEIGHT * h / sh, CHARACTER_ZONE_RIGHT * w / sw)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    subject = subject.resize((nw, nh), Image.LANCZOS)
    mask = mask.resize((nw, nh), Image.LANCZOS)

    x = round(CHARACTER_CENTER_X * w - nw / 2)
    x = max(0, min(x, round(CHARACTER_ZONE_RIGHT * w) - nw))
    y = h - nh                                   # bottom-flush: no floating torso
    frame = bg.copy()
    frame.paste(subject, (x, y), mask)

    # Green panel: a hard-edged, solid rectangle (ImageDraw would be equivalent;
    # an array write makes "no anti-aliasing" structural rather than a setting).
    pw = round((PANEL_RIGHT - PANEL_LEFT) * w)
    ph = round(pw / PANEL_ASPECT)
    if ph > PANEL_MAX_HEIGHT * h:
        ph = round(PANEL_MAX_HEIGHT * h)
        pw = round(ph * PANEL_ASPECT)
    px = round(PANEL_LEFT * w)
    py = round(PANEL_CENTER_Y * h - ph / 2)
    py = max(0, min(py, h - ph))
    arr = np.asarray(frame).copy()
    arr[py:py + ph, px:px + pw] = GREEN_RGB
    frame = Image.fromarray(arr)

    buf = io.BytesIO()
    frame.save(buf, format="PNG")                # lossless
    return StartFrame(buf.getvalue(), (w, h), (px, py, pw, ph), (x, y, nw, nh))


def verify_start_frame(frame: StartFrame, background: Image.Image) -> None:
    """Raise StartFrameError unless the frame keeps its guarantees.

    1. the panel is exactly GREEN_RGB, every pixel;
    2. outside the character box and the panel the frame equals the background
       pixel for pixel (so lighting and colour cannot have changed);
    3. the character box and the panel do not overlap.
    """
    out = np.asarray(Image.open(io.BytesIO(frame.png)).convert("RGB"))
    ref = np.asarray(background.convert("RGB"))
    if out.shape != ref.shape:
        raise StartFrameError(f"frame {out.shape} differs from background {ref.shape}")
    px, py, pw, ph = frame.panel
    cx, cy, cw, ch = frame.character_box
    if not (cx + cw <= px or px + pw <= cx or cy + ch <= py or py + ph <= cy):
        raise StartFrameError("character overlaps the green panel")
    if not (out[py:py + ph, px:px + pw] == GREEN_RGB).all():
        raise StartFrameError("green panel is not a flat key colour")
    untouched = np.ones(out.shape[:2], dtype=bool)
    untouched[py:py + ph, px:px + pw] = False
    untouched[cy:cy + ch, cx:cx + cw] = False
    if not (out[untouched] == ref[untouched]).all():
        raise StartFrameError("background changed outside the character and panel")
