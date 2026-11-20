"""
app/clients/video_veo_client.py

Wrap the Veo generation call (submit job, return the resulting MP4 bytes). 
Reference-to-video (Fast) attaches reference images (storyboard);
image-to-video (Lite) attaches a single seed image (backgrounds or scenes).

Retry transient (5xx / timeout) failures up to N times with exponential
backoff, and append a per-scene cost record to run_0X/cost_log.jsonl.

"""
from __future__ import annotations

import os
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Protocol

from app.utils.retry import (
    RetriesExhausted, backoff_from_env, looks_transient, retry_call,
)


# ---------------------------------------------------------------- errors
class VeoError(RuntimeError):
    pass


class TransientVeoError(VeoError):
    """Retryable: 5xx, rate-limit, poll timeout."""


# ---------------------------------------------------------------- job spec
@dataclass
class VeoJobSpec:
    scene_id: str
    task: str                              # "image-to-video" | "reference-to-video"
    model: str                             # "Veo 3.1 Fast" | "Veo 3.1 Lite"
    prompt: str
    aspect_ratio: str = "16:9"
    duration_s: float | None = None        # required for B-Roll; None lets Veo decide
    image: bytes | None = None             # seed for image-to-video
    references: list[bytes] = field(default_factory=list)  # for reference-to-video
    safety: str = "Allow (Adult only)"
    reference_video: Path | None = None    # H3 only: motion/look reference clip
    seed: int | None = None                # H3 only: fixed seed (None = random, logged)
    turbo: bool | None = None              # H3 only: turbo LoRA (None = server default)


class VeoClient(Protocol):
    def generate(self, spec: VeoJobSpec) -> bytes: ...


# ---------------------------------------------------------------- reliability knobs
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_S = 2.0
DEFAULT_POLL_S = 10.0
DEFAULT_TIMEOUT_S = 600.0

# Veo only accepts these clip lengths.
VEO_ALLOWED_SECONDS = (4, 6, 8)


def snap_duration(seconds: float) -> float:
    """Snap a requested length to Veo's nearest allowed clip length (>=)."""
    return float(next((d for d in VEO_ALLOWED_SECONDS if seconds <= d), VEO_ALLOWED_SECONDS[-1]))


def retry_backoff_from_env() -> float:
    return backoff_from_env("ATLAS_VEO_BACKOFF", DEFAULT_RETRY_BACKOFF_S)


def poll_s_from_env() -> float:
    return float(os.environ.get("ATLAS_VEO_POLL_S", DEFAULT_POLL_S))


def generate_with_retry(
    client: VeoClient,
    spec: VeoJobSpec,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
) -> tuple[bytes, int]:
    """Return (mp4_bytes, attempts). Retries ONLY TransientVeoError.
    Here max_retries is the TOTAL attempt count (historical meaning)."""
    try:
        return retry_call(lambda: client.generate(spec), attempts=max_retries,
                          exc_type=TransientVeoError, backoff_s=backoff_s)
    except RetriesExhausted as ex:
        raise VeoError(
            f"scene '{spec.scene_id}': Veo failed after {max_retries} attempts: {ex.last}")


# ---------------------------------------------------------------- Gemini/Veo
class GeminiVeoClient:
    """submit -> poll(every poll_s) -> download MP4 bytes via google-genai."""

    # VERIFY these ids against your google-genai / Veo access.
    _MODEL_IDS = {
        "Veo 3.1 Fast": "veo-3.1-fast-generate-preview",
        "Veo 3.1 Lite": "veo-3.1-lite-generate-preview",
    }

    # safety string (claude.md) -> Veo person_generation. VERIFY field/values.
    _PERSON_GENERATION = {
        "Allow (Adult only)": "allow_adult",
        "Allow (All ages)": "allow_all",
        "Don't allow": "dont_allow",
    }

    def __init__(self, client, poll_s: float = DEFAULT_POLL_S, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.client = client
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    def generate(self, spec: VeoJobSpec) -> bytes:
        from google.genai import types

        model_id = self._MODEL_IDS.get(spec.model)
        if not model_id:
            raise VeoError(f"unknown Veo model '{spec.model}'")

        cfg = types.GenerateVideosConfig(
            aspect_ratio=spec.aspect_ratio,
            duration_seconds=int(snap_duration(spec.duration_s)) if spec.duration_s else None,
            person_generation=self._PERSON_GENERATION.get(spec.safety, "allow_adult"),
        )
        image = types.Image(image_bytes=spec.image, mime_type="image/png") if spec.image else None

        # reference-to-video: each reference must be wrapped in a
        # VideoGenerationReferenceImage with a reference_type ("asset").
        if spec.references:
            cfg.reference_images = [
                types.VideoGenerationReferenceImage(
                    image=types.Image(image_bytes=r, mime_type="image/png"),
                    reference_type="asset",
                )
                for r in spec.references
            ]

        try:
            op = self.client.models.generate_videos(
                model=model_id, prompt=spec.prompt, image=image, config=cfg,
            )
            waited = 0.0
            while not op.done:
                if waited >= self.timeout_s:
                    raise TransientVeoError(f"poll timeout after {self.timeout_s}s")
                time.sleep(self.poll_s)
                waited += self.poll_s
                op = self.client.operations.get(op)
            if getattr(op, "error", None):
                raise VeoError(f"scene '{spec.scene_id}': Veo operation error: {op.error}")
            resp = op.response
            vids = getattr(resp, "generated_videos", None) if resp else None
            if not vids:
                # Veo completed but returned no clip (safety reasons)
                reason = (getattr(resp, "rai_media_filtered_reasons", None)
                          or getattr(resp, "rai_media_filtered_count", None)
                          or "no generated_videos in response") if resp else "empty response"
                raise VeoError(f"scene '{spec.scene_id}': Veo returned no video (filtered/blocked): {reason}")
            video = vids[0].video
            data = getattr(video, "video_bytes", None)
            if data is None:
                data = self.client.files.download(file=video)
            return data
        except TransientVeoError:
            raise
        except Exception as e:                         # classify 5xx/rate-limit as transient
            if looks_transient(e):
                raise TransientVeoError(str(e)) from e
            raise VeoError(str(e)) from e