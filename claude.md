# This serves as the first md file that initiates this new pipeline

# Project Reference — Source-to-Video Pipeline

> **Purpose:** Transforms source materials (PDF, DOCX, PPTX, TXT) into AI-generated videos for corporate employee learning and training.
> **Current phase:** Director layer (Unfinished).

---

## 1. Stack & Conventions

| Item          | Value                  |
| ------------- | ---------------------- |
| Language      | Python                 |
| DB columns    | `snake_case`           |
| API responses | `snake_case`           |
| Director LLM  | Gemini 2.5 Flash       |
| Image model   | Gemini 2.5 Flash Image |
| Audio model   | Google Lyria 3 Clip    |
| Video model   | Veo 3.1 (Fast / Lite)  |
| TTS           | Azure TTS              |

---

## 2. Pipeline Overview

```
Source + Config layer
        ↓
Director layer  →  blueprint.json
        ↓
User selects characters + audio
        ↓
Asset generation (Gemini 2.5 Flash Image)
   ├── characters
   ├── backgrounds
   ├── start frames (one per group)
   └── keyframes
        ↓
Scene generation (Veo 3.1 Fast / Lite)
        ↓
External TTS layer (Azure TTS overlay)
        ↓
Music generation layer
    └── music_bg
        ↓
Renderer layer
        ↓
Final video
```

**Note**: After one scene of video-with-audio type is generated, integrate it with TTS and trim accordingly before creating the next Veo scene

Output path convention:

```
_storage/atlas_storyboard/run_0X/
├── backgrounds/
├── characters/
├── start_frame/
└── keyframe/
└── music_bg/
```

`run_0X` is set by the user via: `python -m ... --out _storage/atlas_storyboard/run_0X`

---

## 3. Layer 1 — Source + Config

User-facing entry point. Accepts source files and config. This layer will have a dedicated API to accept response from user.

**Source handling:** `extract.py` converts PDF / DOCX / PPTX / TXT → plain text, which becomes the Director layer's input.

### Config fields

| Field        | Description                                                                                   |
| ------------ | --------------------------------------------------------------------------------------------- |
| `language`   | Language the video will be generated in                                                       |
| `audience`   | Who the video is for                                                                          |
| `depth`      | `"standard"` or `"deep dive"` — fed to the Director layer                                     |
| `style`      | One of 6 video styles (see below)                                                             |
| `characters` | Character IDs chosen from the character library. Count allowed is defined by `blueprint.json` |
| `audios`     | Voice IDs chosen from the audio library, one per character                                    |
| `objective`  | The objective the user wants the video to fulfill                                             |

### Style options

1. **Dialogue Style**
2. **Narration Style**
3. **Story Style**
4. _TBD_
5. _TBD_
6. _TBD_

---

## 4. Layer 2 — Director

Uses **Gemini 2.5 Flash** to interpret the source text + config. Outputs `blueprint.json`.

### 4.1 Top-level schema

```json
{
  "style":      "<string>",
  "character_count": "<int>",
  "music_style": "<string>",
  "backgrounds":[ { "id": "...", "prompt": "..." } ],
  "scenes":     [ <scene> ],
  "start_frame": [ { "id": "...", "prompt": "...", "reference": ["<char_id>", "<bg_id>"] } ],
  "keyframe":   [ { "id": "...", "model": "...", "prompt": "...", "reference": ["<bg_id>"] } ]
}
```

