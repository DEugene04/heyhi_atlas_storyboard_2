"""Scene prompt for the green-screen presenter clip (MiniMax H3, first-frame mode).

Kept dependency-free so the bench script can import it on the GPU box.

The clip starts from the start frame (app/utils/start_frame.py), which already shows
who is presenting, the room, and the green rectangle. The prompt therefore does not
describe any of that again: it says what happens, and what must not change. H3 has no
negative-prompt branch, so every constraint is phrased as what must appear. Speech goes
in <d> tags with a speaker id; the slide is composited over the green rectangle later.
"""
from __future__ import annotations


def first_frame_scene_prompt(narration: str) -> str:
    spoken = (narration or "").strip().replace('"', "'")
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
            " (S1) The presenter speaks to camera in calm, clear, professional English with "
            f"natural lip-sync: <d>[en] {spoken}</d> After the line ends, the lips stay closed."
        )
    return (
        f"integrated_multimodal_description: {scene}\n\n"
        "overall_soundscape: Quiet, clean room tone.\n\n"
        "non_diegetic_music: N/A"
    )
