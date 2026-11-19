"""
director.validate_blueprint — the ~25 semantic rules.

WHY THIS FILE MATTERS MOST. validate_blueprint is not a safety net bolted onto
the director; it is the feedback signal the layer runs on. direct() hands its
output straight back to Gemini as the instruction for the next attempt, so a
rule that stops firing does not raise an error anywhere — it silently becomes
optional, and the blueprints quietly get worse.

That failure is invisible end-to-end. You would only notice weeks later, in the
videos. Which is exactly why it needs unit tests.

Each test mutates ONE field of a known-clean blueprint and asserts the matching
problem appears. The assertions match on message fragments rather than exact
strings, because the messages are prompt text aimed at an LLM and get reworded —
but the fragments chosen are the load-bearing parts an LLM needs to act on.
"""
from __future__ import annotations

from app.pipeline.director import validate_blueprint
from conftest import find, first, has


# ---------------------------------------------------------------- baseline
def test_golden_blueprint_is_clean(bp, cfg):
    """The fixture must validate with zero problems, or every other test in this
    file is measuring the wrong thing."""
    assert validate_blueprint(bp, cfg) == []


# ---------------------------------------------------------------- counts + voices
def test_character_count_must_match_distinct_char_ids(bp, cfg):
    bp.character_count = 7
    assert has(validate_blueprint(bp, cfg), "character_count mismatch")


def test_same_character_may_not_have_two_voices(bp, cfg):
    """Voice stability across scenes — a character whose voice changes mid-video
    is the single most obvious quality failure in the output."""
    sc = first(bp, type="video-with-audio")
    sc.audios[0].audio_id = "some_other_voice"
    assert has(validate_blueprint(bp, cfg), "inconsistent audio_ids")


def test_duplicate_background_ids_rejected(bp, cfg):
    bp.backgrounds.append(bp.backgrounds[0].model_copy())
    assert has(validate_blueprint(bp, cfg), "duplicate background ids")


def test_duplicate_scene_ids_rejected(bp, cfg):
    bp.scenes[2].id = bp.scenes[1].id
    assert has(validate_blueprint(bp, cfg), "duplicate scene id")


def test_duplicate_storyboard_ids_rejected(bp, cfg):
    dup = bp.storyboard[0].model_copy()
    bp.storyboard.append(dup)
    assert has(validate_blueprint(bp, cfg), "duplicate storyboard id")


def test_storyboard_reference_must_be_known(bp, cfg):
    bp.storyboard[0].reference = ["char_nobody"]
    assert has(validate_blueprint(bp, cfg), "reference 'char_nobody'")


# ---------------------------------------------------------------- reference_from
def test_ambiguous_reference_from_is_reported_not_raised(bp, cfg):
    """REGRESSION GUARD (open-questions #2).

    A bare keyword makes parse_reference return prefix=None. The opening-B-Roll
    branch used to test `None not in ("background")` — a string, not a tuple —
    which raised TypeError. direct() catches ValidationError/JSONDecodeError/
    ValueError but not TypeError, so it escaped /pipeline/direct as a 500
    instead of becoming a problem the retry loop could repair.

    This test fails with TypeError if the trailing comma is ever lost again.
    """
    sc = first(bp, broll_role="opening")
    sc.reference_from = "background"          # bare keyword, no id
    problems = validate_blueprint(bp, cfg)    # must not raise
    assert has(problems, "ambiguous")


def test_unknown_storyboard_reference(bp, cfg):
    sc = find(bp, "scene_basics_1")
    sc.reference_from = "storyboard:sb_does_not_exist"
    assert has(validate_blueprint(bp, cfg), "unknown storyboard")


def test_unknown_background_reference(bp, cfg):
    sc = first(bp, broll_role="opening")
    sc.reference_from = "background:bg_nope"
    assert has(validate_blueprint(bp, cfg), "unknown background")


def test_scene_reference_must_point_backwards(bp, cfg):
    """Forward references are caught structurally: `seen_scenes` only contains
    scenes already walked, so a later id simply isn't in the set yet."""
    sc = find(bp, "scene_basics_2")
    sc.reference_from = "scene:scene_carry_forward_1"     # much later
    assert has(validate_blueprint(bp, cfg), "not an earlier scene id")


def test_task_must_match_reference_prefix(bp, cfg):
    sc = find(bp, "scene_basics_1")            # storyboard: -> reference-to-video
    sc.task = "image-to-video"
    assert has(validate_blueprint(bp, cfg), "does not match reference_from")


