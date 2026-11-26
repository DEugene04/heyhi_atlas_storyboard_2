"""Scene prompt for the green-screen presenter clip (MiniMax H3, first-frame mode).

Kept dependency-free so the bench script can import it on the GPU box.

The clip starts from the start frame (app/utils/start_frame.py), which already shows
who is presenting, the room, and the green rectangle. The prompt therefore does not
describe any of that again: it says what happens, and what must not change. H3 has no
negative-prompt branch, so every constraint is phrased as what must appear. Speech goes
in <d> tags with a speaker id; the slide is composited over the green rectangle later.

The spoken language is the project's language: it is named in the <d> tag and in the
delivery sentence, because H3 would otherwise assume English.
"""
from __future__ import annotations

# The front end takes free text for the language; these spellings all mean the same
# language to a person, so map them to one name H3's guide uses for its tags.
_ALIASES = {
    "indonesian": ("indonesia",),          # also matches "Bahasa Indonesia"
    "thai": ("thai", "ภาษาไทย"),
}


def language_name(language: str | None) -> str:
    """Canonical language name for the prompt; English when none is given."""
    text = (language or "").strip()
    lowered = text.lower()
    if not lowered:
        return "English"
    for name, keys in _ALIASES.items():
        if any(key in lowered for key in keys):
            return name.capitalize()
    return text


def first_frame_scene_prompt(narration: str, language: str | None = "English") -> str:
    spoken = (narration or "").strip().replace('"', "'")
    name = language_name(language)
    # English keeps the short tag the flow was validated with; other languages are
    # tagged by name, as in the official H3 examples.
    tag = "en" if name == "English" else name
    scene = (
        "[Shot 1] One continuous take starting from the first frame. The presenter gestures "
        "calmly toward the green rectangle, glances at it and back to camera, with subtle, "
        "relaxed body movement. The camera is locked and static. The green rectangle on the "
        "right stays one flat, even green with crisp straight edges, in the same place, for "
        "the whole clip, never overlapped by the presenter. The room, its colors and its "
        "lighting stay exactly the same."
    )
    if spoken:
        scene += (
            f" (S1) The presenter speaks to camera in calm, clear, professional {name} with "
            f"natural lip-sync: <d>[{tag}] {spoken}</d> After the line ends, the lips stay closed."
        )
    return (
        f"integrated_multimodal_description: {scene}\n\n"
        "overall_soundscape: Quiet, clean room tone.\n\n"
        "non_diegetic_music: N/A"
    )
