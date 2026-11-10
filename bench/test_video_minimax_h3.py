"""
MiniMax H3 video generation — local GPU test script.

Connects to a locally-served MiniMax H3 instance (via vLLM or SGLang) running
on your own GPU. The server exposes an OpenAI-style endpoint at localhost.

Serving (vLLM, 4× GPU example):
    vllm serve /path/to/MiniMax-H3 \
        --omni --host 0.0.0.0 --port 8000 --trust-remote-code \
        --num-gpus 4 --usp 4 --ring 1 \
        --vae-patch-parallel-size 4 --vae-parallel-mode tile --vae-use-tiling

Serving (SGLang, FL2VA example):
    sglang serve --model-path MiniMaxAI/MiniMax-H3 \
        --num-gpus 4 --ulysses-degree 4 --performance-mode speed \
        --host 0.0.0.0 --port 30010 --model-variant fl2va

Then run this script:
    python bench/test_video_minimax_h3.py

=============================================================================
TASK TYPES
=============================================================================
  t2va      Text-to-video-audio. Prompt only, no images.
  fl2va     First/last-frame-to-video-audio. 1–2 images (first frame,
            optionally last frame) + prompt. The image becomes the literal
            first/last frame of the clip.
  ref2va    Reference-to-video-audio. 1–9 reference images + prompt. The
            model composes a scene that incorporates the reference material.

=============================================================================
LOCAL API (vLLM)
=============================================================================
  Sync:   POST http://<host>:<port>/v1/videos/sync   -> MP4 bytes directly
  Async:  POST http://<host>:<port>/v1/videos         -> job ID, poll later

  Both accept multipart form data (-F fields).

=============================================================================
KEY PARAMETERS
=============================================================================
  prompt              str    Up to 7000 chars.
  width / height      int    Multiples of 32. Short side 768 for 768p.
  fps                 int    24 (fixed).
  num_inference_steps int    50 (reference quality).
  flow_shift          float  12.0 for video.
  seed                int    Fixed value for deterministic output.
  extra_params        JSON   {"task":"t2va|fl2va|ref2va", "duration":4-15,
                              "audio_flow_shift":3.0}

=============================================================================
RESPONSE
=============================================================================
  Sync endpoint returns raw MP4 bytes (H.264 video @ 24fps, stereo AAC @ 32kHz).
  Save directly to disk.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# ============================ CONFIG — edit these ============================

SERVER_URL = os.environ.get("MINIMAX_H3_URL", "http://127.0.0.1:8000")

TASK = "t2va"  # "t2va" | "fl2va" | "ref2va"

PROMPT = """
A corporate presenter in a modern office speaks to camera, explaining a concept
with calm, clear delivery and natural gestures. Professional lighting, shallow
depth of field background. The presenter speaks in English with natural lip-sync.
"""

# fl2va: 1–2 images (first frame, optionally last frame)
# ref2va: 1–9 reference images
# t2va: leave empty
IMAGE_PATHS: list[str] = []

WIDTH = 1344       # pixels (multiples of 32)
HEIGHT = 768       # pixels — 1344×768 = 16:9 at 768p
DURATION = 5       # seconds, 4–15
FPS = 24           # fixed at 24
NUM_INFERENCE_STEPS = 50
FLOW_SHIFT = 12.0
AUDIO_FLOW_SHIFT = 3.0
SEED = 42          # set to -1 for random

RUN_ID = "01_minimax_h3_test"
ALLOW_EXISTING_RUN = True

# Async mode: use the job-polling endpoint instead of the sync one.
USE_ASYNC = False
POLL_INTERVAL = 15  # seconds
POLL_TIMEOUT = 600  # seconds

# ===========================================================================

STORAGE_ROOT = Path(__file__).resolve().parent / "_storage" / "minimax_h3"

# task -> (min images, max images)
TASKS = {
    "t2va":  (0, 0),
    "fl2va": (1, 2),
    "ref2va": (1, 9),
}


def validate():
    if TASK not in TASKS:
        sys.exit(f"TASK must be one of {list(TASKS)}; got '{TASK}'.")
    min_imgs, max_imgs = TASKS[TASK]
    n = len(IMAGE_PATHS)
    if n < min_imgs:
        sys.exit(f"'{TASK}' needs at least {min_imgs} image(s); got {n}.")
    if n > max_imgs:
        sys.exit(f"'{TASK}' accepts at most {max_imgs} image(s); got {n}.")
    for p in IMAGE_PATHS:
        if not Path(p).expanduser().is_file():
            sys.exit(f"Image not found: {p}")

    run_dir = STORAGE_ROOT / f"run_{RUN_ID}"
    if run_dir.exists() and any(run_dir.iterdir()) and not ALLOW_EXISTING_RUN:
        sys.exit(f"{run_dir} already has files — set ALLOW_EXISTING_RUN or bump RUN_ID.")


def health_check():
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=10)
        r.raise_for_status()
        print(f"Server healthy: {SERVER_URL}")
        return True
    except requests.RequestException as e:
        print(f"Server health check failed: {e}")
        return False


def _build_form_fields() -> dict:
    extra = {
        "task": TASK,
        "duration": float(DURATION),
        "audio_flow_shift": AUDIO_FLOW_SHIFT,
    }
    fields = {
        "prompt": (None, PROMPT.strip()),
        "width": (None, str(WIDTH)),
        "height": (None, str(HEIGHT)),
        "fps": (None, str(FPS)),
        "num_inference_steps": (None, str(NUM_INFERENCE_STEPS)),
        "flow_shift": (None, str(FLOW_SHIFT)),
        "extra_params": (None, json.dumps(extra)),
    }
    if SEED >= 0:
        fields["seed"] = (None, str(SEED))
    return fields


def _open_images() -> list[tuple[str, tuple[str, bytes, str]]]:
    """Return a list of (field_name, (filename, bytes, mime)) tuples for multipart."""
    opened = []
    for p in IMAGE_PATHS:
        path = Path(p).expanduser()
        suffix = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        opened.append(("image", (path.name, path.read_bytes(), mime)))
    return opened


def generate_sync() -> bytes:
    """POST to /v1/videos/sync — returns raw MP4 bytes."""
    url = f"{SERVER_URL}/v1/videos/sync"
    fields = _build_form_fields()
    images = _open_images()

    print(f"\nPOST {url}")
    print(f"  task={TASK}  {WIDTH}x{HEIGHT}  {DURATION}s  seed={SEED}")
    if images:
        print(f"  images: {[name for _, (name, _, _) in images]}")

    files_list = list(fields.items()) + images
    start = time.time()
    r = requests.post(url, files=files_list, timeout=POLL_TIMEOUT)
    elapsed = time.time() - start

    print(f"  status={r.status_code}  elapsed={elapsed:.1f}s  size={len(r.content)} bytes")

    if r.status_code != 200:
        print(f"  ERROR: {r.text[:500]}")
        r.raise_for_status()

    content_type = r.headers.get("content-type", "")
    if "video" not in content_type and "octet" not in content_type:
        print(f"  WARNING: unexpected content-type '{content_type}'")
        print(f"  Body preview: {r.text[:300]}")

    return r.content


def generate_async() -> bytes:
    """POST to /v1/videos — returns a task ID, then poll for the result."""
    url = f"{SERVER_URL}/v1/videos"
    fields = _build_form_fields()
    images = _open_images()

    print(f"\nPOST {url} (async)")
    print(f"  task={TASK}  {WIDTH}x{HEIGHT}  {DURATION}s  seed={SEED}")

    files_list = list(fields.items()) + images
    r = requests.post(url, files=files_list, timeout=60)
    print(f"  status={r.status_code}")

    if r.status_code != 200:
        print(f"  ERROR: {r.text[:500]}")
        r.raise_for_status()

    data = r.json()
    print(f"  response: {json.dumps(data, indent=2)}")

    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        raise RuntimeError(f"No task_id in response: {data}")

    return _poll_task(task_id)


def _poll_task(task_id: str) -> bytes:
    """Poll until the async task completes; download and return the MP4."""
    print(f"\nPolling task {task_id}...")
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        r = requests.get(
            f"{SERVER_URL}/v1/videos/{task_id}",
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "unknown")
        print(f"  status={status}")

        if status in ("succeeded", "complete", "success"):
            video_url = data.get("video_url") or data.get("url")
            if video_url:
                vr = requests.get(video_url, timeout=180)
                vr.raise_for_status()
                return vr.content
            video_b64 = data.get("video")
            if video_b64:
                import base64
                return base64.b64decode(video_b64)
            raise RuntimeError(f"Task succeeded but no video in response: {data}")

        if status in ("failed", "error", "cancelled"):
            raise RuntimeError(f"Task {task_id} failed: {data}")

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"Task {task_id} did not finish within {POLL_TIMEOUT}s")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def save_run(video_bytes: bytes) -> Path:
    run_dir = STORAGE_ROOT / f"run_{RUN_ID}"
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task": TASK,
        "prompt": PROMPT.strip(),
        "width": WIDTH,
        "height": HEIGHT,
        "duration": DURATION,
        "fps": FPS,
        "num_inference_steps": NUM_INFERENCE_STEPS,
        "flow_shift": FLOW_SHIFT,
        "audio_flow_shift": AUDIO_FLOW_SHIFT,
        "seed": SEED,
        "image_paths": IMAGE_PATHS,
        "server_url": SERVER_URL,
    }
    meta_path = _unique(run_dir / "meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    video_path = _unique(run_dir / "video.mp4")
    video_path.write_bytes(video_bytes)

    return run_dir


def main():
    validate()

    if not health_check():
        sys.exit("Cannot reach the local MiniMax H3 server. Is it running?")

    if USE_ASYNC:
        video_bytes = generate_async()
    else:
        video_bytes = generate_sync()

    if not video_bytes:
        sys.exit("No video data received.")

    run_dir = save_run(video_bytes)
    print(f"\nSaved to: {run_dir}")
    print(f"  meta.json")
    print(f"  video.mp4  ({len(video_bytes):,} bytes)")


if __name__ == "__main__":
    main()
