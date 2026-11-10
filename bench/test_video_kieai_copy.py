"""
kie.ai ByteDance Seedance 2.5 video generation — test script.

Seedance 2.5 runs on kie.ai's unified Market "jobs" API, NOT the Veo endpoint.
That means a DIFFERENT endpoint and payload shape than the old Veo script:
params live inside a nested `input` object, and you poll a different URL with a
different success field. (Same jobs API shape as bench/test_image_kieai.py.)

Edit the CONFIG block below, then run:
    export KIE_API_KEY="your_key_here"     # get one at https://kie.ai/api-key
    python bench/test_video_kieai.py

Prints your credit balance before/after, the raw submit response, and the final
record payload, then reports the per-task cost (creditsConsumed) and the balance
diff so you see the actual cost.

Reference: https://kie.ai/seedance-2-5

=============================================================================
JOB MODES  ->  which image field is sent
=============================================================================
  text         no images, prompt only.
  first_frame  1 image -> input.first_frame_url. The image becomes the literal
               FIRST frame of the clip (most faithful when the whole frame
               should match one image), then the model animates from it.
  first_last   2 images -> first_frame_url + last_frame_url (a last frame
               always requires a first frame). Opening + closing frame match.
  reference    1+ images -> input.reference_image_urls. "Material" images the
               video is built around. Use this when you want the model to
               COMPOSE a scene that includes your image (e.g. a presenter on
               the left + your graph on the right). Up to 30 images.

  These modes cannot be combined in one request. Per kie.ai: when a shot must
  match a specific image EXACTLY, first/last frames beat references — but a
  reference is what lets the model place your image INTO a larger composition
  alongside generated content. For "character explains my graph", reference is
  the mode to test; expect the model to still reinterpret fine chart detail.

  Local files: any IMAGE_URLS entry that is NOT an http(s) URL is treated as a
  local path, uploaded to kie.ai's temporary file host, and the returned URL is
  used automatically (kie.ai fetches images server-side).

=============================================================================
PARAMETERS — POST https://api.kie.ai/api/v1/jobs/createTask  (JSON body)
=============================================================================
  model   str   "bytedance/seedance-2-5".
  input   obj   nested, model-specific:
      prompt               str   (required) up to ~20,480 chars.
      first_frame_url      str   see JOB MODES.
      last_frame_url       str   see JOB MODES (requires first_frame_url).
      reference_image_urls list  see JOB MODES.
      duration             int   seconds, 4-30.
      resolution           str   "480p" | "720p" | "1080p" (draft = 480p only).
      aspect_ratio         str   "16:9","4:3","1:1","3:4","9:16","21:9","adaptive".
      output_format        str   "mp4" | "mov".
      generate_audio       bool  synchronized audio (on by default at kie.ai).

=============================================================================
RETURN FORMAT
=============================================================================
  Submit  -> {"code":200,"msg":"success","data":{"taskId":"task_..."}}
             HTTP 200 means CREATED, not finished (generation is async).

  GET https://api.kie.ai/api/v1/jobs/recordInfo?taskId=...  -> data:
      state           "waiting" | "queuing" | "generating" | "success" | "fail"
      resultJson      JSON *string*; parse it -> {"resultUrls": ["...mp4"]}
      creditsConsumed credits this task used  (per-task cost)
      failCode/failMsg failure detail when state == "fail"
      param/progress/costTime/createTime/completeTime
  Result URLs are temporary — download promptly.

=============================================================================
CREDIT COST
=============================================================================
  data.creditsConsumed reports the per-task cost directly; the script also
  diffs the account balance (GET https://api.kie.ai/api/v1/chat/credit) as a
  cross-check. Rates: https://kie.ai/billing ; per-task log: https://kie.ai/logs.
  Failed tasks are not charged; new accounts get 80 free credits.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# ============================ CONFIG — edit these ============================
MODEL = "bytedance/seedance-2-mini"

JOB = "reference"                   # "text" | "first_frame" | "first_last" | "reference"

PROMPT = """
A single corporate presenter stands on the LEFT third of the frame, turned
slightly toward the right, speaking to camera in and gesturing toward an image that
is displayed on the RIGHT side of the frame as if explaining it.

The presenter speaks the following line in English, and ONLY this line, with calm, clear, professional delivery and natural lip-sync:
{"Hello, good morning! Today I will explain to you the 3 stages of the innovation portofolio"}

