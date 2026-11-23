"""
Generating or redoing ONE scene (the Studio's per-scene buttons).

Same rule as test_resume.py: count calls, since every call is paid video. No
network, no ffmpeg, no credits - the video call is replaced by a recorder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.external_tts import mark_scene_done
from app.pipeline.scene_gen import (
    SceneGenerator, continuations_of, continues_from, scene_ready,
)

BP = {"scenes": [
    {"id": "s1", "type": "video-with-audio", "text": "hello", "reference_from": "start_frame:sf1"},
    {"id": "s2", "type": "B-Roll", "length": 2.0, "reference_from": "scene:s1"},
    {"id": "s3", "type": "B-Roll", "length": 2.0, "reference_from": "scene:s2"},
    {"id": "s4", "type": "B-Roll", "length": 2.0, "reference_from": "background:bg1"},
]}


# ---------------------------------------------------------------- pure helpers
def test_continues_from_reads_only_scene_references():
    assert continues_from(BP["scenes"][1]) == "s1"
    assert continues_from(BP["scenes"][0]) is None
    assert continues_from(BP["scenes"][3]) is None


def test_continuations_follow_the_chain_but_not_unrelated_scenes():
    """Redoing s1 stales s2 (seeded from it) and s3 (seeded from s2), never s4."""
    assert continuations_of(BP, "s1") == ["s2", "s3"]
    assert continuations_of(BP, "s2") == ["s3"]
    assert continuations_of(BP, "s4") == []


def test_speaking_scene_is_not_ready_before_its_voice(tmp_path):
    """Its working copy lands in scenes/ before L6 - only the marker counts."""
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "s1.mp4").write_bytes(b"unvoiced")
    assert not scene_ready(tmp_path, BP["scenes"][0])
    mark_scene_done(tmp_path, "s1")
    assert scene_ready(tmp_path, BP["scenes"][0])


def test_broll_is_ready_once_its_clip_exists(tmp_path):
    assert not scene_ready(tmp_path, BP["scenes"][1])
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "s2.mp4").write_bytes(b"x")
    assert scene_ready(tmp_path, BP["scenes"][1])


# ---------------------------------------------------------------- generator
class _Audio:
    def __init__(self):
        self.processed: list[str] = []

    def process(self, raw_mp4, scene, voice, run_dir):
        self.processed.append(scene["id"])
        dest = Path(run_dir) / "scenes" / f"{scene['id']}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"voiced")
        mark_scene_done(run_dir, scene["id"])
        return dest


def _gen(tmp_path, monkeypatch):
    gen = SceneGenerator(tmp_path, object())
    generated: list[str] = []

    def fake(scene):
        generated.append(scene["id"])
        gen.raw_dir.mkdir(parents=True, exist_ok=True)
        gen.scenes_dir.mkdir(parents=True, exist_ok=True)
        (gen.raw_dir / f"{scene['id']}.mp4").write_bytes(b"raw")
        (gen.scenes_dir / f"{scene['id']}.mp4").write_bytes(b"raw")
        return gen.scenes_dir / f"{scene['id']}.mp4"

    monkeypatch.setattr(gen, "generate_scene", fake)
    return gen, generated


def test_run_one_generates_only_that_scene_and_voices_it(tmp_path, monkeypatch):
    gen, generated = _gen(tmp_path, monkeypatch)
    audio = _Audio()
    out = gen.run_one(BP["scenes"][0], lambda s: "v", audio)
    assert generated == ["s1"] and audio.processed == ["s1"]
    assert out == tmp_path / "scenes" / "s1.mp4"


def test_run_one_always_calls_the_video_model_even_if_a_clip_exists(tmp_path, monkeypatch):
    """Regenerate means regenerate: a finished scene is paid for again on request."""
    gen, generated = _gen(tmp_path, monkeypatch)
    gen.raw_dir.mkdir(parents=True)
    (gen.raw_dir / "s2.mp4").write_bytes(b"old")
    gen.run_one(BP["scenes"][1], lambda s: "v", _Audio())
    assert generated == ["s2"]


def test_run_one_skips_voice_for_broll(tmp_path, monkeypatch):
    gen, _ = _gen(tmp_path, monkeypatch)
    audio = _Audio()
    gen.run_one(BP["scenes"][1], lambda s: "v", audio)
    assert audio.processed == []


# ---------------------------------------------------------------- endpoint
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STORAGE_ROOT", str(tmp_path))
    import importlib
    import main as main_module
    importlib.reload(main_module)
    from fastapi.testclient import TestClient

    d = main_module.STORAGE_ROOT / "run_abcdef12"
    (d / "backgrounds").mkdir(parents=True)
    (d / "backgrounds" / "bg1.png").write_bytes(b"x")
    (d / "selections.json").write_text(json.dumps({"voice_source": "elevenlabs"}))
    (d / "blueprint.json").write_text(json.dumps(
        {"scenes": [BP["scenes"][1], BP["scenes"][2], BP["scenes"][3]]}))   # all B-Roll

    monkeypatch.setattr(main_module, "_scene_clients", lambda *a, **k: (object(), object()))
    generated: list[str] = []

    def fake_generate(self, scene):
        generated.append(scene["id"])
        self.scenes_dir.mkdir(parents=True, exist_ok=True)
        (self.scenes_dir / f"{scene['id']}.mp4").write_bytes(b"clip")
        return self.scenes_dir / f"{scene['id']}.mp4"

    monkeypatch.setattr(main_module.SceneGenerator, "generate_scene", fake_generate)
    return TestClient(main_module.app), d, generated


def _post(c, **extra):
    return c.post("/pipeline/scenes", json={"run_id": "run_abcdef12", **extra})


def test_generates_exactly_the_requested_scene(api):
    c, d, generated = api
    r = _post(c, scene_id="s4")
    assert r.status_code == 200, r.text
    assert generated == ["s4"]
    assert r.json()["scenes"] == 1
    assert (d / "scenes" / "s4.mp4").is_file() and not (d / "scenes" / "s2.mp4").exists()


def test_other_scenes_do_not_block_the_exists_guard(api):
    """Regenerating one scene must not need force just because scenes/ is non-empty."""
    c, d, generated = api
    (d / "scenes").mkdir()
    (d / "scenes" / "s4.mp4").write_bytes(b"old")
    assert _post(c, scene_id="s4").status_code == 200
    assert generated == ["s4"]


def test_scene_that_continues_needs_its_source_first(api):
    """s2 is seeded from s1's last frame - without s1 there is nothing to seed from."""
    c, d, generated = api
    (d / "blueprint.json").write_text(json.dumps({"scenes": BP["scenes"][:2]}))
    r = _post(c, scene_id="s2")
    assert r.status_code == 409 and "s1" in r.json()["detail"]
    assert generated == []