| Field             | Notes                                                                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `style`           | A fixed prompt fragment appended to **every** `character` and `background` at image-generation time                                                          |
| `character_count` | The number of characters needed for this blueprint or video, this number is also used to limit the number of characters that the user can pass in the config |
| `music_style`     | The prompt for the background music → Google Lyria                                                                                                           |
| `backgrounds`     | One entry per backgrounds the video needs. `prompt` → Gemini 2.5 Flash Image (+ `style`) → saved to `run_0X/backgrounds/ → saved to `run_0X/backgrounds/`    |
| `start_frame`     | One per group (the scenes between two B-Rolls): the group's one character placed into a background. First frame of the group's first video-with-audio scene |
| `keyframe`        | A single frame of reference images built from a background (`reference` holds `bg_id`s) that is used for scenes with the type of broll                       |
| `scenes`          | Ordered list of scene objects (§4.2)                                                                                                                         |

### 4.2 Scene object schema

```json
{
  "id": "<string>",
  "task": "image-to-video",
  "model": "Veo 3.1 Lite",
  "characters": ["char_id_1"],
  "audios": [
    {
      "char_id": "char_id_1",
      "audio_id": "audio_id_1"
    }
  ],
  "text": "<string>",
  "prompt": "<string>",
  "safety": "Allow (Adult only)",
  "reference_from": "start_frame:<sf_id> | keyframe:<kf_id> | <prev_scene_id>",
  "type": "video-with-audio | B-Roll",
  "broll_role": "null | opening | closing | topic-transition",
  "length": "null | <number>",
  "aspect_ratio": "16:9 | 9:16",
  "topic": "<string> | null"
}
```

| Field            | Description                                                                                                                                                                                                                                                                      |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task`           | Always `image-to-video`                                                                                                                                                                                                                                                          |
| `model`          | Always `Veo 3.1 Lite`                                                                                                                                                                                                                                                            |
| `characters`     | Exactly one char id on a `video-with-audio` scene, the same one for every scene of its group (the scenes between two B-Rolls). It may change only after a B-Roll. `[]` for B-Roll. No dialogue between characters |
| `audios`         | Contains audio objects that contains a character id and their respective audio id                                                                                                                                                                                                |
| `text`           | Spoken dialogue. `""` for B-Roll                                                                                                                                                                                                                                                 |
| `prompt`         | Veo-optimized prompt. **Final prompt sent to Veo** = `<prompt>` + `Audio sync: The character says "<text>"`                                                                                                                                                                      |
| `safety`         | Veo safety parameter                                                                                                                                                                                                                                                             |
| `reference_from` | • `start_frame:<sf_id>` / `keyframe:<kf_id>` → attach that image as Veo input<br>• `<prev_scene_id>` → extract the **last frame** of that scene -- after it has been merged with TTS and trimmed by the TTS layer -- and use it as the input image, this ensures video continuity |
| `type`           | `video-with-audio` = character speaking on-screen, with sound<br>`B-Roll` = filler/transition, no sound                                                                                                                                                                          |
| `broll_role`     | `null` for `video-with-audio`; otherwise `opening` / `closing` / `topic-transition`                                                                                                                                                                                              |
| `length`         | Seconds. **Required for B-Roll** (Veo's minimum output is 4s — shorter B-Roll is trimmed downstream). For `video-with-audio` the Director leaves it `null` and it is filled from that scene's own word count: `max(4, floor(words / 2.5))` (150 wpm, rounded down) |
| `aspect_ratio`   | `16:9` or `9:16`                                                                                                                                                                                                                                                                 |
| `topic`          | **Required** for `video-with-audio`, optional for `B-Roll`                                                                                                                                                                                                                       |

---

### 4.3 Group rules (Director)

- A **group** is every scene between two B-Rolls; each B-Roll ends one group and starts the next.
- **One character per group**, the same in every scene of the group; it may change only after a B-Roll. No dialogue between characters.
- Each group has one `start_frame` (character + background), used by its first video-with-audio scene.
- Keep the performance simple: the character is centered, speaks to the camera, and uses only small body language. No object or background interaction.
- `length` for each video-with-audio scene is computed per scene (not per group) from its own word count: `max(4, floor(words / 2.5))`.

---

## 5. Layer 3 — User selects characters + audio

**Goal:** Allows the user to choose a list of characters and audio to use

**Process:**

1. The front-end (which is outside our scope) will display a list of character
2. The user will choose a number of characters based on the blueprint's character_count
3. For every character chosen, the user will choose a certain voice audio (from the audio list)
4. Therefore the same character id across scenes must have the same audio id
5. After the user submits this requests, update the blueprint.json with the chosen character and audio.

**Note**: Will have a dedicated API for this layer

---

## 6. Layer 4 — Asset generation (Gemini 2.5 Flash Image)

**Goal:** Generates background, start frames and keyframes

**Process:**

1. Take the backgrounds/start_frame/keyframes
2. For each backgrounds: loop through bp.backgrounds, generate from .prompt.
3. For each start frame: loop through bp.start_frame, generate from .prompt + attach the group's character image and the background (.reference).
4. For each keyframes: loop through bp.keyframe, generate from .prompt + attach the single background reference.
5. Save the background, keyframes and start frames with their respective id as the file name

**Note**: Characters are NOT generated here — user selects from library in Layer 3.

---

## 7. Layer 5 — Scene generation (Veo 3.1 Fast / Lite)

**Goal:** Generates the video scenes using Veo 3.1

**Process:**

1. Take the scenes properties and pass it into the Veo model
2. Include the reference_from and prompt as an attachment into the Veo's model parameter
3. If the reference_from is a previous scene_id (indicating a continuous shot) this layer should extract the last frame of the previous scene (which is already integrated with external TTS and trimmed accordingly) and use that last frame as an attachment for the image-to-video task.
4. Send every scene to Veo 3.1 Lite as an image-to-video task. The seed image is the start frame, the background (opening B-Roll), or the previous scene's last frame.
5. For each video-with-audio scene that is successfully generated, send it to layer 6 (External TTS), receive the integrated output from layer 6 and continue the next scene. Scenes that are considered B-Roll does not need to be passed to layer 6 (External TTS)

---

## 8. Layer 6 — External TTS

**Goal:** make voice consistent across Veo-generated clips.

**Process, per scene:**

1. Take the scene's `text`.
2. Split it into phrases on punctuation marks.
3. Send each phrase to **Azure TTS** → voiceover clips.
4. Transcribe the Veo clip's native audio with an LLM and run **diarization** to get per-word timings.
5. Overlay the Azure TTS phrases onto the video using those timings (this is what keeps lipsync accurate).
6. Trim the video according to the last TTS length of that scene. (e.g. scene 1 has 3 TTS, TTS 1 [00:00 - 00:02], TTS 2 [00:03 - 00:05], TTS [00:06 - 00:07]. This means the Veo video is trimmed to 7 seconds)
7. **Mute** Veo's original audio — only the Azure TTS track is audible. -> This video is what will be used as the last frame extractor for scenes that reference_from other scenes (continuous shot)

**Voice mapping:** the front-end passes the user's per-character voice selection (from Layer 1's `audios` config) into this layer; the layer maps each voice ID to its character.

---

## 9. Layer 7 — Music Generation Layer

**Goal:** Create background music from the prompt from blueprint.json

**Process**

1. Take the music_style from blueprint.json
2. Send prompt to **Google Lyria**
3. Save music background to \_storage/atlas_storyboard/run_0X/music_bg

**Voice mapping:** the front-end passes the user's per-character voice selection (from Layer 1's `audios` config) into this layer; the layer maps each voice ID to its character.

---

## 10. Layer 8 - Renderer Layer

1. Takes all generated scenes
2. Merges them together
3. Add greenscreen overlay transition after brolls with broll-type "opening" and "closing", before and after with broll-type "topic-transition", but the transition should be above the video layer so the overlays stacks above the video layer, not video -> transition -> video. But Video -transition-> video
4. Add subtitle
5. Add music in the background from the music generation layer
6. Loop music background to fit the merged video, add a crossfade to ensure a smooth loop and make the loops seamless

**Note**: Will have a dedicate API so that the front-end can request the successfully rendered video.
