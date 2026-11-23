"""
Director configuration and Gemini client creation.

Contains only:

  * DirectorConfig — all settings the director (L2) reads. The first seven
    fields map 1:1 to schemas.ConfigIn (used by DirectorConfig(**config.json)),
    so renaming them breaks config loading. The remaining fields are internal
    pacing constants, configurable only in code.

  * make_client — the only place the Gemini API key is read.

load_dotenv() runs on import so .env is available everywhere.
"""

import os
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
from typing import Protocol
from dataclasses import dataclass
from app.schemas import AspectRatio, BlueprintType

load_dotenv()

@dataclass
class DirectorConfig:
    # --- user-supplied (mirrors schemas.ConfigIn field-for-field) ---
    language: str = "English"
    audience: str = "corporate learners"
    depth: str = "standard"
    objective: str = ""   # user-provided description of the video's goal
    style: str = (
        "modern flat 2D cartoon corporate explainer, bold clean outlines, "
        "cel-shading, muted color palette"
    )
    aspect_ratio: AspectRatio = "16:9"
    blueprint_type: BlueprintType = "opening"

    # Internal pacing limits used by prompt generation, validation, and the
    # length fill. 2.5 words/second = 150 words/minute. Speaking scenes are capped at:
    #   words_per_second × max_scene_seconds (default: 2.5 × 8 = 20 words),
    # and each one's `length` is floor(words / words_per_second), minimum 4s.
    # Increasing max_scene_seconds alone is insufficient because Veo only
    # supports 4/6/8-second clips.
    words_per_second: float = 2.5
    min_scene_seconds: float = 6.0
    max_scene_seconds: float = 8.0
    # Target B-Roll duration. Generated as a 4s clip (Veo minimum) then trimmed.
    # Opening blueprints always use a full 4s B-Roll for the title card.
    broll_length_seconds: float = 1

    # min_scene_seconds is interpolated into the prompt as `min_words` but is
    # not enforced by validate_blueprint — short scenes pass. Advisory only.


class DirectorLLM(Protocol):
    """Minimal interface the director needs: one text completion.

    Using a Protocol lets the director be unit-tested with stub LLMs instead of
    the live OpenAI implementation.
    """
    def complete(self, system: str, user: str) -> str: ...

def make_client(api_key_env: str = "HEYHI_GEMINI_API_KEY"):
    """Construct the Gemini client from the environment. Credential edge:
    the key is read here and nowhere else, and is never printed or persisted."""
    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"{api_key_env} not set in environment")
    # Pinned: JSON mode, Veo and Lyria need v1beta, and newer SDKs default to v1.
    return genai.Client(
        api_key=key, http_options=genai_types.HttpOptions(api_version="v1beta"))


def make_openai_client(api_key_env: str = "OPENAI_API_KEY"):
    """Construct the OpenAI client from the environment (director, L2). Same
    credential-edge rule as make_client: read here, never printed or persisted."""
    key = os.environ.get(api_key_env)
    if not key:
        raise RuntimeError(f"{api_key_env} not set in environment")
    from openai import OpenAI
    return OpenAI(api_key=key)
