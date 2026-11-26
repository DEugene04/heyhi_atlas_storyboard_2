"""run_ffmpeg must never wait forever on a stuck ffmpeg."""
import time

import pytest

from app.utils import ffmpeg_utils
from app.utils.ffmpeg_utils import FfmpegError, run_ffmpeg


@pytest.fixture
def fake_binary(monkeypatch):
    """Stand `sleep` in for ffmpeg so the test needs no ffmpeg and no media."""
    monkeypatch.setattr(ffmpeg_utils, "_require", lambda name: "sleep")


def test_a_stuck_command_is_killed_and_reported(fake_binary) -> None:
    started = time.monotonic()
    with pytest.raises(FfmpegError, match=r"boxed subtitle burn timed out after 0\.3s"):
        run_ffmpeg(["30"], flags=(), label="boxed subtitle burn", timeout=0.3)
    assert time.monotonic() - started < 5      # returned at the limit, not after 30 s


def test_a_timeout_raises_even_when_the_exit_code_is_not_checked(fake_binary) -> None:
    with pytest.raises(FfmpegError, match="timed out"):
        run_ffmpeg(["30"], flags=(), check=False, timeout=0.3)


def test_a_command_inside_the_limit_is_unaffected(fake_binary) -> None:
    proc = run_ffmpeg(["0"], flags=(), timeout=10)
    assert proc.returncode == 0


def test_the_limit_comes_from_the_environment(fake_binary, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_FFMPEG_TIMEOUT_S", "0.3")
    with pytest.raises(FfmpegError, match=r"after 0\.3s"):
        run_ffmpeg(["30"], flags=())


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_a_bad_environment_value_falls_back_to_the_default(monkeypatch, raw) -> None:
    monkeypatch.setenv("ATLAS_FFMPEG_TIMEOUT_S", raw)
    assert ffmpeg_utils._default_timeout() == ffmpeg_utils.DEFAULT_TIMEOUT_S