THE IMAGE — STRICT: the image on the right is the provided reference image.
Reproduce it EXACTLY as given — THE SAME layout, colors, axes, gridlines,
labels, numbers, text, and proportions. Do NOT redraw, restyle, recolor,
relabel, translate, summarize, crop, warp, blur, add, remove, or regenerate any
part of the image. Every number and word on it must stay pixel-identical and
fully legible. It is a flat rectangular on-screen graphic panel on the right
side of the frame, not a 3D object, not a physical prop.

The presenter never overlaps, covers, or occludes the image. The image stays
fixed in place and completely unchanged for the entire clip — it does not move,
scale, rotate, slide, or animate. Only the presenter moves: natural explanatory
hand gestures toward the image, occasional glances at it and back to camera,
subtle, calm body movement.

Camera, STRICT: locked, static, eye-level medium-wide shot that keeps the
presenter on the left and the FULL image on the right in frame at all times. No
zoom, pan, tilt, dolly, push-in, orbit, handheld shake, or reframing. One
continuous static shot.

Rule: Strictly use the attached character, background and diagram inside the video as it is.

Setting: clean, softly lit modern corporate studio, neutral uncluttered
background. Professional explainer-video look.
"""

# text: []   first_frame: 1   first_last: 2 (first, last)   reference: 1+ (your image)
IMAGE_URLS = [
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/Diagram3.png",
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/Character1.png",
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/background2.jpeg",
]

ASPECT_RATIO = "16:9"               # "16:9","4:3","1:1","3:4","9:16","21:9","adaptive"
RESOLUTION = "480p"                 # "480p" | "720p" | "1080p"
DURATION = 4                       # seconds, 4-30
OUTPUT_FORMAT = "mp4"               # "mp4" | "mov"
GENERATE_AUDIO = True               # True = the presenter speaks / ambient sound

RUN_ID = "13th_video_for_reference"    # set per run; output -> _storage/kie.ai/run_<RUN_ID>/

ALLOW_EXISTING_RUN = True           # True: save into an existing run folder (new
                                    # files are numbered, never overwriting). False:
                                    # refuse a non-empty folder.
# ===========================================================================

BASE_URL = "https://api.kie.ai"
UPLOAD_ENDPOINT = "https://kieai.redpandaai.co/api/file-stream-upload"
API_KEY = os.environ.get("KIE_API_KEY")

if not API_KEY:
    sys.exit("Set KIE_API_KEY first:  export KIE_API_KEY='your_key'")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# Output root, relative to this script so it's the same wherever you run from.
STORAGE_ROOT = Path(__file__).resolve().parent / "_storage" / "kie.ai"

# job mode -> (min images, max images)
JOBS = {
    "text":        (0, 0),
    "first_frame": (1, 1),
    "first_last":  (2, 2),
    "reference":   (1, 30),
}


def get_credits():
    """Return the account's remaining credit balance (or None on failure)."""
    r = requests.get(f"{BASE_URL}/api/v1/chat/credit", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data")


def upload_local(path):
    """Upload a local file to kie.ai's temporary host; return its public URL."""
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"Image not found: {p}")
    with p.open("rb") as fh:
        # Multipart upload — don't reuse HEADERS (its JSON Content-Type is wrong);
        # requests sets the multipart boundary itself when `files=` is passed.
        r = requests.post(
            UPLOAD_ENDPOINT,
            headers={"Authorization": f"Bearer {API_KEY}"},
            files={"file": (p.name, fh)},
            data={"uploadPath": "veo-inputs"},
            timeout=120,
        )
    r.raise_for_status()
    url = (r.json().get("data") or {}).get("downloadUrl")
    if not url:
        raise RuntimeError(f"Upload returned no downloadUrl: {r.text}")
    print(f"uploaded {p.name} -> {url}")
    return url


def resolve_image_urls(entries):
    """Pass http(s) URLs through unchanged; upload everything else as a local file."""
    return [
        e if e.startswith(("http://", "https://")) else upload_local(e)
        for e in entries
    ]


def build_input(image_urls):
    """Assemble the nested `input` object for the current JOB mode."""
    task_input = {
        "prompt": PROMPT,
        "aspect_ratio": ASPECT_RATIO,
        "resolution": RESOLUTION,
        "duration": DURATION,
        "output_format": OUTPUT_FORMAT,
        "generate_audio": GENERATE_AUDIO,
    }
    if JOB == "first_frame":
        task_input["first_frame_url"] = image_urls[0]
    elif JOB == "first_last":
        task_input["first_frame_url"] = image_urls[0]
        task_input["last_frame_url"] = image_urls[1]
    elif JOB == "reference":
        task_input["reference_image_urls"] = image_urls
    # "text": no image field
    return task_input


