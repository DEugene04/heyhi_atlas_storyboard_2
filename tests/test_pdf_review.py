"""Pure endpoint checks for the review-only PDF workflow.

No OpenAI, Gemini, Kie, S3, or video call is made here.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pymupdf
from fastapi.testclient import TestClient

import app.pdf_review as review
from main import app


def _pdf_bytes() -> bytes:
    doc = pymupdf.open()
    first = doc.new_page(width=960, height=540)
    first.insert_text((70, 90), "Training overview", fontsize=28)
    first.insert_text((70, 145), "A useful diagram and its key idea", fontsize=16)
    second = doc.new_page(width=960, height=540)
    second.insert_text((70, 90), "Key takeaway", fontsize=28)
    second.insert_text((70, 145), "Review the source before acting", fontsize=16)
    return doc.tobytes()


def test_pdf_review_ui_is_served() -> None:
    response = TestClient(app).get("/pdf-review/")
    assert response.status_code == 200
    assert "Video generation is intentionally disabled" in response.text


def test_local_review_flow_stops_at_locked_preflight(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    client = TestClient(app)

    imported = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("training.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    )
    assert imported.status_code == 200
    source = imported.json()
    assert source["page_count"] == 2
    assert source["video_generation_enabled"] is False
    project_id = source["project_id"]

    draft = client.post(
        "/pdf-review/draft-local",
        json={
            "project_id": project_id,
            "selected_pages": [1, 2],
            "objective": "Explain the deck",
            "language": "English",
            "target_seconds": 75,
        },
    )
    assert draft.status_code == 200
    assert draft.json()["mode"] == "local_extract"

    crops = client.post(
        "/pdf-review/apply-crops",
        json={
            "project_id": project_id,
            "crops": [
                {"page": 1, "x": 0.05, "y": 0.05, "width": 0.9, "height": 0.9, "label": "Overview"}
            ],
        },
    )
    assert crops.status_code == 200
    assert crops.json()["crops"][0]["pixel_width"] > 0

    preflight = client.post(
        "/pdf-review/preflight",
        json={
            "project_id": project_id,
            "avatar_url": "",
            "scenes": [
                {
                    "id": "scene_01",
                    "page": 1,
                    "duration_s": 8,
                    "narration": "Welcome to the training.",
                    "prompt": "Presenter points to the source visual.",
                    "crop_path": "crops/slide_001.png",
                }
            ],
        },
    )
    assert preflight.status_code == 200
    manifest = preflight.json()
    assert manifest["video_generation_enabled"] is False
    assert manifest["verified_provider_model"] == "bytedance/seedance-2"


def test_local_draft_condenses_a_large_selection() -> None:
    slides = [
        {
            "page": page,
            "title": f"Slide {page}",
            "text": f"Content {page}",
            "visual_score": page % 4,
        }
        for page in range(1, 102)
    ]
    body = review.DraftIn(
        project_id="pdf_deadbeef",
        selected_pages=list(range(1, 102)),
        target_seconds=75,
    )

    result = review._local_draft(body, slides)

    assert 1 <= len(result["sections"]) <= 8
    assert sum(section["duration_s"] for section in result["sections"]) <= 120


def test_url_import_rejects_non_s3_host() -> None:
    response = TestClient(app).post(
        "/pdf-review/import-url",
        json={"pdf_url": "https://example.com/training.pdf"},
    )
    assert response.status_code == 422


def test_openai_client_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        review._openai_client()
    except RuntimeError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("missing OpenAI key should fail before any API call")


def test_openai_script_visual_selection_and_crop_use_structured_responses(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    script = review._AIScript(
        title="Source-grounded training",
        opening="Welcome.",
        sections=[
            review._AIScriptSection(
                page=1,
                heading="Overview",
                narration="Explain the source diagram.",
                visual_direction="Point to the source visual.",
                duration_s=10,
            )
        ],
        closing="Review the source.",
    )
    crop = review._AICrop(
        x=0.1,
        y=0.2,
        width=0.7,
        height=0.6,
        label="Source diagram",
        rationale="Keeps the useful visual and labels.",
    )
    visual_selection = review._AIVisualSelection(
        choices=[
            review._AIVisualChoice(
                page=1,
                scene_ids=["scene_01"],
                label="Overview diagram",
                rationale="It directly supports the scene narration.",
            )
        ],
        summary="Use the overview diagram.",
    )

    class FakeResponses:
        def __init__(self) -> None:
            self.outputs = [script, visual_selection, crop]
            self.calls = []

        def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output_parsed=self.outputs.pop(0))

    responses = FakeResponses()
    fake_client = SimpleNamespace(responses=responses)
    monkeypatch.setattr(review, "_openai_client", lambda: fake_client)
    monkeypatch.setattr(review, "_openai_model", lambda: "test-openai-model")
    client = TestClient(app)

    imported = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("training.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    ).json()
    project_id = imported["project_id"]

    drafted = client.post(
        "/pdf-review/draft-ai",
        json={
            "project_id": project_id,
            "selected_pages": [1, 2],
            "objective": "Explain the deck",
            "language": "English",
            "target_seconds": 75,
        },
    )
    assert drafted.status_code == 200
    assert drafted.json()["mode"] == "openai"
    assert drafted.json()["model"] == "test-openai-model"
    assert responses.calls[0]["text_format"] is review._AIScript

    selected = client.post(
        "/pdf-review/select-visuals",
        json={
            "project_id": project_id,
            "candidate_pages": [1, 2],
            "scenes": [
                {
                    "id": "scene_01",
                    "page": 1,
                    "narration": "Explain the source diagram.",
                    "prompt": "Point to the source visual.",
                }
            ],
            "max_visuals": 2,
        },
    )
    assert selected.status_code == 200
    assert selected.json()["choices"][0]["page"] == 1
    assert selected.json()["calls"] == 1
    assert selected.json()["candidate_count"] == 2
    assert responses.calls[1]["text_format"] is review._AIVisualSelection

    suggested = client.post(
        "/pdf-review/suggest-crops",
        json={"project_id": project_id, "pages": [1]},
    )
    assert suggested.status_code == 200
    assert suggested.json()["calls"] == 1
    assert responses.calls[2]["text_format"] is review._AICrop
    image_input = responses.calls[2]["input"][0]["content"][1]
    assert image_input["type"] == "input_image"
    assert image_input["image_url"].startswith("data:image/jpeg;base64,")
    assert image_input["detail"] == "high"
