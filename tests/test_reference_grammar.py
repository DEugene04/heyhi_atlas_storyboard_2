"""
The reference_from grammar and Veo routing.

reference_from is the single field that decides which Veo model runs, which task
it runs as, and which asset gets attached. It is defined once in director.py and
re-checked in scene_gen.route() at generation time — the last gate before a
billable call.

The duplication is deliberate (belt and braces on spend), so these tests pin
BOTH ends and assert they agree. If someone adds a prefix to one and not the
other, test_route_agrees_with_director_table fails.
"""
from __future__ import annotations

import pytest

from app.pipeline.director import (
    REFERENCE_PREFIXES, ROUTE_BY_PREFIX, parse_reference,
)
from app.pipeline.scene_gen import VeoRouteError, route


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("value,expected", [
    ("start_frame:sf_1",    ("start_frame", "sf_1", True)),
    ("background:bg_office", ("background", "bg_office", True)),
    ("scene:s1",             ("scene", "s1", True)),
    # a bare id is treated as a scene reference, flagged inexplicit so the
    # validator can report it differently from "scene:<id>"
    ("s1",                   ("scene", "s1", False)),
    # a bare KEYWORD has no id -> ambiguous, prefix None
    ("start_frame",          (None, "start_frame", False)),
    ("background",           (None, "background", False)),
    ("scene",                (None, "scene", False)),
])
def test_parse_reference(value, expected):
    assert parse_reference(value) == expected


def test_parse_reference_tolerates_whitespace():
    assert parse_reference("  start_frame: sf_1  ") == ("start_frame", "sf_1", True)


def test_empty_reference_is_a_bare_scene_id():
    """Empty string falls through to the bare-id branch with an empty id, which
    the validator then rejects as 'not an earlier scene id'."""
    assert parse_reference("") == ("scene", "", False)


# ---------------------------------------------------------------- routing table
def test_every_prefix_has_a_route():
    assert set(ROUTE_BY_PREFIX) == set(REFERENCE_PREFIXES)


def test_every_route_is_image_to_video_on_lite():
    """The reference only picks the seed image; there is no reference-to-video."""
    for prefix in ("start_frame", "background", "scene"):
        assert ROUTE_BY_PREFIX[prefix] == ("image-to-video", "Veo 3.1 Lite")


# ---------------------------------------------------------------- route()
def _scene(ref, task, model):
    return {"id": "s", "reference_from": ref, "task": task, "model": model}


@pytest.mark.parametrize("prefix", sorted(REFERENCE_PREFIXES))
def test_route_agrees_with_director_table(prefix):
    """scene_gen and director must not drift apart."""
    task, model = ROUTE_BY_PREFIX[prefix]
    assert route(_scene(f"{prefix}:x", task, model)) == (task, model)


def test_route_rejects_incoherent_task_model_pair():
    """Only image-to-video on Lite exists."""
    with pytest.raises(VeoRouteError, match="image-to-video"):
        route(_scene("start_frame:sf1", "image-to-video", "Veo 3.1 Fast"))


def test_route_rejects_reference_to_video():
    with pytest.raises(VeoRouteError, match="image-to-video"):
        route(_scene("scene:s1", "reference-to-video", "Veo 3.1 Lite"))


def test_route_rejects_ambiguous_reference():
    with pytest.raises(VeoRouteError, match="ambiguous"):
        route(_scene("start_frame", "image-to-video", "Veo 3.1 Lite"))


def test_route_rejects_unknown_model():
    with pytest.raises(VeoRouteError, match="image-to-video"):
        route(_scene("scene:s1", "image-to-video", "Veo 4 Ultra"))


# ---------------------------------------------------------------- golden data
def test_every_golden_scene_routes(bp_dict):
    """Real director output must route without exception — the end-to-end
    guarantee that the grammar and the prompt rules agree."""
    for sc in bp_dict["scenes"]:
        task, model = route(sc)
        assert (task, model) == ROUTE_BY_PREFIX[parse_reference(sc["reference_from"])[0]]
