"""
app/clients/video_seedance_kieai_client.py

kie.ai ByteDance Seedance 2.5 video client for the PDF-review flow.

Seedance 2.5 runs on kie.ai's unified Market "jobs" API (createTask / recordInfo),
NOT the Veo endpoint used by app/clients/video_kieai_client.py. The request /
response / auth contract mirrors bench/test_video_kieai.py (the source of truth).

*** LIVE CALLS CONSUME kie.ai CREDITS and EGRESS PROJECT FRAMES TO A THIRD-PARTY
HOST. This client is only invoked from an explicit, user-triggered generate call. ***
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.kie.ai"
UPLOAD_ENDPOINT = "https://kieai.redpandaai.co/api/file-stream-upload"
DEFAULT_MODEL = "bytedance/seedance-2-mini"
ALLOWED_MODELS = {"bytedance/seedance-2-mini", "bytedance/seedance-2-5"}


class SeedanceError(RuntimeError):
    """Any Seedance/kie.ai failure (configuration, submit, poll, or generation)."""


class SeedanceKieClient:
    """Minimal Seedance 2.5 client over kie.ai's jobs API.

    Reads KIE_API_KEY at construction and nowhere else; the key is never printed
    or persisted (mirrors test_video_kieai).
    """

    def __init__(self, api_key_env: str = "KIE_API_KEY",
                 poll_s: float = 15.0, timeout_s: float = 1200.0):
        key = os.environ.get(api_key_env)
        if not key:
            raise SeedanceError(f"{api_key_env} not set in environment")
        self._key = key
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    # -- account credit balance (for before/after cost cross-check) --
    def get_credits(self) -> Any:
        r = requests.get(f"{BASE_URL}/api/v1/chat/credit", headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json().get("data")

    # -- local file -> kie temp host URL (kie fetches the URL server-side) --
    def upload_local(self, path: Path) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            raise SeedanceError(f"image not found: {p}")
        with p.open("rb") as fh:
            # Multipart: requests sets the boundary itself; don't send the JSON header.
            r = requests.post(
                UPLOAD_ENDPOINT,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (p.name, fh)},
                data={"uploadPath": "veo-inputs"},
                timeout=120,
            )
        r.raise_for_status()
        url = (r.json().get("data") or {}).get("downloadUrl")
        if not url:
            raise SeedanceError(f"upload returned no downloadUrl: {r.text}")
        return url

    def resolve_image_urls(self, entries: list[str | Path]) -> list[str]:
        """Pass http(s) URLs through unchanged; upload everything else as a local file."""
        resolved = []
        for entry in entries:
            text = str(entry)
            resolved.append(text if text.startswith(("http://", "https://")) else self.upload_local(entry))
        return resolved

    # -- submit a jobs/createTask request; returns the taskId --
    def create_task(self, payload: dict[str, Any]) -> str:
        if payload.get("model") not in ALLOWED_MODELS:
            raise SeedanceError(f"unknown model: {payload.get('model')}")
        r = requests.post(f"{BASE_URL}/api/v1/jobs/createTask",
                          headers=self._headers, json=payload, timeout=30)
        r.raise_for_status()
        task_id = (r.json().get("data") or {}).get("taskId")
        if not task_id:
            raise SeedanceError(f"no taskId returned: {r.text}")
        return task_id

    # -- poll jobs/recordInfo until the task finishes; returns the final record --
    def poll(self, task_id: str) -> dict[str, Any]:
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            r = requests.get(f"{BASE_URL}/api/v1/jobs/recordInfo",
                             headers=self._headers, params={"taskId": task_id}, timeout=30)
            r.raise_for_status()
            data = r.json().get("data") or {}
            state = data.get("state")
            if state == "success":
                return data
            if state == "fail":
                raise SeedanceError(
                    f"task {task_id} failed: {data.get('failMsg') or data.get('failCode')}")
            time.sleep(self.poll_s)
        raise SeedanceError(f"task {task_id} did not finish within {self.timeout_s}s")

    @staticmethod
    def result_urls(record: dict[str, Any]) -> list[str]:
        """resultJson is a JSON *string* — parse it and pull resultUrls."""
        raw = record.get("resultJson")
        if not raw:
            return []
        try:
            return (json.loads(raw) or {}).get("resultUrls") or []
        except (json.JSONDecodeError, TypeError):
            return []

    def generate(
        self,
        prompt: str,
        image_paths: list[Path],
        *,
        duration: int = 8,
        aspect_ratio: str = "16:9",
        resolution: str = "480p",
        generate_audio: bool = True,
        model: str = DEFAULT_MODEL,
        video_ref_paths: list[Path | str] | None = None,
    ) -> dict[str, Any]:
        """Run one reference-mode Seedance job end to end.

        Uploads the reference images, submits, polls, downloads the first result
        video, and returns {video_bytes, credits_consumed, task_id, result_url, request, record}
        where `request` is the exact createTask body that was submitted.
        """
        reference_urls = self.resolve_image_urls(image_paths)
        task_input: dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "duration": int(duration),
            "output_format": "mp4",
            "generate_audio": generate_audio,
        }
        if reference_urls:
            task_input["reference_image_urls"] = reference_urls
        if video_ref_paths:
            task_input["reference_video_urls"] = self.resolve_image_urls(video_ref_paths)

        request = {"model": model, "input": task_input}
        task_id = self.create_task(request)
        record = self.poll(task_id)
        urls = self.result_urls(record)
        if not urls:
            raise SeedanceError(f"task {task_id} finished with no result videos")
        video = requests.get(urls[0], timeout=180)
        video.raise_for_status()
        return {
            "video_bytes": video.content,
            "credits_consumed": record.get("creditsConsumed"),
            "task_id": task_id,
            "result_url": urls[0],
            "request": request,
            "record": record,
        }
