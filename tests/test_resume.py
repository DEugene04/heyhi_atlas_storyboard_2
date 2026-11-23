"""
Resume semantics for the billable stages (open-questions #24).

The point of these tests is money. Every one of them is really asking "did we
avoid paying for something we already have", so they all count CALLS rather than
inspecting output — a fake client that records its invocations is the whole
apparatus. No network, no ffmpeg, no credits.

The three rules being pinned:
  * resume never regenerates a file that exists;
  * resume still finishes a scene whose expensive half succeeded and whose cheap
    half did not (raw clip present, no tts/<id>.done marker);
  * force and resume together are refused rather than resolved by guessing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.external_tts import mark_scene_done, scene_is_done
from app.pipeline.image_gen import AssetGenerator
from app.pipeline.scene_gen import SceneGenerator


# ---------------------------------------------------------------- fakes
class CountingImageClient:
    """ImageLLM that returns a valid-looking PNG and remembers every prompt."""
    model = "fake-image"

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt: str, references: list[bytes]) -> bytes:
        self.calls.append(prompt)
        return b"\x89PNG\r\n\x1a\n" + prompt.encode()[:8]


class CountingVeo:
    """Stands in for the whole Veo client + retry helper.

    generate_scene is patched out wholesale in these tests rather than faked at
    the HTTP layer: what matters here is whether SceneGenerator DECIDED to call
    it, not what Veo would have returned.
    """


class RecordingAudio:
    """SceneAudioProvider double. Writes the scene file the way L6 does — via
    the done-marker — so scene_is_done() sees a genuinely finished scene."""
    voice_field = "voice_name"

    def __init__(self):
        self.processed: list[str] = []

    def process(self, raw_mp4: Path, scene: dict, voice: str, run_dir: Path) -> Path:
        self.processed.append(scene["id"])
        dest = run_dir / "scenes" / f"{scene['id']}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"voiced")
        mark_scene_done(run_dir, scene["id"])
        return dest


# ---------------------------------------------------------------- L4 assets
BP = {
    "style": "cartoon style",
    "backgrounds": [{"id": "bg1", "prompt": "an office"},
                    {"id": "bg2", "prompt": "a corridor"}],
    "start_frame": [{"id": "sf1", "prompt": "a frame", "reference": ["bg1"]}],
}


def test_assets_resume_skips_pngs_that_exist(tmp_path):
    client = CountingImageClient()
    gen = AssetGenerator(tmp_path, tmp_path / "chars", client, resume=True)

    (tmp_path / "backgrounds").mkdir()
    (tmp_path / "backgrounds" / "bg1.png").write_bytes(b"already here")

    res = gen.generate_all(BP)

    assert len(client.calls) == 2, "only bg2 and sf1 should have been generated"
    assert [p.name for p in gen.reused] == ["bg1.png"]
    assert (tmp_path / "backgrounds" / "bg1.png").read_bytes() == b"already here"
    assert len(res["backgrounds"]) == 2, "the skipped file is still reported"


def test_assets_without_resume_regenerates_everything(tmp_path):
    """The default is unchanged: resume is opt-in, and force (which is
    resume=False here) overwrites."""
    client = CountingImageClient()
    (tmp_path / "backgrounds").mkdir()
    (tmp_path / "backgrounds" / "bg1.png").write_bytes(b"already here")

    AssetGenerator(tmp_path, tmp_path / "chars", client).generate_all(BP)

    assert len(client.calls) == 3
    assert (tmp_path / "backgrounds" / "bg1.png").read_bytes() != b"already here"


def test_resume_skips_a_start_frame_before_resolving_its_references(tmp_path):
    """The existence check runs BEFORE _resolve_ref, so a start frame that is
    already generated does not fail on a reference that is no longer on disk.
    Without the ordering this raises AssetError."""
    client = CountingImageClient()
    gen = AssetGenerator(tmp_path, tmp_path / "chars", client, resume=True)

    for name in ("backgrounds/bg1.png", "backgrounds/bg2.png", "start_frame/sf1.png"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    (tmp_path / "backgrounds" / "bg1.png").unlink()   # the reference is gone

    gen.generate_all(BP)      # regenerates bg1 only; sf1 is skipped, not resolved
    assert len(client.calls) == 1


# ---------------------------------------------------------------- L5 scenes
SCENES = {"scenes": [
    {"id": "s1", "type": "video-with-audio", "text": "hello"},
    {"id": "s2", "type": "B-Roll", "length": 2.0},
    {"id": "s3", "type": "video-with-audio", "text": "goodbye"},
]}


def _generator(tmp_path, monkeypatch):
    """A SceneGenerator whose Veo half is replaced by a counter."""
    gen = SceneGenerator(tmp_path, CountingVeo())
    generated: list[str] = []

    def fake_generate_scene(scene):
        generated.append(scene["id"])
        gen.raw_dir.mkdir(parents=True, exist_ok=True)
        gen.scenes_dir.mkdir(parents=True, exist_ok=True)
        (gen.raw_dir / f"{scene['id']}.mp4").write_bytes(b"raw")
        (gen.scenes_dir / f"{scene['id']}.mp4").write_bytes(b"raw")
        return gen.scenes_dir / f"{scene['id']}.mp4"

    monkeypatch.setattr(gen, "generate_scene", fake_generate_scene)
    return gen, generated


def test_resume_skips_scenes_that_finished(tmp_path, monkeypatch):
    gen, generated = _generator(tmp_path, monkeypatch)
    audio = RecordingAudio()

    # s1 completed on the previous attempt: raw clip, output clip, done marker.
    gen.raw_dir.mkdir(parents=True)
    gen.scenes_dir.mkdir(parents=True)
    (gen.raw_dir / "s1.mp4").write_bytes(b"raw")
    (gen.scenes_dir / "s1.mp4").write_bytes(b"voiced")
    mark_scene_done(tmp_path, "s1")

    gen.run_all(SCENES, voice_for=lambda s: "v", audio=audio, resume=True)

    assert generated == ["s2", "s3"], "s1 must not be paid for twice"
    assert audio.processed == ["s3"], "s1's audio half was already done"
    assert len(gen.reused) == 1


def test_resume_redoes_only_the_audio_half_when_veo_succeeded(tmp_path, monkeypatch):
    """THE CASE THAT MATTERS. A crash between the Veo call and L6 leaves a paid
    clip in veo_raw/ and no done marker. Resume must reuse the clip and re-run
    the cheap half — not regenerate the video, and not skip the scene as if it
    were finished."""
    gen, generated = _generator(tmp_path, monkeypatch)
    audio = RecordingAudio()

    gen.raw_dir.mkdir(parents=True)
    (gen.raw_dir / "s1.mp4").write_bytes(b"raw")      # paid for, never voiced

    gen.run_all(SCENES, voice_for=lambda s: "v", audio=audio, resume=True)

    assert "s1" not in generated, "the Veo call must not be repeated"
    assert audio.processed == ["s1", "s3"], "s1's audio half must be finished"
    assert scene_is_done(tmp_path, "s1")


def test_resume_off_regenerates_every_scene(tmp_path, monkeypatch):
    gen, generated = _generator(tmp_path, monkeypatch)
    gen.raw_dir.mkdir(parents=True)
    (gen.raw_dir / "s1.mp4").write_bytes(b"raw")
    mark_scene_done(tmp_path, "s1")

    gen.run_all(SCENES, voice_for=lambda s: "v", audio=RecordingAudio())

    assert generated == ["s1", "s2", "s3"]
    assert gen.reused == []


def test_broll_is_restored_from_raw_rather_than_trusted(tmp_path, monkeypatch):
    """B-Roll has no done marker — there is no L6 to mark. Restoring it from the
    pristine clip costs nothing and is deterministic, so resume always does it
    rather than reasoning about whether the trim finished."""
    gen, generated = _generator(tmp_path, monkeypatch)
    gen.raw_dir.mkdir(parents=True)
    gen.scenes_dir.mkdir(parents=True)
    (gen.raw_dir / "s2.mp4").write_bytes(b"pristine")
    (gen.scenes_dir / "s2.mp4").write_bytes(b"half-trimmed junk")

    monkeypatch.setattr("app.pipeline.scene_gen.trim_video", lambda *a, **k: None)
    gen.run_all({"scenes": [SCENES["scenes"][1]]},
                voice_for=lambda s: "v", audio=RecordingAudio(), resume=True)

    assert generated == []
    assert (gen.scenes_dir / "s2.mp4").read_bytes() == b"pristine"


# ---------------------------------------------------------------- the marker
def test_done_marker_needs_both_halves(tmp_path):
    """Clearing scenes/ to re-render must not leave the run claiming the work is
    already finished."""
    mark_scene_done(tmp_path, "s1")
    assert not scene_is_done(tmp_path, "s1"), "marker without output is not done"

    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "s1.mp4").write_bytes(b"x")
    assert scene_is_done(tmp_path, "s1")

    (tmp_path / "tts" / "s1.done").unlink()
    assert not scene_is_done(tmp_path, "s1"), "output without marker is not done"


# ---------------------------------------------------------------- API surface
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STORAGE_ROOT", str(tmp_path))
    import importlib
    import main as main_module
    importlib.reload(main_module)
    from fastapi.testclient import TestClient
    return TestClient(main_module.app), main_module


def _make_run(main_module, run_id="run_abcdef12"):
    d = main_module.STORAGE_ROOT / run_id
    d.mkdir(parents=True)
    (d / "selections.json").write_text("{}")
    (d / "blueprint.json").write_text(json.dumps({"music_style": "calm piano"}))
    return d


@pytest.mark.parametrize("endpoint", ["assets", "scenes", "music"])
def test_force_and_resume_together_is_refused(client, endpoint):
    """Guessing which one was meant is how you spend someone's credits by
    accident. 422, on every billable stage, with the same message."""
    c, main_module = client
    _make_run(main_module)
    r = c.post(f"/pipeline/{endpoint}",
               json={"run_id": "run_abcdef12", "force": True, "resume": True})
    assert r.status_code == 422
    assert "mutually exclusive" in json.dumps(r.json())


def test_resume_clears_the_already_exists_guard(client):
    """resume must not be blocked by the 409 that force clears — otherwise it is
    unreachable exactly when it is needed."""
    c, main_module = client
    d = _make_run(main_module)
    (d / "backgrounds").mkdir()
    (d / "backgrounds" / "bg1.png").write_bytes(b"x")

    blocked = c.post("/pipeline/assets", json={"run_id": "run_abcdef12"})
    assert blocked.status_code == 409
    assert "resume=true" in blocked.json()["detail"]

    # resume gets past the guard and fails later, on the missing API key —
    # which is proof it got past it.
    r = c.post("/pipeline/assets", json={"run_id": "run_abcdef12", "resume": True})
    assert r.status_code != 409


def test_music_resume_keeps_the_existing_track(client):
    c, main_module = client
    d = _make_run(main_module)
    (d / "music_bg").mkdir()
    (d / "music_bg" / "track.mp3").write_bytes(b"ID3 existing")

    r = c.post("/pipeline/music", json={"run_id": "run_abcdef12", "resume": True})
    assert r.status_code == 200
    body = r.json()
    assert body["reused"] is True and body["regenerated"] is False
    assert (d / "music_bg" / "track.mp3").read_bytes() == b"ID3 existing"


def test_a_start_frame_seeds_an_image_to_video_clip(tmp_path):
    """Every scene is image-to-video: the start frame is the first frame, and there
    are no reference images."""
    (tmp_path / "start_frame").mkdir()
    (tmp_path / "start_frame" / "sf1.png").write_bytes(b"sf")
    gen = SceneGenerator(tmp_path, CountingVeo(), seed=7)
    scene = {"id": "s1", "task": "image-to-video", "model": "Veo 3.1 Lite",
             "type": "video-with-audio", "text": "hi", "prompt": "p", "length": 4,
             "reference_from": "start_frame:sf1", "aspect_ratio": "16:9"}
    spec = gen.build_spec(scene)
    assert spec.image == b"sf" and spec.references == []
    assert spec.task == "image-to-video" and spec.seed == 7 and spec.duration_s == 4.0


def test_turbo_flag_reaches_the_spec():
    scene = {"id": "s1", "task": "image-to-video", "model": "Veo 3.1 Lite", "type": "B-Roll",
             "prompt": "p", "length": 4, "reference_from": "background:k1", "aspect_ratio": "16:9"}
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "backgrounds").mkdir(); (Path(d) / "backgrounds" / "k1.png").write_bytes(b"k")
        assert SceneGenerator(d, CountingVeo(), turbo=True).build_spec(scene).turbo is True
        assert SceneGenerator(d, CountingVeo()).build_spec(scene).turbo is None