def test_model_must_match_reference_prefix(bp, cfg):
    sc = find(bp, "scene_basics_1")            # storyboard: -> Veo 3.1 Fast
    sc.model = "Veo 3.1 Lite"
    assert has(validate_blueprint(bp, cfg), "model=", "does not match")


# ---------------------------------------------------------------- speaking scenes
def test_speaking_scene_needs_audio_for_every_character(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.audios = []
    assert has(validate_blueprint(bp, cfg), "have no audio mapping")


def test_audio_for_character_not_in_scene_rejected(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.audios[0].char_id = "char_ghost"
    assert has(validate_blueprint(bp, cfg), "char_ids not in")


def test_speaking_scene_requires_text(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.text = "   "
    assert has(validate_blueprint(bp, cfg), "requires non-empty 'text'")


def test_word_budget_enforced(bp, cfg):
    """max_words = words_per_second * max_scene_seconds = 2.5 * 8 = 20.

    This is the rule that keeps a scene's narration inside the 8s Veo clip. Note
    build_director_prompt derives the same number for the prompt — if the two
    ever disagree, the retry loop cannot converge, because the model is told one
    ceiling and judged against another.
    """
    sc = first(bp, type="video-with-audio")
    sc.text = " ".join(["word"] * 21)
    assert has(validate_blueprint(bp, cfg), "21 words", "max 20")


def test_word_budget_follows_config(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.text = " ".join(["word"] * 21)
    cfg.max_scene_seconds = 12.0                     # -> max_words 30
    assert not has(validate_blueprint(bp, cfg), "max 20")


def test_speaking_scene_must_not_set_length(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.length = 6.0
    assert has(validate_blueprint(bp, cfg), "must leave 'length' null")


def test_speaking_scene_must_not_set_broll_role(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.broll_role = "closing"
    assert has(validate_blueprint(bp, cfg), "must leave 'broll_role' null")


def test_speaking_scene_needs_a_speaker(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.characters = []
    sc.audios = []
    assert has(validate_blueprint(bp, cfg), "at least one")


def test_speaking_scene_prompt_must_describe_mouth_motion(bp, cfg):
    """Without this clause Veo animates a still face and the TTS overlay has
    nothing to sync to."""
    sc = first(bp, type="video-with-audio")
    # "lip" not "lips" — the prompt also contains "lip-sync", and the validator
    # accepts any substring "lip", so a narrower replace leaves the check passing.
    sc.prompt = sc.prompt.replace("lip", "hand").replace("mouth", "hand")
    assert has(validate_blueprint(bp, cfg), "continuous mouth/lip movement")


# ---------------------------------------------------------------- B-Roll
def test_broll_must_have_empty_text(bp, cfg):
    sc = first(bp, type="B-Roll")
    sc.text = "this B-Roll should not speak"
    assert has(validate_blueprint(bp, cfg), "must have empty text")


def test_broll_requires_positive_length(bp, cfg):
    sc = first(bp, type="B-Roll")
    sc.length = None
    assert has(validate_blueprint(bp, cfg), "positive 'length'")


def test_broll_length_capped_at_max_scene_seconds(bp, cfg):
    sc = first(bp, type="B-Roll")
    sc.length = 99.0
    assert has(validate_blueprint(bp, cfg), "exceeds max")


def test_broll_requires_role(bp, cfg):
    sc = first(bp, type="B-Roll")
    sc.broll_role = None
    assert has(validate_blueprint(bp, cfg), "must set 'broll_role'")


def test_opening_broll_must_reference_a_background(bp, cfg):
    sc = first(bp, broll_role="opening")
    sc.reference_from = "storyboard:sb_basics_1"
    assert has(validate_blueprint(bp, cfg), "must be 'background:...'")


def test_closing_broll_must_reference_immediately_previous_scene(bp, cfg):
    sc = find(bp, "broll_basics_closing")
    sc.reference_from = "scene:scene_basics_1"      # one too far back
    assert has(validate_blueprint(bp, cfg), "immediately-previous")


def test_closing_broll_must_hold_the_previous_frame(bp, cfg):
    """A closing B-Roll is the previous frame held, not a new shot. The phrase is
    checked literally because it is what tells Veo to inherit rather than
    reinvent (AI RULE #6)."""
    sc = find(bp, "broll_basics_closing")
    sc.prompt = sc.prompt.replace("Consistent with previous frame", "A new empty office")
    assert has(validate_blueprint(bp, cfg), "must HOLD the previous frame")


# ---------------------------------------------------------------- prompt content
def test_missing_prompt_labels_rejected(bp, cfg):
    """The six labels are a parsing contract, not a style preference."""
    sc = bp.scenes[1]
    sc.prompt = sc.prompt.replace("Ambient noise:", "Sounds:")
    assert has(validate_blueprint(bp, cfg), "missing labels")


def test_wordless_clause_required(bp, cfg):
    """AI RULE #3 — without it Veo hallucinates signage and gibberish text."""
    sc = bp.scenes[1]
    sc.prompt = sc.prompt.replace("wordless", "").replace("no signage", "")
    assert has(validate_blueprint(bp, cfg), "wordless-surfaces clause")


def test_forbidden_negations_rejected(bp, cfg):
    """AI RULE #1 — Veo acts OUT a negation rather than avoiding it. Tell it
    'not speaking' and you get a deliberate not-speaking face."""
    sc = first(bp, type="B-Roll")
    sc.prompt += " The character is not speaking, mouth closed."
    problems = validate_blueprint(bp, cfg)
    assert has(problems, "forbidden pure-negation")


def test_storyboard_scene_must_instruct_veo_to_skip_the_grid(bp, cfg):
    """Without this, Veo animates the 2x2 grid itself as the first frames."""
    sc = find(bp, "scene_basics_1")
    sc.prompt = sc.prompt.replace("storyboard grid", "reference")
    assert has(validate_blueprint(bp, cfg), "NOT to display the storyboard grid")


# ---------------------------------------------------------------- topic boundaries
def test_topic_change_without_closing_broll_rejected(bp, cfg):
    """B-Roll marks topic ENDS. Removing the boundary marker must be caught, or
    two topics run together with no visual break."""
    bp.scenes = [s for s in bp.scenes if s.id != "broll_basics_closing"]
    assert has(validate_blueprint(bp, cfg), "no closing B-Roll marking the boundary")


def test_closing_broll_inside_a_topic_rejected(bp, cfg):
    """The mirror image: a closing B-Roll between two scenes that SHARE a topic
    breaks the sentence into two, which AI RULE #4 forbids."""
    sc = find(bp, "broll_basics_closing")
    sc.topic = "Annual Leave Basics"
    find(bp, "scene_earning_period_1").topic = "Annual Leave Basics"
    find(bp, "scene_earning_period_1").reference_from = "scene:broll_basics_closing"
    find(bp, "scene_earning_period_1").task = "image-to-video"
    find(bp, "scene_earning_period_1").model = "Veo 3.1 Lite"
    assert has(validate_blueprint(bp, cfg), "sharing topic")


def test_topic_continuation_must_chain_not_restart(bp, cfg):
    """A second scene in the same topic must seed from the previous clip, not
    open a fresh storyboard — otherwise the character visibly resets."""
    sc = find(bp, "scene_basics_2")
    sc.reference_from = "storyboard:sb_basics_1"
    sc.task = "reference-to-video"
    sc.model = "Veo 3.1 Fast"
    assert has(validate_blueprint(bp, cfg), "must chain from previous scene")


def test_topic_start_must_use_a_storyboard(bp, cfg):
    sc = find(bp, "scene_earning_period_1")
    sc.reference_from = "scene:broll_basics_closing"
    sc.task = "image-to-video"
    sc.model = "Veo 3.1 Lite"
    assert has(validate_blueprint(bp, cfg), "must be", "storyboard:<sb_id>")


def test_speaking_scene_requires_a_topic(bp, cfg):
    sc = first(bp, type="video-with-audio")
    sc.topic = None
    assert has(validate_blueprint(bp, cfg), "must set 'topic'")


# ---------------------------------------------------------------- documented gap
def test_final_topic_may_end_without_a_closing_broll(bp, cfg):
    """DOCUMENTS A GAP, does not assert desired behaviour.

    DIRECTOR_RULES says "Every topic ends with a closing B-Roll, including the
    final topic." The validator cannot enforce that for the LAST topic: the
    boundary check only fires when a NEW speaking scene appears with a different
    topic, and after the final topic there is none.

    So dropping the last closing B-Roll produces ZERO problems. The renderer
    then has no `closing` placement at the end and the video ends on a cut.

    If that rule is ever enforced, this test should start failing — change it
    then. It exists so the gap is discovered here rather than in a finished
    video. See open-questions.md.
    """
    bp.scenes = [s for s in bp.scenes if s.id != "broll_carry_forward_closing"]
    assert validate_blueprint(bp, cfg) == []


# ------------------------------------------------------ aspect_ratio (#13)
def test_a_scene_in_the_wrong_aspect_ratio_is_rejected(bp, cfg):
    """ConfigIn pins 16:9, but that only stops the USER asking for portrait.
    Scene.aspect_ratio still admits "9:16", and until this check existed a
    director that emitted one produced a blueprint that validated clean and
    then asked Veo for portrait video against landscape reference images."""
    find(bp, "broll_opening").aspect_ratio = "9:16"
    problems = validate_blueprint(bp, cfg)
    assert has(problems, "broll_opening", "aspect_ratio", "16:9")


def test_mixed_aspect_ratios_are_rejected(bp, cfg):
    """Doubles as the uniformity check the renderer needs: merge_scenes
    normalises every scene to the FIRST scene's geometry with scale+pad, so an
    off-ratio scene is silently letterboxed rather than erroring."""
    bp.scenes[1].aspect_ratio = "9:16"
    bp.scenes[2].aspect_ratio = "9:16"
    problems = validate_blueprint(bp, cfg)
    assert sum("aspect_ratio" in p for p in problems) == 2


def test_the_check_follows_config_rather_than_hardcoding_169(bp, cfg):
    """If portrait is ever unlocked, this check must not be the thing that
    blocks it. A 9:16 run should accept 9:16 scenes and reject 16:9 ones."""
    import dataclasses
    portrait = dataclasses.replace(cfg, aspect_ratio="9:16")

    assert any("aspect_ratio" in p for p in validate_blueprint(bp, portrait)), \
        "16:9 scenes must be rejected on a 9:16 run"

    for s in bp.scenes:
        s.aspect_ratio = "9:16"
    assert not any("aspect_ratio" in p for p in validate_blueprint(bp, portrait))


def test_the_prompt_states_one_value_rather_than_offering_a_choice(cfg):
    """The scene schema in DIRECTOR_RULES used to read `"16:9" | "9:16"`, which
    told the model both were available while the config had already decided.
    A rule stated nowhere and checked nowhere is how #13 stayed open."""
    from app.pipeline.director import build_director_prompt
    system, _ = build_director_prompt("SOURCE", cfg)
    line = next(l for l in system.splitlines() if '"aspect_ratio"' in l)
    assert cfg.aspect_ratio in line
    assert "|" not in line, f"still offering a choice: {line.strip()}"


# ---------------------------------------------------------------- coverage
def test_too_few_topics_is_a_problem(bp, cfg):
    """A one-topic video for a long source was accepted before this rule."""
    topics = {s.topic for s in bp.scenes if s.type == "video-with-audio"}
    assert has(validate_blueprint(bp, cfg, min_topics=len(topics) + 1), "coverage")
    assert not has(validate_blueprint(bp, cfg, min_topics=len(topics)), "coverage")


def test_topic_bounds_scale_with_depth_and_source_length():
    from app.pipeline.director import topic_bounds
    long_src = "word " * 5000
    assert topic_bounds("standard", long_src) == (3, 5)
    assert topic_bounds("deep dive", long_src) == (4, 8)
    assert topic_bounds("anything else", long_src) == (3, 5)
    # a short source can't fill 3 topics: the floor shrinks, never below 1
    assert topic_bounds("standard", "word " * 200) == (1, 5)
    assert topic_bounds("deep dive", "") == (1, 8)


def test_prompt_states_the_topic_range():
    from app.config import DirectorConfig
    from app.pipeline.director import build_director_prompt
    system, _ = build_director_prompt("word " * 5000, DirectorConfig(depth="deep dive"))
    assert "between 4 and 8 distinct topics" in system


def test_direct_derives_character_count_instead_of_retrying(bp_dict, cfg):
    """GPT-4o declares the wrong character_count and repeats it after feedback.
    direct() computes it from the scenes, so a miscount costs no retry."""
    import json
    from app.pipeline.director import direct

    bp_dict["character_count"] = 99

    class OneShot:
        calls = 0
        def complete(self, system, user):
            OneShot.calls += 1
            return json.dumps(bp_dict)

    src = "word " * 5000
    out = direct(src, cfg, OneShot(), max_retries=0)
    used = {c for s in out.scenes for c in s.characters}
    assert out.character_count == len(used)
    assert OneShot.calls == 1
