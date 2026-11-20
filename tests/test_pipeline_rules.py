"""
The small pure rules scattered across layers: blueprint shape post-processing,
Veo duration arithmetic, prompt composition, and the shared retry loop.

Individually trivial. Collectively they decide how long every clip is, what Veo
is actually told, and how much a run costs — and none of them is exercised by
anything else in the suite.
"""
from __future__ import annotations

import json

import pytest

from app.clients.video_veo_client import VEO_ALLOWED_SECONDS, snap_duration
from app.pipeline.director import (
    OPENING_LENGTH_S, derive_style_type, enforce_opening_length,
    has_opening_section, read_blueprint_type,
)
from app.pipeline.image_gen import _compose_prompt
from app.pipeline.scene_gen import (
    VEO_BROLL_SECONDS, VEO_VIDEO_SECONDS, compose_veo_prompt, veo_generation_seconds,
)
from app.utils.retry import RetriesExhausted, looks_transient, retry_call
from app.utils.text_utils import parse_json_lenient


def _run_with_config(tmp_path, **cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- blueprint_type
def test_blueprint_type_read_from_config(tmp_path):
    assert read_blueprint_type(_run_with_config(tmp_path, blueprint_type="summary")) == "summary"


def test_unknown_blueprint_type_falls_back_to_opening(tmp_path):
    """Anything unrecognised behaves as 'opening' so pre-existing runs — and any
    half-added type like the specced-but-unimplemented 'illustration' — keep
    working rather than failing."""
    assert read_blueprint_type(_run_with_config(tmp_path, blueprint_type="illustration")) == "opening"


def test_missing_config_falls_back_to_opening(tmp_path):
    assert read_blueprint_type(tmp_path) == "opening"


def test_only_opening_runs_get_the_title_section(tmp_path):
    assert has_opening_section(_run_with_config(tmp_path, blueprint_type="opening")) is True
    assert has_opening_section(_run_with_config(tmp_path, blueprint_type="summary")) is False


# ---------------------------------------------------------------- opening length
def test_opening_broll_forced_to_four_seconds(tmp_path):
    """The title card needs a plate long enough to read. Runs BEFORE L5 so Veo
    generates it at the right length — too late afterwards."""
    run = _run_with_config(tmp_path, blueprint_type="opening")
    bp = {"scenes": [
        {"id": "b1", "type": "B-Roll", "broll_role": "opening", "length": 1.0},
        {"id": "b2", "type": "B-Roll", "broll_role": "closing", "length": 1.0},
    ]}
    out = enforce_opening_length(bp, run)
    assert out["scenes"][0]["length"] == OPENING_LENGTH_S == 4.0
    assert out["scenes"][1]["length"] == 1.0, "closing B-Roll must be untouched"


def test_summary_runs_keep_the_directors_short_plate(tmp_path):
    run = _run_with_config(tmp_path, blueprint_type="summary")
    bp = {"scenes": [{"id": "b1", "type": "B-Roll", "broll_role": "opening", "length": 1.0}]}
    assert enforce_opening_length(bp, run)["scenes"][0]["length"] == 1.0


def test_enforce_opening_length_does_not_mutate_its_input(tmp_path):
    run = _run_with_config(tmp_path, blueprint_type="opening")
    bp = {"scenes": [{"id": "b1", "type": "B-Roll", "broll_role": "opening", "length": 1.0}]}
    enforce_opening_length(bp, run)
    assert bp["scenes"][0]["length"] == 1.0


# ---------------------------------------------------------------- style_type
@pytest.mark.parametrize("style,expected", [
    ("A professional realism art style", "realism"),
    ("photorealistic corporate explainer", "realism"),
    ("modern flat 2D cartoon, cel-shading", "cartoon"),
    ("semi-realism illustrated style", "semi-realism"),
    ("semi realism, painterly", "semi-realism"),
    ("semi-realistic characters", "semi-realism"),
])
def test_derive_style_type(style, expected):
    assert derive_style_type(style) == expected


def test_semi_realism_checked_before_realism():
    """Order dependency: 'semi-realism' contains 'realism', so a naive check
    order would label every semi-realism run as realism."""
    assert derive_style_type("semi-realism") == "semi-realism"


@pytest.mark.parametrize("style", ["watercolour storybook", "", "claymation"])
def test_unrecognised_style_falls_back_silently(style):
    """No signal that the match missed — a wrong title treatment is cosmetic and
    a hard failure would waste a whole director call."""
    assert derive_style_type(style) == "realism"


# ---------------------------------------------------------------- Veo durations
def test_broll_generated_at_the_four_second_floor():
    """Veo will not emit anything shorter, so a 1s B-Roll is GENERATED at 4s —
    and billed at 4s — then trimmed. This is why cost does not track video
    length."""
    assert veo_generation_seconds({"type": "B-Roll", "length": 1.0}) == VEO_BROLL_SECONDS == 4.0


def test_long_broll_rounded_up_to_a_whole_second():
    assert veo_generation_seconds({"type": "B-Roll", "length": 5.2}) == 6.0


@pytest.mark.parametrize("words,expected", [
    (0, 4.0), (5, 4.0), (8, 5.0), (12, 6.0), (17, 8.0), (20, 8.0), (40, 8.0),
])
def test_speaking_scenes_sized_to_their_dialogue(words, expected):
    """words / 2.5 wps + 1s headroom, 4s floor, 8s ceiling — so a short line is not
    given 8s to fill with babble."""
    scene = {"type": "video-with-audio", "length": None, "text": " ".join(["word"] * words)}
    assert veo_generation_seconds(scene) == expected <= VEO_VIDEO_SECONDS


@pytest.mark.parametrize("requested,snapped", [
    (0.5, 4), (4, 4), (4.1, 6), (6, 6), (6.5, 8), (8, 8), (20, 8),
])
def test_snap_duration_to_allowed_lengths(requested, snapped):
    """Veo accepts only 4/6/8s; anything longer clamps to the maximum."""
    assert snap_duration(requested) == snapped
    assert snapped in VEO_ALLOWED_SECONDS


# Veo pricing moved out of video_veo_client into app/costs.py, which prices
# every layer rather than just video. See tests/test_costs.py.


# ---------------------------------------------------------------- prompts
def test_spoken_text_appended_for_speaking_scenes():
    """Looks contradictory — L6 mutes Veo's audio — but Veo needs the words to
    animate the mouth, and on the Azure path its native audio is the timing
    reference the overlay aligns against."""
    out = compose_veo_prompt({"type": "video-with-audio", "prompt": "P", "text": "Hello."})
    assert out == ('P Audio sync: The character says "Hello."'
                   ' Once the line is finished the character stays silent with their mouth closed.')


def test_broll_prompt_left_alone():
    assert compose_veo_prompt({"type": "B-Roll", "prompt": "P", "text": ""}) == "P"


def test_speaking_scene_with_empty_text_is_not_annotated():
    assert compose_veo_prompt({"type": "video-with-audio", "prompt": "P", "text": "  "}) == "P"


def test_style_prepended_to_image_prompts():
    """The single lever that changes every image in a run at once."""
    assert _compose_prompt("cartoon style", "An office.") == "cartoon style, An office."


def test_trailing_comma_in_style_not_doubled():
    assert _compose_prompt("cartoon style,", "An office.") == "cartoon style, An office."


def test_empty_style_leaves_the_prompt_untouched():
    assert _compose_prompt("", "An office.") == "An office."


# ---------------------------------------------------------------- lenient JSON
def test_plain_json_parsed():
    assert parse_json_lenient('{"a": 1}') == {"a": 1}


def test_markdown_fence_stripped():
    """Gemini wraps its output in a fence often enough that this is load-bearing,
    not a nicety."""
    assert parse_json_lenient('```json\n{"a": 1}\n```') == {"a": 1}


def test_bare_fence_stripped():
    assert parse_json_lenient('```\n{"a": 1}\n```') == {"a": 1}


def test_junk_raises_value_error():
    """direct() catches ValueError, so a parse failure becomes a retry with
    feedback rather than a 500."""
    with pytest.raises(ValueError):
        parse_json_lenient("not json at all")


# ---------------------------------------------------------------- retry
def test_returns_immediately_on_success():
    assert retry_call(lambda: "ok", attempts=3, exc_type=RuntimeError, backoff_s=0) == ("ok", 1)


def test_retries_until_success_and_reports_the_attempt_number():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("500 transient")
        return "ok"

    assert retry_call(flaky, attempts=5, exc_type=RuntimeError, backoff_s=0) == ("ok", 3)


def test_exhausted_retries_carry_the_last_error():
    def always_fails():
        raise RuntimeError("boom")

    with pytest.raises(RetriesExhausted) as ex:
        retry_call(always_fails, attempts=3, exc_type=RuntimeError, backoff_s=0)
    assert ex.value.attempts == 3
    assert str(ex.value.last) == "boom"


def test_non_retryable_exceptions_propagate_immediately():
    """Veo and Lyria retry ONLY their Transient* subclass — a genuine error must
    not be retried three times at full price."""
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        raise ValueError("400 bad request")

    with pytest.raises(ValueError):
        retry_call(bad_request, attempts=3, exc_type=RuntimeError, backoff_s=0)
    assert calls["n"] == 1


@pytest.mark.parametrize("msg", [
    "503 Service Unavailable", "rate limit exceeded", "deadline timeout",
    "500 internal", "backend unavailable",
])
def test_transient_markers_recognised(msg):
    assert looks_transient(Exception(msg))


@pytest.mark.parametrize("msg", ["400 bad request", "invalid api key", "safety filtered"])
def test_permanent_errors_not_treated_as_transient(msg):
    assert not looks_transient(Exception(msg))


# ---------------------------------------------------------------- title scaling
def test_title_style_scales_to_the_frame_short_side():
    """Title sizes are authored for 720p; at H3's 480P (832x480) they must shrink,
    in portrait too, and 720p must be left alone."""
    from app.pipeline.renderer import DEFAULT_TITLE_STYLE as base, scale_title_style
    assert scale_title_style(base, 1280, 720).font_size == base.font_size
    small = scale_title_style(base, 832, 480)
    assert small.font_size == round(base.font_size * 480 / 720) == 64
    assert small.outline_w < base.outline_w and small.anim_travel < base.anim_travel
    assert scale_title_style(base, 480, 832).font_size == small.font_size
