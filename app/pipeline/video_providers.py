"""
app/pipeline/video_providers.py

Layer 5 video provider registry.

Maps a provider name to a VeoClient builder so the pipeline can select a video
backend without provider-specific branching.

Add a new provider by implementing VeoClient and registering it in
VIDEO_PROVIDERS.
"""
from __future__ import annotations

from typing import Callable

from app.clients.video_veo_client import GeminiVeoClient, VeoClient
from app.clients.video_minimax_h3_client import MinimaxH3SceneClient


def _build_gemini(gemini) -> VeoClient:
    return GeminiVeoClient(gemini)


def _build_minimax_h3(gemini) -> VeoClient:   # gemini arg unused: reads MINIMAX_H3_URL
    return MinimaxH3SceneClient()


VIDEO_PROVIDERS: dict[str, Callable[[object], VeoClient]] = {
    "minimax_h3": _build_minimax_h3,   # default: company GPU server
    "gemini": _build_gemini,
}