def test_regenerating_a_scene_reports_the_stale_continuations(api):
    c, d, generated = api
    (d / "scenes").mkdir()
    for sid in ("s2", "s3"):
        (d / "scenes" / f"{sid}.mp4").write_bytes(b"old")
    # s2 is the head of the chain here; s3 was seeded from it and is already finished.
    (d / "blueprint.json").write_text(json.dumps({"scenes": [
        {"id": "s2", "type": "B-Roll", "length": 2.0, "reference_from": "background:bg1"},
        BP["scenes"][2]]}))
    r = _post(c, scene_id="s2")
    assert r.status_code == 200, r.text
    assert r.json()["stale_dependents"] == ["s3"]


def test_unknown_scene_is_404(api):
    c, _, generated = api
    assert _post(c, scene_id="nope").status_code == 404
    assert generated == []


@pytest.mark.parametrize("flag", ["force", "resume"])
def test_scene_id_does_not_combine_with_force_or_resume(api, flag):
    c, _, generated = api
    r = _post(c, scene_id="s4", **{flag: True})
    assert r.status_code == 422
    assert generated == []


def test_revoice_one_scene_needs_that_scenes_raw_clip(api):
    c, d, generated = api
    r = _post(c, scene_id="s4", tts_only=True)
    assert r.status_code == 409 and "s4" in r.json()["detail"]
    assert generated == []


def test_status_lists_what_each_scene_continues_from(api):
    c, d, _ = api
    states = {s["id"]: s for s in c.get("/pipeline/status?run_id=run_abcdef12").json()["scene_states"]}
    assert states["s3"]["continues_from"] == "s2"
    assert states["s4"]["continues_from"] is None
