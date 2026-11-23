"""
bench/run_h3_start_frame_test.py

One MiniMax H3 first-frame (image-to-video) clip from the green-screen starting frame,
using the same prompt as the PDF-review flow. Standalone: needs only requests, Pillow,
numpy, python-dotenv and ffmpeg, so it also runs on the GPU box (where the clip
download does not cross the slow Tailscale link).

Run from the repo root; resolution is an optional argument:
  python bench/run_h3_start_frame_test.py [RESOLUTION]        e.g.  ... 768P
Output: _storage/h3/start_frame_test/first_frame_<resolution>_seed<N>.mp4
Reuses start_frame.png in that folder if present, else builds it from the SETTINGS inputs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from PIL import Image

from app.clients.video_minimax_h3_client import MinimaxH3Client
from app.utils.presenter_prompt import first_frame_scene_prompt
from app.utils.start_frame import compose_start_frame, cut_out_character, verify_start_frame

# ----------------------------- SETTINGS -----------------------------
NARRATION = "Welcome to the safety briefing. Today we cover three key rules."
CHARACTER_IMAGE = Path("_storage/image_library/Character2.png")
BACKGROUND_IMAGE = Path("_storage/image_library/background2.jpeg")
DURATION_S = 6                        # 4-15; the GPU is shared
SEED = 1234                           # keep identical across modes
ASPECT_RATIO = "16:9"
RESOLUTION = sys.argv[1] if len(sys.argv) > 1 else "768P"                   # short side: 480P took ~4 min for 6 s; cost grows with pixels
TURBO = False                         # keep False: turbo changes quality, which is what is being judged
OUT_DIR = Path("_storage/h3/start_frame_test")
# --------------------------------------------------------------------

prompt = first_frame_scene_prompt(NARRATION)

OUT_DIR.mkdir(parents=True, exist_ok=True)
frame_path = OUT_DIR / "start_frame.png"
if not frame_path.exists():
    bg = Image.open(BACKGROUND_IMAGE).convert("RGB")
    frame = compose_start_frame(cut_out_character(CHARACTER_IMAGE), bg)
    verify_start_frame(frame, bg)
    frame_path.write_bytes(frame.png)
print("start frame:", frame_path.resolve())
print("seed:", SEED, "| duration:", DURATION_S, "s | turbo:", TURBO)
print("PROMPT:\n", prompt, "\n")
(OUT_DIR / "first_frame_prompt.txt").write_text(prompt, encoding="utf-8")

result = MinimaxH3Client().generate(
    prompt, [], first_frame=frame_path, duration=DURATION_S, aspect_ratio=ASPECT_RATIO,
    resolution=RESOLUTION, seed=SEED, turbo=TURBO)

out = OUT_DIR / f"first_frame_{RESOLUTION}_seed{SEED}.mp4"
out.write_bytes(result["video_bytes"])
print("task:", result["task"], "| job:", result["job_id"], "| seconds:", result["generation_seconds"])
print("saved:", out.resolve())
