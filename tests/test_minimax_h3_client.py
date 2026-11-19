"""Local-GPU H3 client: sizing maths and the submit/poll flow (HTTP mocked)."""
import base64

import pytest

from app.clients import video_minimax_h3_client as h3
from app.clients.video_veo_client import TransientVeoError, VeoError, VeoJobSpec


def test_frame_count_lands_on_17k_plus_5_grid():
    assert h3.frame_count(8) == 192          # 8 s * 24 fps is already on the grid
    assert h3.frame_count(4) == 107          # 96 rounds up
    assert all((h3.frame_count(s) - 5) % 17 == 0 for s in range(4, 16))


@pytest.mark.parametrize("ratio,res,size", [
    ("16:9", "480P", (832, 480)), ("16:9", "768P", (1344, 768)),
    ("9:16", "480P", (480, 832)), ("1:1", "480P", (480, 480)),
])
def test_frame_size_is_multiple_of_32(ratio, res, size):
    assert h3.frame_size(ratio, res) == size
    assert all(v % 32 == 0 for v in size)


def test_frame_size_rejects_bad_input():
    with pytest.raises(h3.MinimaxH3Error):
        h3.frame_size("16:9", "2K")
    with pytest.raises(h3.MinimaxH3Error):
        h3.frame_size("5:7", "480P")


def _client(monkeypatch, polls):
    monkeypatch.setattr(h3, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(h3, "_webm_to_mp4", lambda b: b"MP4:" + b)
    c = h3.MinimaxH3Client(base_url="http://gpu:8001", fl2va_url="http://gpu:8002")
    seen = {}
    c._submit = lambda base, body: seen.update(base=base, body=body) or {"id": "j1", "poll_url": "/p/j1"}
    it = iter(polls)

    class R:
        def __init__(self, d): self.d = d
        ok = True
        def json(self): return self.d
    monkeypatch.setattr(c._http, "get", lambda url, timeout: R(next(it)))
    monkeypatch.setattr(h3.time, "sleep", lambda s: None)
    return c, seen


def test_text_to_video_flow(monkeypatch):
    done = {"status": "completed", "result": {"b64_json": base64.b64encode(b"webm").decode(), "fps": 24}}
    c, seen = _client(monkeypatch, [{"status": "queued"}, {"status": "generating"}, done])
    out = c.generate("hello", [], duration=8)
    assert out["video_bytes"] == b"MP4:webm" and out["job_id"] == "j1"
    assert out["task"] == "text-to-video" and seen["base"] == "http://gpu:8001"
    assert seen["body"]["video_frames"] == 192 and "ref_images" not in seen["body"]
    assert "b64_json" not in str(out["record"])


def test_first_frame_uses_fl2va_server(monkeypatch, tmp_path):
    from PIL import Image
    img = tmp_path / "f.png"
    Image.new("RGBA", (64, 64)).save(img)
    done = {"status": "completed", "result": {"b64_json": base64.b64encode(b"w").decode()}}
    c, seen = _client(monkeypatch, [done])
    c.generate("x", [], first_frame=img, duration=4)
    assert seen["base"] == "http://gpu:8002" and "init_image" in seen["body"]


def test_failed_job_raises(monkeypatch):
    c, _ = _client(monkeypatch, [{"status": "failed", "error": {"message": "oom"}}])
    with pytest.raises(h3.MinimaxH3Error, match="oom"):
        c.generate("x", [], duration=8)


def test_input_validation():
    c = h3.MinimaxH3Client(base_url="http://gpu:8001")
    with pytest.raises(h3.MinimaxH3Error):
        c.generate("x", [], duration=2)
    with pytest.raises(h3.MinimaxH3Error):
        c.generate("x", [], first_frame="a.png", duration=8) if False else c.generate("", [], duration=8)


def test_scene_client_maps_5xx_to_transient(monkeypatch):
    c = h3.MinimaxH3SceneClient(base_url="http://gpu:8001")

    def boom(*a, **k):
        r = h3.requests.Response(); r.status_code = 503
        raise h3.requests.HTTPError("503", response=r)
    monkeypatch.setattr(h3.MinimaxH3Client, "generate", boom)
    spec = VeoJobSpec(scene_id="s1", task="reference-to-video", model="x", prompt="p",
                      aspect_ratio="16:9", duration_s=8, image=None, references=[], safety="")
    with pytest.raises(TransientVeoError):
        c.generate(spec)
    c2 = h3.MinimaxH3SceneClient(base_url="http://gpu:8001")
    spec.task = "text-to-video"
    with pytest.raises(VeoError):
        c2.generate(spec)


def test_session_ignores_system_proxy():
    """A system proxy answers 503 for Tailscale addresses; the client must bypass it."""
    assert h3.MinimaxH3Client(base_url="http://gpu:8001")._http.trust_env is False


def test_broken_result_download_is_refetched(monkeypatch):
    done = {"status": "completed", "result": {"b64_json": base64.b64encode(b"w").decode()}}
    c, _ = _client(monkeypatch, [])
    calls = []

    class R:
        ok = True
        def json(self): return done

    def get(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise h3.requests.exceptions.ChunkedEncodingError("broken")
        return R()
    monkeypatch.setattr(c._http, "get", get)
    assert c.generate("x", [], duration=4)["job_id"] == "j1" and len(calls) == 3


def test_reference_images_are_capped_to_the_output_size(tmp_path):
    import base64, io
    from PIL import Image
    from app.clients.video_minimax_h3_client import _encode_image
    big = tmp_path / "big.png"
    Image.new("RGBA", (768, 1344), (10, 20, 30, 255)).save(big)
    out = Image.open(io.BytesIO(base64.b64decode(_encode_image(big, 832))))
    assert out.mode == "RGB" and max(out.size) == 832 and out.size[0] < out.size[1]
    small = tmp_path / "small.png"
    Image.new("RGB", (100, 60)).save(small)
    assert Image.open(io.BytesIO(base64.b64decode(_encode_image(small, 832)))).size == (100, 60)


def test_seed_is_concrete_logged_and_overridable(monkeypatch):
    done = {"status": "completed", "result": {"b64_json": base64.b64encode(b"w").decode()}}
    c, seen = _client(monkeypatch, [done, done])
    out = c.generate("hi", [], duration=4)
    assert out["seed"] == seen["body"]["seed"] == out["request"]["seed"] >= 0
    out = c.generate("hi", [], duration=4, seed=1234)
    assert out["seed"] == seen["body"]["seed"] == 1234


def test_reference_video_is_sent_as_one_frame_list(monkeypatch, tmp_path):
    done = {"status": "completed", "result": {"b64_json": base64.b64encode(b"w").decode()}}
    c, seen = _client(monkeypatch, [done])
    monkeypatch.setattr(h3, "_encode_video_frames", lambda p, secs, side: ["f1", "f2", "f3"])
    out = c.generate("hi", [], reference_video=tmp_path / "v.mp4", duration=4)
    assert seen["body"]["ref_videos"] == [["f1", "f2", "f3"]]
    assert "ref_images" not in seen["body"] and seen["base"] == "http://gpu:8001"
    assert out["task"] == "reference-to-video" and out["request"]["reference_video_frames"] == 3
    assert "ref_videos" not in out["request"]
    with pytest.raises(h3.MinimaxH3Error):
        c.generate("hi", [], first_frame="x.png", reference_video=tmp_path / "v.mp4")
