"""
bench/run_h3_greenscreen_once.py

ONE MiniMax H3 call (company GPU server, MINIMAX_H3_URL) to check how well it renders
the chroma-key green panel. Uses the exact prompt builder from the PDF-review flow.

Run from the repo root:
  /opt/homebrew/Caskroom/miniconda/base/envs/heyhi_atlas_ai_video/bin/python bench/run_h3_greenscreen_once.py

Edit the SETTINGS block below, then rerun. Output: _storage/h3_greenscreen_test/h3_test.mp4
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.clients.video_minimax_h3_client import MinimaxH3Client
from app.pdf_review import _scene_prompt

# ----------------------------- SETTINGS -----------------------------
NARRATION = "Welcome to the safety briefing. Today we cover three key rules."
CHARACTER_IMAGE: Path | None = Path("_storage/image_library/Character1.png")   # None = text-to-video
BACKGROUND_IMAGE: Path | None = Path("_storage/image_library/background2.jpeg")  # optional room reference
DURATION_S = 4                        # 4-15; keep short, the GPU is shared
ASPECT_RATIO = "16:9"
RESOLUTION = "480P"                   # short side, e.g. "480P" or "768P"
OUT_DIR = Path("_storage/h3/1st_greenscreen_test")
# --------------------------------------------------------------------

images = [p for p in (CHARACTER_IMAGE, BACKGROUND_IMAGE) if p]
prompt = _scene_prompt(NARRATION, CHARACTER_IMAGE is not None, BACKGROUND_IMAGE is not None)
print("PROMPT:\n", prompt, "\n")

result = MinimaxH3Client().generate(
    prompt, images, duration=DURATION_S, aspect_ratio=ASPECT_RATIO, resolution=RESOLUTION
)
OUT_DIR.mkdir(parents=True, exist_ok=True)
out = OUT_DIR / "h3_test.mp4"
out.write_bytes(result["video_bytes"])
print("task:", result["task"], "| job:", result["job_id"], "| seconds:", result["generation_seconds"])
print("saved:", out.resolve())