def submit_video(task_input):
    """Submit a jobs/createTask request. Returns the taskId."""
    payload = {"model": MODEL, "input": task_input}

    r = requests.post(f"{BASE_URL}/api/v1/jobs/createTask",
                      headers=HEADERS, json=payload, timeout=30)
    print("\n--- submit response ---")
    print(r.status_code, r.text)
    r.raise_for_status()

    task_id = (r.json().get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"No taskId returned: {r.text}")
    return task_id


def poll_video(task_id, interval=15, timeout=1200):
    """Poll jobs/recordInfo until the task finishes. Returns the final record."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{BASE_URL}/api/v1/jobs/recordInfo",
                         headers=HEADERS, params={"taskId": task_id}, timeout=30)
        r.raise_for_status()
        data = r.json().get("data") or {}
        state = data.get("state")

        if state == "success":
            print("\n--- final record (success) ---")
            print(r.text)
            return data
        if state == "fail":
            print("\n--- final record (fail) ---")
            print(r.text)
            return data

        print(f"state={state} progress={data.get('progress')}… waiting {interval}s")
        time.sleep(interval)

    raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")


def result_urls(record):
    """resultJson is a JSON *string* — parse it and pull resultUrls."""
    raw = record.get("resultJson")
    if not raw:
        return []
    try:
        return (json.loads(raw) or {}).get("resultUrls") or []
    except (json.JSONDecodeError, TypeError):
        return []


def validate():
    """Fail fast on documented constraints, before spending a call."""
    if JOB not in JOBS:
        sys.exit(f"JOB must be one of {list(JOBS)}; got '{JOB}'.")
    min_imgs, max_imgs = JOBS[JOB]
    n = len(IMAGE_URLS)
    if n < min_imgs:
        sys.exit(f"'{JOB}' needs at least {min_imgs} image URL(s); got {n}.")
    if n > max_imgs:
        sys.exit(f"'{JOB}' accepts at most {max_imgs} image URL(s); got {n}.")
    if JOB == "text" and n:
        sys.exit("'text' takes no images — empty IMAGE_URLS.")

    for entry in IMAGE_URLS:
        if not entry.startswith(("http://", "https://")) \
                and not Path(entry).expanduser().is_file():
            sys.exit(f"Image not found: {entry}")

    run_dir = STORAGE_ROOT / f"run_{RUN_ID}"
    if run_dir.exists() and any(run_dir.iterdir()) and not ALLOW_EXISTING_RUN:
        sys.exit(f"{run_dir} already has files — set ALLOW_EXISTING_RUN or bump RUN_ID.")


def _unique(path):
    """Return path if free, else path with _2, _3, … so nothing is overwritten."""
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def save_run(run_id, record):
    """Write the run metadata and download the result video(s). Returns paths."""
    run_dir = STORAGE_ROOT / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    record_path = _unique(run_dir / "record.json")
    record_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    urls = result_urls(record)
    ext = "mov" if OUTPUT_FORMAT.lower() == "mov" else "mp4"
    saved = []
    for i, url in enumerate(urls):
        name = f"video.{ext}" if len(urls) == 1 else f"video_{i}.{ext}"
        dest = _unique(run_dir / name)
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        dest.write_bytes(r.content)
        saved.append(dest)
    return run_dir, saved


def main():
    validate()

    before = get_credits()
    print(f"Credits before: {before}")

    image_urls = resolve_image_urls(IMAGE_URLS) if IMAGE_URLS else []
    task_input = build_input(image_urls)

    task_id = submit_video(task_input)
    print(f"\ntaskId: {task_id}")

    record = poll_video(task_id)

    run_dir, saved = save_run(RUN_ID, record)
    print(f"\nSaved run to: {run_dir}")
    print("  record.json")
    for path in saved:
        print(f"  {path.name}")
    if not saved:
        print("  (no result videos — check record.json for the failure reason)")

    # Per-task cost is reported by this API directly; balance diff cross-checks it.
    print(f"\ncreditsConsumed (from record): {record.get('creditsConsumed')}")
    after = get_credits()
    print(f"Credits before/after: {before} -> {after}")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        print(f"Balance delta: {before - after}")


if __name__ == "__main__":
    main()
