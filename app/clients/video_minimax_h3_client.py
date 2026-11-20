"""
app/clients/video_minimax_h3_client.py

MiniMax H3 video client for the company GPU server (stable-diffusion.cpp
`sd-server`, reached over Tailscale). Nothing leaves the company network.

Server API (native async):
  POST /sdcpp/v1/vid_gen        JSON body, 202 + {id, poll_url}
  GET  <poll_url>               status: queued | generating | completed | failed | cancelled
  POST /sdcpp/v1/jobs/{id}/cancel
The finished job carries the clip as base64 WebM (`result.b64_json`), which is
transcoded here to H.264/AAC mp4 so every later layer keeps receiving mp4.

Tasks map onto the loaded checkpoint:
  reference images -> ref_images   (Ref2VA checkpoint, MINIMAX_H3_URL)
  reference video  -> ref_videos   (Ref2VA; first ATLAS_H3_REF_VIDEO_SECONDS at 24 fps)
  first frame      -> init_image   (FL2VA checkpoint, MINIMAX_H3_FL2VA_URL, defaults to MINIMAX_H3_URL)
  neither          -> text-to-video (MINIMAX_H3_URL)

Env:
  MINIMAX_H3_URL          base URL of the Ref2VA server, e.g. http://100.104.69.66:8001
  MINIMAX_H3_FL2VA_URL    base URL of the FL2VA server (optional)
  ATLAS_H3_RESOLUTION     short side, e.g. "480P" (default) or "768P"
  ATLAS_H3_STEPS          sampling steps (default 8; 4 smears clips longer than a few seconds)
  ATLAS_H3_TIMEOUT_S      per-clip wall-clock limit (default 1800)
  ATLAS_H3_REF_VIDEO_SECONDS  reference-video seconds sent (default 2; every frame is attended each step)
  ATLAS_H3_COMPRESSION    WebM quality 1-100 (default 50); lower = smaller download
  ATLAS_H3_TURBO          "1" makes turbo the default for clips that do not choose (default off)
  ATLAS_H3_TURBO_LORA_FL2V / _REF2V   turbo LoRA file names inside the server's --lora-model-dir

Turbo mode (per clip, `turbo=True`): the task's distillation LoRA is sent in the
structured `lora` field, with 4 steps and flow shift 6. The sd-server must be started
with `--lora-model-dir` pointing at the LoRA files. Text-to-video has no LoRA and
always runs the normal path.
"""
from __future__ import annotations

import base64
import io
import math
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from app.clients.video_veo_client import TransientVeoError, VeoError, VeoJobSpec
from app.utils.ffmpeg_utils import FfmpegError, run_ffmpeg
from app.utils.retry import looks_transient

MODEL_NAME = "minimax-h3"
MIN_DURATION_S = 4
MAX_DURATION_S = 15
MAX_REFERENCE_IMAGES = 9
FPS = 24                                   # fixed by the model
ASPECT_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
DEFAULT_RESOLUTION = "480P"
DEFAULT_STEPS = 8   # 4 steps visibly degrades 12 s clips (smeared presenter, ghosting); 8 is clean
DEFAULT_COMPRESSION = 50
TURBO_STEPS = 4
TURBO_FLOW_SHIFT = 6.0
DEFAULT_TURBO_LORAS = {                    # task -> file in the server's --lora-model-dir
    "first-frame-to-video": "minimax_h3_fl2v_turbo_4step_v1.3_768p_bf16.safetensors",
    "reference-to-video": "minimax_h3_ref2v_turbo_4step_v1.0_768p_bf16.safetensors",
}
DEFAULT_REF_VIDEO_SECONDS = 2.0            # reference-video seconds sent (24 fps frames; costly per step)
FETCH_ATTEMPTS = 6                         # re-fetches of a finished job (server keeps it ~10 min)
DEFAULT_TIMEOUT_S = 1800
POLL_INTERVAL_S = 3.0
_ACTIVE = ("queued", "generating")


class MinimaxH3Error(RuntimeError):
    """Non-retryable H3 failure (bad input, failed job, timeout)."""


