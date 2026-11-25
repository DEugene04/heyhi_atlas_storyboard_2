import shutil

import pytest

from app.utils.ffmpeg_utils import probe, run_ffmpeg, strip_audio

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_strip_audio_removes_the_track_and_keeps_the_video(tmp_path):
    clip = tmp_path / "clip.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=24",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", clip],
               expect=clip, label="fixture")
    assert probe(clip)["has_audio"]
    strip_audio(clip)
    after = probe(clip)
    assert not after["has_audio"]
    assert int(after["w"]) == 64
