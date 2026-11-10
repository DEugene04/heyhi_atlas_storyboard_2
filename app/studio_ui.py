"""
app/studio_ui.py — static front-end for the default (/pipeline/*) API.

Two routers, both pure file serving:
  landing_router   GET /          mode picker: Default (studio) vs Presentation (pdf-review)
  router           /studio/...    the default-API UI

Same allow-list approach as app/pdf_review.py: only named files are served, so
no path from the URL ever reaches the filesystem.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.paths import REPO_ROOT

LANDING_ROOT = REPO_ROOT / "web" / "landing"
STUDIO_ROOT = REPO_ROOT / "web" / "studio"

_MEDIA = {".css": "text/css", ".js": "text/javascript"}
_STUDIO_ASSETS = {"styles.css", "app.js"}

landing_router = APIRouter(include_in_schema=False)
router = APIRouter(prefix="/studio", tags=["Default pipeline UI"])


@landing_router.get("/")
def landing() -> FileResponse:
    return FileResponse(LANDING_ROOT / "index.html", media_type="text/html")


@router.get("/")
def studio_index() -> FileResponse:
    return FileResponse(STUDIO_ROOT / "index.html", media_type="text/html")


@router.get("/ui/{filename}")
def studio_asset(filename: str) -> FileResponse:
    if filename not in _STUDIO_ASSETS:
        raise HTTPException(404, "unknown UI asset")
    suffix = filename[filename.rfind("."):]
    return FileResponse(STUDIO_ROOT / filename, media_type=_MEDIA[suffix])
