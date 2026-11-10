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
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests

try:
    import cv2
except ImportError:
    sys.exit("opencv-python is required:  pip install opencv-python-headless numpy")

# ============================ CONFIG — edit these ============================
MODEL = "bytedance/seedance-2-mini"

JOB = "reference"                   # "text" | "first_frame" | "first_last" | "reference"

PROMPT = """
A single corporate presenter stands on the LEFT third of the frame, turned
slightly toward the right, speaking to camera and gesturing toward a flat panel
on the RIGHT side of the frame as if explaining a graphic shown there.

The presenter MUST look exactly like the provided character reference image —
same face, hair, clothing, and build. Reproduce the character faithfully.

The background/setting MUST match the provided background reference image —
same environment, lighting, colors, and layout. Reproduce it faithfully.

The presenter speaks the following line in English, and ONLY this line, with calm, clear, professional delivery and natural lip-sync:
{"The CBM Innovation council consist of 3 levels, the innovation council, portfolio and steering level"}

THE PANEL — STRICT: on the RIGHT side of the frame there is a large, flat,
rectangular panel filled with ONE solid, uniform chroma-key green (bright pure
green, like a green screen). It is a placeholder that will be replaced later, so
it must be perfectly keyable: a single even green with NO text, numbers, logos,
icons, charts, images, patterns, textures, gradients, shadows, highlights,
reflections, borders, or glare. Do NOT draw anything on it, do NOT add any
content, and do NOT vary its color across its surface. The panel is a flat
on-screen rectangle, not a 3D object and not a physical prop.

The presenter never overlaps, covers, or occludes the green panel. The panel
stays fixed in place, perfectly rectangular, and completely unchanged for the
entire clip — it does not move, scale, rotate, slide, or animate, and its edges
stay crisp and straight. Only the presenter moves: natural explanatory hand
gestures toward the panel, occasional glances at it and back to camera, subtle,
calm body movement.

Camera, STRICT: locked, static, eye-level medium-wide shot that keeps the
presenter on the left and the FULL green panel on the right in frame at all
times. No zoom, pan, tilt, dolly, push-in, orbit, handheld shake, or reframing.
One continuous static shot.
"""

# text: []   first_frame: 1   first_last: 2 (first, last)   reference: 1+ (your image)
# Character + background are references so the model reproduces them exactly.
# The slide is NOT sent to the model — it's composited in post via chroma-key.
IMAGE_URLS = [
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/Character1.png",
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/background2.jpeg",
]

# Video reference — a previously generated video whose format/style you want
# the model to follow. Uploaded the same way as images. Set to "" to skip.
VIDEO_REF_URL = [
    "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/video_library/reference_video_2.mp4"
]

# Post-processing: the slide image to composite into the chroma-keyed green area,
# and the background image to fill any gaps behind the slide.
SLIDE_IMAGE = "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/Diagram2.png"
BG_IMAGE    = "/Users/macbookpro/Documents/HeyHi/Atlas Project/new_code_v2/_storage/image_library/background2.jpeg"

# Chroma-key tuning (HSV range for "green screen" detection)
CHROMA_HUE_RANGE = (30, 90)         # hue range (0-180 in OpenCV HSV)
CHROMA_SAT_MIN   = 30               # minimum saturation
CHROMA_VAL_MIN   = 30               # minimum value/brightness

ASPECT_RATIO = "16:9"               # "16:9","4:3","1:1","3:4","9:16","21:9","adaptive"
RESOLUTION = "480p"                 # "480p" | "720p" | "1080p"
DURATION = 4                        # seconds, 4-30
OUTPUT_FORMAT = "mp4"               # "mp4" | "mov"
GENERATE_AUDIO = True               # True = the presenter speaks / ambient sound

RUN_ID = "14th_greenkey_composite"     # set per run; output -> _storage/kie.ai/run_<RUN_ID>/

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

    # Attach video reference if provided (style/format guide for generation).
    if VIDEO_REF_URL:
        refs = VIDEO_REF_URL if isinstance(VIDEO_REF_URL, list) else [VIDEO_REF_URL]
        resolved_refs = []
        for ref in refs:
            if not ref.startswith(("http://", "https://")):
                ref = upload_local(ref)
            resolved_refs.append(ref)
        task_input["reference_video_urls"] = resolved_refs

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

    # Validate post-processing assets.
    if SLIDE_IMAGE and not Path(SLIDE_IMAGE).expanduser().is_file():
        sys.exit(f"Slide image not found: {SLIDE_IMAGE}")
    if BG_IMAGE and not Path(BG_IMAGE).expanduser().is_file():
        sys.exit(f"Background image not found: {BG_IMAGE}")
    for ref in VIDEO_REF_URL:
        if not ref.startswith(("http://", "https://")) \
                and not Path(ref).expanduser().is_file():
            sys.exit(f"Video reference not found: {ref}")

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


