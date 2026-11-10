"""Run just the chroma-key composite on an existing video — no API call."""

import os, sys
os.environ.setdefault("KIE_API_KEY", "dummy")

from pathlib import Path
from bench.test_video_kieai import composite_video

RUN_DIR = Path("bench/_storage/kie.ai/run_14th_greenkey_composite")
VIDEO   = RUN_DIR / "video_6.mp4"
SLIDE   = Path("_storage/image_library/Diagram2.png")
BG      = Path("_storage/image_library/background2.jpeg")
OUT     = RUN_DIR / "video_8_composite.mp4"

if __name__ == "__main__":
    composite_video(VIDEO, SLIDE, BG, OUT)
