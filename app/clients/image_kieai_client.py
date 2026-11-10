"""
app/clients/image_kieai_client.py

kie.ai Google Nano Banana image client for the PDF-review flow.

Text-to-image (google/nano-banana) and image editing (google/nano-banana-edit)
via kie.ai's unified Market "jobs" API (createTask / recordInfo).  The request /
response / auth contract mirrors bench/test_image_kieai.py (the source of truth).

*** LIVE CALLS CONSUME kie.ai CREDITS and EGRESS DATA TO A THIRD-PARTY HOST.
This client is only invoked from an explicit, user-triggered generate call. ***
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

GENERATE_MODEL = "google/nano-banana"
EDIT_MODEL = "google/nano-banana-edit"


class ImageKieError(RuntimeError):
    """Any kie.ai image generation failure."""


class ImageKieClient:
    """Minimal Nano Banana client over kie.ai's jobs API.

    Reads KIE_API_KEY at construction; the key is never printed or persisted.
    """

    def __init__(self, api_key_env: str = "KIE_API_KEY",
                 poll_s: float = 5.0, timeout_s: float = 900.0):
        key = os.environ.get(api_key_env)
        if not key:
            raise ImageKieError(f"{api_key_env} not set in environment")
        self._key = key
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.poll_s = poll_s
        self.timeout_s = timeout_s

    def upload_local(self, path: Path) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ImageKieError(f"image not found: {p}")
        with p.open("rb") as fh:
            r = requests.post(
                UPLOAD_ENDPOINT,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (p.name, fh)},
                data={"uploadPath": "nano-banana-inputs"},
                timeout=120,
            )
        r.raise_for_status()
        url = (r.json().get("data") or {}).get("downloadUrl")
        if not url:
            raise ImageKieError(f"upload returned no downloadUrl: {r.text}")
        return url

    def _create_task(self, task_input: dict[str, Any], model: str) -> str:
        payload = {"model": model, "input": task_input}
        r = requests.post(f"{BASE_URL}/api/v1/jobs/createTask",
                          headers=self._headers, json=payload, timeout=30)
        r.raise_for_status()
        task_id = (r.json().get("data") or {}).get("taskId")
        if not task_id:
            raise ImageKieError(f"no taskId returned: {r.text}")
        return task_id

    def _poll(self, task_id: str) -> dict[str, Any]:
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
                raise ImageKieError(
                    f"task {task_id} failed: {data.get('failMsg') or data.get('failCode')}")
            time.sleep(self.poll_s)
        raise ImageKieError(f"task {task_id} did not finish within {self.timeout_s}s")

    @staticmethod
    def result_urls(record: dict[str, Any]) -> list[str]:
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
        *,
        output_format: str = "png",
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        """Text-to-image via google/nano-banana.

        Returns {image_bytes, credits_consumed, task_id, result_url, record}.
        """
        task_input = {
            "prompt": prompt,
            "output_format": output_format,
            "aspect_ratio": aspect_ratio,
        }
        task_id = self._create_task(task_input, model=GENERATE_MODEL)
        record = self._poll(task_id)
        urls = self.result_urls(record)
        if not urls:
            raise ImageKieError(f"task {task_id} finished with no result images")
        image = requests.get(urls[0], timeout=180)
        image.raise_for_status()
        return {
            "image_bytes": image.content,
            "credits_consumed": record.get("creditsConsumed"),
            "task_id": task_id,
            "result_url": urls[0],
            "record": record,
        }

    def edit(
        self,
        prompt: str,
        image_urls: list[str],
        *,
        output_format: str = "png",
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        """Image editing via google/nano-banana-edit.

        Returns {image_bytes, credits_consumed, task_id, result_url, record}.
        """
        if not image_urls:
            raise ImageKieError("edit requires at least one image_url")
        task_input = {
            "prompt": prompt,
            "image_urls": image_urls,
            "output_format": output_format,
            "aspect_ratio": aspect_ratio,
        }
        task_id = self._create_task(task_input, model=EDIT_MODEL)
        record = self._poll(task_id)
        urls = self.result_urls(record)
        if not urls:
            raise ImageKieError(f"task {task_id} finished with no result images")
        image = requests.get(urls[0], timeout=180)
        image.raise_for_status()
        return {
            "image_bytes": image.content,
            "credits_consumed": record.get("creditsConsumed"),
            "task_id": task_id,
            "result_url": urls[0],
            "record": record,
        }