# ---------------------------------------------------------------------------
#  Post-processing: chroma-key the green panel and composite slide + background
# ---------------------------------------------------------------------------

def detect_green_rect(frame):
    """Find the bounding box of the largest green region in *frame* (BGR).

    Returns (x, y, w, h) in pixel coordinates, or None if nothing found.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([CHROMA_HUE_RANGE[0], CHROMA_SAT_MIN, CHROMA_VAL_MIN])
    hi = np.array([CHROMA_HUE_RANGE[1], 255, 255])
    mask = cv2.inRange(hsv, lo, hi)

    # Heavy close to merge the whole panel into one blob, then open to drop
    # tiny green noise elsewhere in the frame.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    biggest = max(contours, key=cv2.contourArea)
    return cv2.boundingRect(biggest)


def _soft_green_mask(frame_bgr, rect):
    """Build a soft (float 0-1) chroma-key mask *only inside rect*.

    Pixels outside the rect are always 0 (= keep original video).
    Inside the rect, green pixels approach 1.0 (= replace with slide/bg),
    with a smooth falloff at edges so there's no hard fringe.

    Returns an (H, W) float32 array in [0, 1].
    """
    h, w = frame_bgr.shape[:2]
    rx, ry, rw, rh = rect

    # Work only on the rect crop for speed.
    crop = frame_bgr[ry:ry + rh, rx:rx + rw]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).astype(np.float32)

    hue = hsv[:, :, 0]        # 0-180
    sat = hsv[:, :, 1]        # 0-255
    val = hsv[:, :, 2]        # 0-255

    hue_lo, hue_hi = CHROMA_HUE_RANGE
    hue_mid = (hue_lo + hue_hi) / 2.0
    hue_half = (hue_hi - hue_lo) / 2.0

    # Smooth falloff: 1.0 at the centre of the hue range, fading to 0 at
    # the edges and beyond.  Same idea for saturation and value.
    hue_score = np.clip(1.0 - np.abs(hue - hue_mid) / hue_half, 0, 1)
    sat_score = np.clip((sat - CHROMA_SAT_MIN) / max(80, 255 - CHROMA_SAT_MIN), 0, 1)
    val_score = np.clip((val - CHROMA_VAL_MIN) / max(80, 255 - CHROMA_VAL_MIN), 0, 1)

    # Multiply the three scores → only confidently-green pixels score high.
    alpha_crop = (hue_score * sat_score * val_score).astype(np.float32)

    # Contrast boost: remap so even faint greens snap toward 1.0.
    # Without this, Seedance's uneven green panel produces alpha ~0.3-0.6
    # which bleeds green into the composite.
    alpha_crop = np.clip((alpha_crop - 0.05) / (0.25 - 0.05), 0, 1)
    alpha_crop = alpha_crop ** 0.35

    # Dilate the mask so it eats a few pixels into the green fringe that
    # sits at the boundary between the panel and the presenter/background.
    dilate_k = max(3, (min(rw, rh) // 80) | 1)
    dilate_el = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_k, dilate_k))
    alpha_crop = cv2.dilate(alpha_crop, dilate_el, iterations=2)

    # Gaussian blur to feather the edges (kills jagged fringe).
    ksize = max(3, (min(rw, rh) // 30) | 1)   # odd kernel, proportional to rect
    alpha_crop = cv2.GaussianBlur(alpha_crop, (ksize, ksize), 0)

    # Embed into full-frame mask (zeros outside the rect).
    mask = np.zeros((h, w), dtype=np.float32)
    mask[ry:ry + rh, rx:rx + rw] = alpha_crop
    return mask


def _despill_green(frame_bgr, rect, band=40):
    """Suppress green spill in a band around the edges of *rect*.

    For each pixel in the band, if the green channel exceeds the average of
    red and blue (i.e. the pixel has a green tint), the green channel is
    pulled down toward that average.  The strength tapers off with distance
    from the rect edge so the correction is invisible further away.
    """
    h, w = frame_bgr.shape[:2]
    rx, ry, rw, rh = rect

    # Expand the rect outward by `band` pixels (clamped to frame).
    x0 = max(rx - band, 0)
    y0 = max(ry - band, 0)
    x1 = min(rx + rw + band, w)
    y1 = min(ry + rh + band, h)

    roi = frame_bgr[y0:y1, x0:x1].astype(np.float32)
    b, g, r = roi[:, :, 0], roi[:, :, 1], roi[:, :, 2]

    # Build a distance-from-rect-edge map within the expanded region.
    ys = np.arange(y0, y1)[:, None]
    xs = np.arange(x0, x1)[None, :]
    xs = xs.astype(np.float32)
    ys = ys.astype(np.float32)
    dx = np.maximum(rx - xs, 0) + np.maximum(xs - (rx + rw), 0)
    dy = np.maximum(ry - ys, 0) + np.maximum(ys - (ry + rh), 0)
    dist = np.sqrt(dx * dx + dy * dy)

    # Inside the rect, distance-from-edge = how far inside. We want the
    # despill to be strongest right at the edge (dist ~0) and taper both
    # inward and outward.
    inside_x = np.clip(xs - rx, 0, rw).astype(np.float32)
    inside_y = np.clip(ys - ry, 0, rh).astype(np.float32)
    in_dist = np.minimum(np.minimum(inside_x, rw - inside_x),
                         np.minimum(inside_y, rh - inside_y))
    # For pixels inside the rect, use the distance from the nearest edge.
    is_inside = (xs >= rx) & (xs < rx + rw) & (ys >= ry) & (ys < ry + rh)
    edge_dist = np.where(is_inside, in_dist, dist)

    # Strength: 1.0 at the edge, fading to 0 at `band` pixels away.
    strength = np.clip(1.0 - edge_dist / band, 0, 1).astype(np.float32)

    # Green excess over average of R and B.
    avg_rb = (r + b) * 0.5
    excess = np.maximum(g - avg_rb, 0)

    # Pull green down proportionally to strength and excess.
    g_new = g - excess * strength
    roi[:, :, 1] = g_new
    frame_bgr[y0:y1, x0:x1] = np.clip(roi, 0, 255).astype(np.uint8)
    return frame_bgr


def composite_video(video_path, slide_path, bg_path, out_path):
    """Chroma-key the green panel and composite three layers.

    The frame is left untouched everywhere except inside the detected green
    rectangle.  Inside that rectangle the layers are (bottom to top):

        1. Background image — scaled to fill the rect, so any gap around the
           slide is the background, not black.
        2. Slide image — aspect-ratio-fit inside the rect, centred.
        3. Original video — blended back in via a soft inverse-green mask so
           edges stay clean and the presenter is never affected.

    Everything outside the rect is the original video frame, untouched.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # --- detect green rect from first frame ---
    ret, first_frame = cap.read()
    if not ret:
        raise RuntimeError("Video has no frames")
    rect = detect_green_rect(first_frame)
    if rect is None:
        print("WARNING: no green rectangle detected — skipping composite")
        cap.release()
        return None
    rx, ry, rw, rh = rect
    print(f"Detected green rect: x={rx} y={ry} w={rw} h={rh}")

    # --- prepare the replacement patch (bg + slide) for the rect region ---
    # Background: scale to cover the full frame (preserving aspect ratio),
    # then crop the rect region out of it — the rect is a "window" into the
    # background, so proportions stay natural and gaps around the slide are
    # filled seamlessly.
    bg_full = cv2.imread(str(bg_path))
    if bg_full is None:
        raise RuntimeError(f"Cannot read background: {bg_path}")
    bgh, bgw = bg_full.shape[:2]
    bg_scale = max(frame_w / bgw, frame_h / bgh)
    scaled_w = max(int(bgw * bg_scale + 0.5), frame_w)
    scaled_h = max(int(bgh * bg_scale + 0.5), frame_h)
    bg_scaled = cv2.resize(bg_full, (scaled_w, scaled_h),
                           interpolation=cv2.INTER_AREA)
    # Centre-crop to exact frame size.
    crop_x = (scaled_w - frame_w) // 2
    crop_y = (scaled_h - frame_h) // 2
    bg_frame = bg_scaled[crop_y:crop_y + frame_h, crop_x:crop_x + frame_w]
    # The rect window into the background.
    bg_patch = bg_frame[ry:ry + rh, rx:rx + rw]

    # Slide: fit inside the rect (aspect-ratio-preserving), centred.
    slide = cv2.imread(str(slide_path))
    if slide is None:
        raise RuntimeError(f"Cannot read slide: {slide_path}")
    sh, sw = slide.shape[:2]
    scale = min(rw / sw, rh / sh) * 0.90
    new_sw, new_sh = int(sw * scale), int(sh * scale)
    slide_resized = cv2.resize(slide, (new_sw, new_sh),
                               interpolation=cv2.INTER_AREA)

    # Paste slide centred onto the bg patch (clamp if rounding made it 1px too big).
    new_sw = min(new_sw, rw)
    new_sh = min(new_sh, rh)
    slide_resized = slide_resized[:new_sh, :new_sw]
    pad_x = (rw - new_sw) // 2
    pad_y = (rh - new_sh) // 2
    replacement = bg_patch.copy()
    replacement[pad_y:pad_y + new_sh, pad_x:pad_x + new_sw] = slide_resized

    # --- set up writer (preserve audio via ffmpeg mux later) ---
    tmp_noaudio = out_path.with_name(out_path.stem + "_noaudio" + out_path.suffix)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_noaudio), fourcc, fps, (frame_w, frame_h))

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    frame_no = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Soft green mask: 1.0 = definitely green, 0.0 = definitely not green,
        # smooth gradient in between.  Only non-zero inside the rect.
        alpha = _soft_green_mask(frame, rect)

        # Blend only inside the rect: composite = alpha * replacement + (1-alpha) * video.
        # Outside the rect alpha is 0, so we just start from the original frame.
        composite = frame.copy()
        a3 = alpha[ry:ry + rh, rx:rx + rw, np.newaxis]   # (rh, rw, 1) for broadcasting
        roi = composite[ry:ry + rh, rx:rx + rw]
        composite[ry:ry + rh, rx:rx + rw] = (
            a3 * replacement.astype(np.float32)
            + (1.0 - a3) * roi.astype(np.float32)
        ).astype(np.uint8)

        # Suppress residual green tint on pixels near the panel edges.
        _despill_green(composite, rect, band=30)

        writer.write(composite)
        frame_no += 1

    cap.release()
    writer.release()
    print(f"Composited {frame_no} frames -> {tmp_noaudio}")

    # --- mux original audio back in ---
    # Brew's ffmpeg-full may link an older x265 dylib than the currently
    # installed version.  Point DYLD_LIBRARY_PATH at all x265 Cellar dirs
    # so the linker can find whichever version ffmpeg was built against.
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
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0?",
            "-shortest",
            str(out_path),
        ], check=True, capture_output=True, timeout=120, env=ffmpeg_env)
        tmp_noaudio.unlink(missing_ok=True)
        print(f"Final composite with audio: {out_path}")
    except FileNotFoundError:
        tmp_noaudio.rename(out_path)
        print(f"ffmpeg not found; saved without audio: {out_path}")
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg mux failed ({exc.returncode}): {exc.stderr[:500] if exc.stderr else ''}")
        tmp_noaudio.rename(out_path)

    return out_path


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

    # --- post-processing: chroma-key composite ---
    if saved and SLIDE_IMAGE and BG_IMAGE:
        print("\n--- post-processing: chroma-key composite ---")
        for video_path in saved:
            out_name = f"{video_path.stem}_composite{video_path.suffix}"
            out_path = video_path.with_name(out_name)
            result = composite_video(
                video_path,
                Path(SLIDE_IMAGE).expanduser(),
                Path(BG_IMAGE).expanduser(),
                out_path,
            )
            if result:
                print(f"  {out_path.name}")

    # Per-task cost is reported by this API directly; balance diff cross-checks it.
    print(f"\ncreditsConsumed (from record): {record.get('creditsConsumed')}")
    after = get_credits()
    print(f"Credits before/after: {before} -> {after}")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        print(f"Balance delta: {before - after}")


if __name__ == "__main__":
    main()
