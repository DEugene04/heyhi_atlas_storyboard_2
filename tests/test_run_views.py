"""
Read-only views the front-end depends on (/pipeline/status, runs, blueprint,
files, library media). Pure: a temp storage root and hand-made files, no
network, no keys, no credits.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main

RID = "run_abcdef12"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    chars = tmp_path / "chars"
    chars.mkdir()
    auds = tmp_path / "auds"
    auds.mkdir()
    monkeypatch.setattr(main, "STORAGE_ROOT", root.resolve())
    monkeypatch.setattr(main, "CHAR_LIB_ROOT", chars.resolve())
    monkeypatch.setattr(main, "AUDIO_LIB_ROOT", auds.resolve())
    (root / RID).mkdir()
    return TestClient(main.app), root / RID, chars, auds


def _touch(d, rel, data=b"x"):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


BP = {"title": "T", "scenes": [
    {"id": "s1", "type": "video-with-audio", "topic": "a"},
    {"id": "s2", "type": "B-Roll", "topic": None}]}


def test_status_walks_the_stages(env):
    c, d, *_ = env
    assert c.get("/pipeline/status", params={"run_id": RID}).json()["stage"] == "new"
    _touch(d, "config.json", b"{}")
    assert c.get("/pipeline/status", params={"run_id": RID}).json()["stage"] == "config"
    _touch(d, "source.txt")
    assert c.get("/pipeline/status", params={"run_id": RID}).json()["stage"] == "source"
    _touch(d, "blueprint.json", json.dumps(BP).encode())
    st = c.get("/pipeline/status", params={"run_id": RID}).json()
    assert st["stage"] == "blueprint" and st["scenes_total"] == 2 and st["title"] == "T"
    _touch(d, "selections.json", b"{}")
    assert c.get("/pipeline/status", params={"run_id": RID}).json()["stage"] == "selected"
    _touch(d, "backgrounds/bg1.png")
    st = c.get("/pipeline/status", params={"run_id": RID}).json()
    assert st["stage"] == "assets" and st["backgrounds"] == 1


def test_speaking_scene_needs_the_tts_marker_to_count_as_ready(env):
    c, d, *_ = env
    _touch(d, "blueprint.json", json.dumps(BP).encode())
    _touch(d, "scenes/s1.mp4")          # working copy, voice not laid on yet
    _touch(d, "scenes/s2.mp4")          # B-Roll: file alone is enough
    st = c.get("/pipeline/status", params={"run_id": RID}).json()
    assert [s["ready"] for s in st["scene_states"]] == [False, True]
    _touch(d, "tts/s1.done")
    st = c.get("/pipeline/status", params={"run_id": RID}).json()
    assert st["scenes_ready"] == 2 and st["stage"] == "scenes"
    _touch(d, "final.mp4")
    assert c.get("/pipeline/status", params={"run_id": RID}).json()["stage"] == "final"


def test_status_rejects_bad_and_unknown_run_ids(env):
    c, *_ = env
    assert c.get("/pipeline/status", params={"run_id": "nope"}).status_code == 422
    assert c.get("/pipeline/status", params={"run_id": "run_00000000"}).status_code == 404


def test_runs_lists_only_run_dirs_newest_first(env):
    c, d, *_ = env
    (d.parent / "run_11111111").mkdir()
    (d.parent / "not_a_run").mkdir()
    (d.parent / "stray.txt").write_text("x")
    import os
    os.utime(d, (1, 1))
    ids = [r["run_id"] for r in c.get("/pipeline/runs").json()]
    assert ids == ["run_11111111", RID]


def test_blueprint_409_then_placeholder_snapshot(env):
    c, d, *_ = env
    assert c.get("/pipeline/blueprint", params={"run_id": RID}).status_code == 409
    _touch(d, "blueprint.json", json.dumps(BP).encode())
    body = c.get("/pipeline/blueprint", params={"run_id": RID}).json()
    assert body["blueprint"]["title"] == "T" and body["placeholder"] is None
    _touch(d, "blueprint.placeholder.json", json.dumps({"title": "P"}).encode())
    assert c.get("/pipeline/blueprint", params={"run_id": RID}).json()["placeholder"]["title"] == "P"


def test_files_are_served_with_their_media_type(env):
    c, d, *_ = env
    _touch(d, "backgrounds/bg1.png", b"\x89PNG")
    _touch(d, "final.mp4", b"vid")
    assert c.get(f"/pipeline/files/{RID}/backgrounds/bg1.png").headers["content-type"] == "image/png"
    assert c.get(f"/pipeline/files/{RID}/final.mp4").headers["content-type"] == "video/mp4"
    assert c.get(f"/pipeline/files/{RID}/missing.png").status_code == 404


def test_files_refuse_traversal_symlinks_and_unserved_types(env):
    c, d, *_ = env
    secret = d.parent / "run_22222222"
    secret.mkdir()
    _touch(secret, "final.mp4")
    _touch(d, "config.json", b"{}")
    assert c.get(f"/pipeline/files/{RID}/..%2Frun_22222222%2Ffinal.mp4").status_code == 400
    assert c.get(f"/pipeline/files/{RID}/config.json").status_code == 404
    (d / "link.mp4").symlink_to(secret / "final.mp4")
    assert c.get(f"/pipeline/files/{RID}/link.mp4").status_code == 400


def test_library_media_routes(env):
    c, d, chars, auds = env
    (chars / "a.png").write_bytes(b"\x89PNG")
    (chars / "index.json").write_text(json.dumps(
        [{"char_id": "c1", "image_path": "a.png"},
         {"char_id": "evil", "image_path": "../outside.png"}]))
    (auds / "v.wav").write_bytes(b"RIFF")
    (auds / "index.json").write_text(json.dumps(
        [{"audio_id": "a1", "voice_name": "n", "language": "en-US", "sample_path": "v.wav"}]))
    assert c.get("/pipeline/characters/c1/image").status_code == 200
    assert c.get("/pipeline/characters/zzz/image").status_code == 404
    assert c.get("/pipeline/characters/evil/image").status_code == 400
    assert c.get("/pipeline/audios/a1/sample").status_code == 200
    assert c.get("/pipeline/audios/zzz/sample").status_code == 404


def test_landing_and_both_apps_are_served(env):
    c, *_ = env
    assert "/studio/" in c.get("/").text and "/pdf-review/" in c.get("/").text
    assert c.get("/studio/").status_code == 200
    assert c.get("/studio/ui/app.js").status_code == 200
    assert c.get("/studio/ui/secret.py").status_code == 404
    assert c.get("/pdf-review/").status_code == 200


def test_select_can_be_submitted_twice(env, tmp_path):
    """After the first /select blueprint.json holds resolved ids; the second call
    must still validate against the placeholders (the pristine snapshot)."""
    import shutil
    c, d, chars, auds = env
    src = main.Path(__file__).resolve().parents[1] / "docs" / "examples" / "run_0616c9e5"
    for name in ("blueprint.json", "blueprint.placeholder.json", "config.json"):
        shutil.copy(src / name, d / name)
    # first select starts from a fresh director output: placeholders only
    shutil.copy(src / "blueprint.placeholder.json", d / "blueprint.json")
    (d / "blueprint.placeholder.json").unlink()
    for cid in ("char_a", "char_b"):
        (chars / f"{cid}.png").write_bytes(b"\x89PNG")
    (chars / "index.json").write_text(json.dumps(
        [{"char_id": i, "image_path": f"{i}.png"} for i in ("char_a", "char_b")]))
    body = lambda cid: {"run_id": RID, "voice_source": "elevenlabs", "selections": [
        {"placeholder_id": "char_narrator", "char_id": cid, "audio_id": "voice1"}]}
    assert c.post("/pipeline/select", json=body("char_a")).status_code == 200
    r = c.post("/pipeline/select", json=body("char_b"))
    assert r.status_code == 200, r.text
    assert "char_b" in (d / "blueprint.json").read_text()