def frame_count(seconds: int) -> int:
    """Frames for `seconds` of 24 fps video, on the model's 17k+5 grid."""
    k = max(0, math.ceil((seconds * FPS - 5) / 17))
    return 17 * k + 5


def frame_size(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    """(width, height) for an aspect ratio and a short-side resolution like '480P'.

    Both sides are floored to multiples of 32, as the model requires."""
    m = re.fullmatch(r"(\d{3,4})[Pp]", resolution)
    if not m or not 256 <= int(m.group(1)) <= 1080:
        raise MinimaxH3Error(f"unsupported resolution: {resolution}")
    short = int(m.group(1))
    if aspect_ratio not in ASPECT_RATIOS:
        raise MinimaxH3Error(f"unsupported aspect_ratio: {aspect_ratio}")
    a, b = (int(x) for x in aspect_ratio.split(":"))
    if a >= b:
        w, h = short * a / b, short
    else:
        w, h = short, short * b / a
    return int(w // 32 * 32), int(h // 32 * 32)


def _raise_for_status(r: requests.Response) -> None:
    """raise_for_status, but keep the server's error body: sd-server explains a 503
    (model_not_loaded / model_loading) only there."""
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} from {r.url}: {r.text[:300]}", response=r)


def _encode_image(path: Path | str, max_side: int) -> str:
    """Base64 JPEG for the server: 3 channels (it rejects alpha), longest side
    capped at `max_side` (the model works at the output resolution, so a larger
    reference only costs upload time over the slow Tailscale relay)."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _encode_video_frames(path: Path | str, seconds: float, max_side: int) -> list[str]:
    """Base64 JPEG frames of the first `seconds` of a reference video.

    The server treats a reference video as a frame sequence at 24 fps and
    attends it at every sampling step, so the clip is resampled to 24 fps, cut
    short and scaled to the output size to keep GPU memory and upload time down.
    """
    with tempfile.TemporaryDirectory() as d:
        pattern = Path(d) / "f_%04d.jpg"
        try:
            run_ffmpeg(["-i", path, "-t", f"{seconds}", "-an",
                        "-vf", f"fps={FPS},scale={max_side}:{max_side}:"
                               "force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-q:v", "2", pattern],
                       expect=Path(d) / "f_0001.jpg", label="h3 reference video frames")
        except FfmpegError as e:
            raise MinimaxH3Error(str(e)) from e
        return [base64.b64encode(f.read_bytes()).decode("ascii")
                for f in sorted(Path(d).glob("f_*.jpg"))]


def _webm_to_mp4(webm: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "in.webm", Path(d) / "out.mp4"
        src.write_bytes(webm)
        try:
            run_ffmpeg(["-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                        "-c:a", "aac", "-movflags", "+faststart", dst],
                       expect=dst, label="h3 webm->mp4")
        except FfmpegError as e:
            raise MinimaxH3Error(str(e)) from e
        return dst.read_bytes()


class MinimaxH3Client:
    cost_provider = "local-gpu"
    cost_model = MODEL_NAME

    def __init__(self, *, base_url: str | None = None, fl2va_url: str | None = None,
                 resolution: str | None = None, steps: int | None = None,
                 timeout_s: float | None = None, compression: int | None = None,
                 turbo: bool | None = None):
        url = base_url or os.environ.get("MINIMAX_H3_URL")
        if not url:
            raise MinimaxH3Error("MINIMAX_H3_URL is not set (e.g. http://100.104.69.66:8001)")
        self.base_url = url.rstrip("/")
        self.fl2va_url = (fl2va_url or os.environ.get("MINIMAX_H3_FL2VA_URL") or url).rstrip("/")
        self.resolution = resolution or os.environ.get("ATLAS_H3_RESOLUTION", DEFAULT_RESOLUTION)
        self.steps = int(steps or os.environ.get("ATLAS_H3_STEPS", DEFAULT_STEPS))
        self.ref_video_seconds = float(os.environ.get("ATLAS_H3_REF_VIDEO_SECONDS", DEFAULT_REF_VIDEO_SECONDS))
        self.compression = int(compression or os.environ.get("ATLAS_H3_COMPRESSION", DEFAULT_COMPRESSION))
        self.timeout_s = float(timeout_s or os.environ.get("ATLAS_H3_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.turbo = (os.environ.get("ATLAS_H3_TURBO", "0") == "1") if turbo is None else turbo
        self.turbo_loras = {
            "first-frame-to-video": os.environ.get("ATLAS_H3_TURBO_LORA_FL2V",
                                                   DEFAULT_TURBO_LORAS["first-frame-to-video"]),
            "reference-to-video": os.environ.get("ATLAS_H3_TURBO_LORA_REF2V",
                                                 DEFAULT_TURBO_LORAS["reference-to-video"]),
        }
        # The GPU server is on the private Tailscale network. A system/corporate HTTP
        # proxy cannot reach it and answers 503, so this session must ignore proxies.
        self._http = requests.Session()
        self._http.trust_env = False

    def generate(
        self,
        prompt: str,
        image_paths: list[Path],
        *,
        reference_video: Path | str | None = None,
        first_frame: Path | str | None = None,
        duration: int = 8,
        aspect_ratio: str = "16:9",
        resolution: str | None = None,
        seed: int | None = None,
        turbo: bool | None = None,
    ) -> dict[str, Any]:
        """Run one H3 job end to end.

        First-frame (image-to-video) when `first_frame` is given, reference-to-video
        when there are reference images, text-to-video otherwise. The clip carries a
        stereo audio track. Returns {video_bytes, job_id, task, request, record,
        generation_seconds}.
        """
        if not 1 <= len(prompt) <= 7000:
            raise MinimaxH3Error(f"prompt must be 1-7000 characters, got {len(prompt)}")
        if not MIN_DURATION_S <= int(duration) <= MAX_DURATION_S:
            raise MinimaxH3Error(f"duration must be {MIN_DURATION_S}-{MAX_DURATION_S} seconds, got {duration}")
        if len(image_paths) > MAX_REFERENCE_IMAGES:
            raise MinimaxH3Error(f"at most {MAX_REFERENCE_IMAGES} reference images, got {len(image_paths)}")
        if first_frame and (image_paths or reference_video):
            raise MinimaxH3Error("first_frame and reference images/video are mutually exclusive")

        # A concrete seed (never -1) so every clip can be reproduced from its manifest.
        seed = random.randint(0, 2**31 - 1) if seed is None else int(seed)
        width, height = frame_size(aspect_ratio, resolution or self.resolution)
        body: dict[str, Any] = {
            "prompt": prompt, "negative_prompt": "",
            "width": width, "height": height,
            "video_frames": frame_count(int(duration)), "fps": FPS,
            "seed": seed, "output_format": "webm", "output_compression": self.compression,
            "sample_params": {"sample_steps": self.steps, "guidance": {"txt_cfg": 1.0}},
        }
        if first_frame:
            task, base = "first-frame-to-video", self.fl2va_url
            body["init_image"] = _encode_image(first_frame, max(width, height))
        elif image_paths or reference_video:
            task, base = "reference-to-video", self.base_url
            if image_paths:
                body["ref_images"] = [_encode_image(p, max(width, height)) for p in image_paths]
            if reference_video:
                frames = _encode_video_frames(reference_video, self.ref_video_seconds, max(width, height))
                body["ref_videos"] = [frames]
        else:
            task, base = "text-to-video", self.base_url

        # Turbo: task LoRA + 4 steps. Text-to-video has no LoRA, so it stays normal.
        lora = self.turbo_loras.get(task) if (self.turbo if turbo is None else turbo) else None
        if lora:
            body["lora"] = [{"path": lora, "multiplier": 1.0, "is_high_noise": False}]
            body["sample_params"]["sample_steps"] = TURBO_STEPS
            body["sample_params"]["flow_shift"] = TURBO_FLOW_SHIFT

        started = time.monotonic()
        job = self._submit(base, body)
        record = self._wait(base, job)
        result = record.get("result") or {}
        if not result.get("b64_json"):
            raise MinimaxH3Error(f"job {job['id']} completed with no video")
        video = _webm_to_mp4(base64.b64decode(result["b64_json"]))
        # The submitted request without the (large) base64 images, for the manifest.
        logged = {k: v for k, v in body.items() if k not in ("init_image", "ref_images", "ref_videos")}
        logged["reference_video_frames"] = len(body["ref_videos"][0]) if reference_video else 0
        logged["task"] = task
        logged["turbo"] = bool(lora)
        logged["reference_image_count"] = len(image_paths)
        return {
            "video_bytes": video,
            "job_id": job["id"],
            "task": task,
            "seed": seed,
            "turbo": bool(lora),
            "request": logged,
            "record": {k: v for k, v in record.items() if k != "result"}
                      | {"result": {k: v for k, v in result.items() if k != "b64_json"}},
            "generation_seconds": round(time.monotonic() - started, 2),
        }

    def _submit(self, base: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._http.post(f"{base}/sdcpp/v1/vid_gen", json=body, timeout=120)
        _raise_for_status(r)
        return r.json()

    def _poll(self, url: str) -> dict[str, Any]:
        """GET a job record. The finished record carries the whole clip, so over a slow
        link that one transfer can break mid-body; the job is still on the server, so
        re-fetch it instead of failing the scene."""
        for attempt in range(FETCH_ATTEMPTS):
            try:
                r = self._http.get(url, timeout=60)
                _raise_for_status(r)
                return r.json()
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                if attempt == FETCH_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 * (attempt + 1), 8))
        raise AssertionError("unreachable")

    def _wait(self, base: str, job: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        url = base + job["poll_url"]
        while True:
            rec = self._poll(url)
            status = rec.get("status")
            if status == "completed":
                return rec
            if status not in _ACTIVE:
                err = rec.get("error") or {}
                raise MinimaxH3Error(f"job {job['id']} {status}: {err.get('message') or err}")
            if time.monotonic() > deadline:
                try:
                    self._http.post(f"{base}/sdcpp/v1/jobs/{job['id']}/cancel", timeout=30)
                except requests.RequestException:
                    pass
                raise MinimaxH3Error(f"job {job['id']} exceeded {self.timeout_s:.0f}s and was cancelled")
            time.sleep(POLL_INTERVAL_S)


class MinimaxH3SceneClient(MinimaxH3Client):
    """Layer 5 video provider: satisfies the VeoClient protocol (VeoJobSpec -> mp4 bytes),
    so routing, retry and cost logging in scene_gen stay as they are.

    spec.task maps onto H3: image-to-video -> init_image, reference-to-video ->
    ref_images. H3 takes any whole duration of 4-15 s, so there is no snapping to
    Veo's 4/6/8 s steps. Its audio track is kept: L6 reads it for word timings and
    then replaces it.
    """

    def generate(self, spec: VeoJobSpec) -> bytes:        # type: ignore[override]
        if spec.task not in ("image-to-video", "reference-to-video"):
            raise VeoError(f"H3: unsupported task '{spec.task}'")
        seconds = min(MAX_DURATION_S, max(MIN_DURATION_S, math.ceil(spec.duration_s or 8)))
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                first = None
                if spec.image:
                    first = tmp / "first.png"
                    first.write_bytes(spec.image)
                refs = []
                for i, blob in enumerate(spec.references):
                    p = tmp / f"ref{i}.png"
                    p.write_bytes(blob)
                    refs.append(p)
                out = MinimaxH3Client.generate(
                    self, spec.prompt, refs, first_frame=first, duration=seconds,
                    aspect_ratio=spec.aspect_ratio,
                    reference_video=spec.reference_video, seed=spec.seed,
                    turbo=spec.turbo,
                )
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if (code and 500 <= code < 600) or looks_transient(e):
                raise TransientVeoError(str(e)) from e
            raise VeoError(str(e)) from e
        except MinimaxH3Error as e:
            raise VeoError(f"scene '{spec.scene_id}': {e}") from e
        return out["video_bytes"]
