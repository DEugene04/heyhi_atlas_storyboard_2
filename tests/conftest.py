"""
Shared fixtures.

Everything in this suite is PURE — no network, no API keys, no ffmpeg, no
credits. That is possible because the pipeline's decision-making logic is
already separated from its I/O: validation, alignment, routing and patching are
all functions over plain data.

The golden fixture is REAL DIRECTOR OUTPUT (docs/examples/run_0616c9e5), not a
hand-written blueprint. It validates clean, so any test that mutates one field
and expects one problem is measuring exactly that rule and nothing else. A
synthetic fixture would drift away from what Gemini actually emits; this one
cannot, because it IS what Gemini emitted.

16 scenes, 5 topics, each opened by a start_frame-referenced scene, continued by
a scene: chain, and closed by a closing B-Roll.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DirectorConfig          # noqa: E402
from app.schemas import VideoBlueprint         # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples"
GOLDEN = EXAMPLES / "run_0616c9e5"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Loaded once; every fixture hands out a deep copy so mutations can't leak
# between tests.
_GOLDEN_DICT = _load(GOLDEN / "blueprint.placeholder.json")
_GOLDEN_SELECTIONS = _load(GOLDEN / "selections.json")


@pytest.fixture
def bp_dict() -> dict:
    """The golden blueprint as a plain dict (placeholder ids, pre-L3)."""
    return deepcopy(_GOLDEN_DICT)


@pytest.fixture
def bp(bp_dict) -> VideoBlueprint:
    """The golden blueprint as a validated model. Mutable — pydantic v2 does not
    validate on assignment by default, which is what lets a test assign a
    deliberately invalid value and check the SEMANTIC validator catches it."""
    return VideoBlueprint.model_validate(bp_dict)


@pytest.fixture
def cfg() -> DirectorConfig:
    """Default director config. max_words = 2.5 * 8 = 20."""
    return DirectorConfig()


@pytest.fixture
def selections() -> list[dict]:
    return deepcopy(_GOLDEN_SELECTIONS["selections"])


# ---------------------------------------------------------------- helpers
def find(bp: VideoBlueprint, scene_id: str):
    """A scene by id, or fail loudly — a typo in a test id should not silently
    pass by mutating nothing."""
    for s in bp.scenes:
        if s.id == scene_id:
            return s
    raise AssertionError(f"no scene '{scene_id}' in the golden blueprint")


def first(bp: VideoBlueprint, **match):
    """First scene whose attributes match, e.g. first(bp, type='B-Roll')."""
    for s in bp.scenes:
        if all(getattr(s, k) == v for k, v in match.items()):
            return s
    raise AssertionError(f"no scene matching {match}")


def has(problems: list[str], *fragments: str) -> bool:
    """True when some problem string contains ALL the fragments. Substring
    matching keeps tests readable without pinning exact message wording — but
    note the wording IS a product surface here (it is fed back to the LLM), so
    these fragments are the load-bearing parts."""
    return any(all(f in p for f in fragments) for p in problems)
