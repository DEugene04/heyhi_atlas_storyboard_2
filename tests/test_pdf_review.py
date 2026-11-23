"""Pure endpoint checks for the review-only PDF workflow.

No OpenAI, Gemini, Kie, S3, or video call is made here.
"""
from __future__ import annotations

import io
import json
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
    assert "company GPU server" in response.text


def test_ui_files_are_revalidated_so_edits_are_never_served_stale() -> None:
    client = TestClient(app)
    for path in ("/pdf-review/", "/pdf-review/ui/app.js", "/pdf-review/ui/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"


def test_local_review_flow_reaches_gpu_preflight(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    client = TestClient(app)

    imported = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("training.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    )
    assert imported.status_code == 200
    source = imported.json()
    assert source["page_count"] == 2
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
    assert manifest["video_generation_enabled"] is True
    assert manifest["provider"] == "local-gpu"
    assert manifest["model"].startswith("minimax-h3")


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


def test_detect_crops_finds_a_figure_region_without_ai(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((70, 60), "Slide title", fontsize=24)  # header chrome
    # a filled box in the middle of the slide = a detectable figure region
    page.draw_rect(pymupdf.Rect(300, 180, 660, 400), color=(0, 0, 0), fill=(0.2, 0.4, 0.8))
    pdf_bytes = doc.tobytes()

    client = TestClient(app)
    imported = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("figure.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    ).json()

    detected = client.post(
        "/pdf-review/detect-crops",
        json={"project_id": imported["project_id"], "pages": [1]},
    )
    assert detected.status_code == 200
    body = detected.json()
    assert body["calls"] == 0  # no OpenAI spend
    assert body["model"] == "pdf-native"
    region = body["suggestions"][0]
    assert region["region_count"] >= 1
    # a tight box, not the whole slide, roughly where the rectangle was drawn
    assert 0.0 < region["width"] < 1.0 and 0.0 < region["height"] < 1.0
    assert 0.25 < region["x"] < 0.4


def test_detect_crops_reports_no_figure_on_text_only_slide(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    client = TestClient(app)
    imported = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("training.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    ).json()

    detected = client.post(
        "/pdf-review/detect-crops",
        json={"project_id": imported["project_id"], "pages": [1]},
    )
    assert detected.status_code == 200
    region = detected.json()["suggestions"][0]
    assert region["crop"] is None  # nothing to crop; caller decides, no blind full-page box


def _two_figure_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((70, 40), "Two figures", fontsize=24)  # header chrome
    page.draw_rect(pymupdf.Rect(100, 150, 300, 350), fill=(0.8, 0.2, 0.2))  # candidate 1 (left)
    page.draw_rect(pymupdf.Rect(650, 150, 850, 350), fill=(0.2, 0.4, 0.8))  # candidate 2 (right)
    return doc.tobytes()


def _import(client, pdf_bytes):
    return client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("f.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    ).json()


def test_auto_crops_single_figure_skips_the_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.draw_rect(pymupdf.Rect(300, 180, 660, 400), fill=(0.2, 0.4, 0.8))  # one figure
    client = TestClient(app)
    imported = _import(client, doc.tobytes())

    result = client.post(
        "/pdf-review/auto-crops",
        json={"project_id": imported["project_id"], "pages": [1]},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["calls"] == 0  # single candidate -> no paid call
    region = body["suggestions"][0]
    assert region["candidate_count"] == 1
    assert region["source"] == "pdf"
    assert 0.0 < region["width"] < 1.0 and 0.25 < region["x"] < 0.4


def test_auto_crops_multi_figure_lets_the_model_choose(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())

    class FakeResponses:
        def __init__(self) -> None:
            self.calls = []

        def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                output_parsed=review._AICropChoice(candidate_number=2, rationale="right chart")
            )

    responses = FakeResponses()
    monkeypatch.setattr(review, "_openai_client", lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(review, "_openai_model", lambda: "test-model")
    client = TestClient(app)
    imported = _import(client, _two_figure_pdf())

    result = client.post(
        "/pdf-review/auto-crops",
        json={
            "project_id": imported["project_id"],
            "pages": [1],
            "scenes": [{"page": 1, "narration": "Look at the chart on the right."}],
        },
    )
    assert result.status_code == 200
    body = result.json()
    assert body["calls"] == 1
    region = body["suggestions"][0]
    assert region["candidate_count"] == 2
    assert region["source"] == "pdf+llm"
    # candidate 2 is the right-hand box (x ~ 650/960)
    assert 0.6 < region["x"] < 0.72
    # the numbered overlay image was actually sent to the model
    image_part = responses.calls[0]["input"][0]["content"][1]
    assert image_part["type"] == "input_image"
    assert image_part["image_url"].startswith("data:image/jpeg;base64,")


def test_auto_crops_falls_back_to_union_without_a_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(app)
    imported = _import(client, _two_figure_pdf())

    result = client.post(
        "/pdf-review/auto-crops",
        json={"project_id": imported["project_id"], "pages": [1]},
    )
    assert result.status_code == 200  # degrades, never errors
    body = result.json()
    assert body["calls"] == 0
    region = body["suggestions"][0]
    assert region["source"] == "pdf-fallback"
    # union of both figures spans left box start to right box end
    assert region["x"] < 0.2 and region["x"] + region["width"] > 0.8


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


def test_scene_prompt_describes_action_not_the_frame_contents():
    from app.utils.presenter_prompt import first_frame_scene_prompt
    p = first_frame_scene_prompt('Say "hello".')
    assert "<d>[en] Say 'hello'.</d>" in p            # speech in tags, quotes neutralised
    assert "<Picture" not in p                        # no reference images in first-frame mode
    assert "green rectangle" in p and "first frame" in p
    assert "<d>" not in first_frame_scene_prompt("")  # no narration -> no speech tag


def _project_with_character(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path.resolve())
    client = TestClient(app)
    pid = client.post(
        "/pdf-review/import-file?target_seconds=75&language=English",
        files={"file": ("t.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    ).json()["project_id"]
    project = tmp_path / pid
    (project / "characters").mkdir()
    char = Image.new("RGBA", (120, 300), (0, 0, 0, 0))
    char.paste((200, 60, 40, 255), (15, 20, 105, 300))
    char.save(project / "characters/generated_character.png")
    data = json.loads((project / "project.json").read_text())
    data["character_path"] = "characters/generated_character.png"
    (project / "project.json").write_text(json.dumps(data))
    return client, pid, project


def _write_png(path, size, color):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_start_frame_needs_a_background_then_builds(tmp_path, monkeypatch) -> None:
    client, pid, project = _project_with_character(tmp_path, monkeypatch)
    assert client.post("/pdf-review/compose-start-frame", json={"project_id": pid}).status_code == 409

    _write_png(project / "backgrounds/generated_background.png", (640, 360), (150, 110, 90))
    r = client.post("/pdf-review/compose-start-frame", json={"project_id": pid})
    assert r.status_code == 200, r.text
    assert (project / r.json()["start_frame_path"]).is_file()
    assert (project / review.START_FRAME_BACKGROUND).is_file()      # exact pixels kept for compositing
    assert client.get(r.json()["start_frame_url"]).status_code == 200


def test_start_frame_can_use_an_image_library_background(tmp_path, monkeypatch) -> None:
    client, pid, project = _project_with_character(tmp_path, monkeypatch)
    lib = tmp_path / "library"
    monkeypatch.setattr(review, "IMAGE_LIBRARY_ROOT", lib.resolve())
    _write_png(lib / "background9.png", (640, 360), (100, 120, 160))

    r = client.post("/pdf-review/compose-start-frame",
                    json={"project_id": pid, "background": "background9.png"})
    assert r.status_code == 200, r.text
    assert json.loads((project / "project.json").read_text())["start_frame_background"] == "background9.png"
    missing = client.post("/pdf-review/compose-start-frame",
                          json={"project_id": pid, "background": "nope.png"})
    assert missing.status_code == 404


def test_scene_generation_starts_from_the_start_frame(tmp_path, monkeypatch) -> None:
    client, pid, project = _project_with_character(tmp_path, monkeypatch)
    body = {"project_id": pid, "scene_id": "s1", "narration": "hi"}
    assert client.post("/pdf-review/generate-scene", json=body).status_code == 409   # no frame yet

    _write_png(project / "backgrounds/generated_background.png", (640, 360), (150, 110, 90))
    assert client.post("/pdf-review/compose-start-frame", json={"project_id": pid}).status_code == 200

    seen = {}

    class FakeH3:
        def generate(self, prompt, image_paths, **kw):
            seen.update(prompt=prompt, images=image_paths, **kw)
            raise review.requests.RequestException("stop here")

    import app.clients.video_minimax_h3_client as h3
    monkeypatch.setattr(h3, "MinimaxH3Client", FakeH3)
    assert client.post("/pdf-review/generate-scene", json=body).status_code == 502
    assert seen["first_frame"] == project / "start_frames/start_frame.png"
    assert seen["images"] == []                    # first-frame mode excludes reference images
    assert "<d>[en] hi</d>" in seen["prompt"]
