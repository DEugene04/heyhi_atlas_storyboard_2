"""Run just the chroma-key composite on an existing video — no API call."""

import os, sys
os.environ.setdefault("KIE_API_KEY", "dummy")

from pathlib import Path
from bench.test_video_kieai import composite_video

RUN_DIR = Path("_storage/h3_greenscreen_test")
VIDEO   = RUN_DIR / "h3_test.mp4"
SLIDE   = Path("_storage/image_library/Diagram2.png")
BG      = Path("_storage/image_library/background2.jpeg")
OUT     = RUN_DIR / "h3_test_composite.mp4"

if __name__ == "__main__":
    composite_video(VIDEO, SLIDE, BG, OUT)
