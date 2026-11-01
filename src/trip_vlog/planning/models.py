"""Selection-plan and edit-plan models (DESIGN §8.3, §10). The committed JSON Schemas are generated from these.

The selection plan is LLM-facing: moment IDs, target durations and anchors only, no source timestamps.
The edit plan is the resolver output: exact source ranges, frame counts and timeline positions.
Neither contains FilmCraft IDs or ticks. Times are seconds (floats) with exact integer frame counts alongside.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trip_vlog.state import stable_hash

SCHEMA_VERSION = "1.0"
SCHEMA_ID_PREFIX = "urn:trip-vlog:schema:"

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,80}$"
ASSET_ID_PATTERN = r"^asset_[0-9a-f]{16}$"
ASPECT_PATTERN = r"^[1-9][0-9]{0,2}:[1-9][0-9]{0,2}$"
FPS_PATTERN = r"^[1-9][0-9]{0,5}(/[1-9][0-9]{0,6})?$"
HASH_PATTERN = r"^[0-9a-f]{64}$"

Anchor = Literal["start", "center", "end", "best"]
TrackRole = Literal["primary_video", "overlay", "natural_audio", "music", "narration"]
AudioPolicy = Literal["natural_sound", "ambient_low", "muted"]
TransitionType = Literal["cut", "dissolve"]
TransitionAlignment = Literal["center", "start_at_cut", "end_at_cut"]
MediaKind = Literal["video", "image", "audio"]
MusicPolicy = Literal["none", "user_supplied_only"]



class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


# ---- selection plan (LLM-facing) --------------------------------------------------------------

class SelectionTransition(_Model):
    type: TransitionType
    duration_seconds: float = Field(ge=0, le=5)


class SelectionClip(_Model):
    clip_instance_id: str = Field(pattern=ID_PATTERN)
    moment_id: str = Field(pattern=ID_PATTERN)
    target_duration_seconds: float = Field(gt=0, le=3600)
    anchor: Anchor
    track_role: TrackRole
    audio_policy: AudioPolicy
    transition_in: SelectionTransition
    rationale: str = Field(max_length=2000)
    intentional_repeat_reason: str | None = Field(None, max_length=2000)


class SelectionScene(_Model):
    scene_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(max_length=200)
    purpose: str = Field(max_length=500)
    target_duration_seconds: float = Field(gt=0, le=7200)
    clips: list[SelectionClip] = Field(min_length=1, max_length=500)


class SelectionPlan(_Model):
    schema_version: Literal["1.0"]
    planner: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    scenes: list[SelectionScene] = Field(min_length=1, max_length=200)
    uncertainties: list[str] = Field(max_length=100)


# ---- edit plan (resolver output) --------------------------------------------------------------

class ProjectSettings(_Model):
    title: str = Field(max_length=200)
    target_duration_seconds: float = Field(gt=0, le=14400)
    aspect_ratio: str = Field(pattern=ASPECT_PATTERN)
    frame_rate: str = Field(pattern=r"^[0-9]+([./][0-9]+)?$")


class SequenceSettings(_Model):
    width: int = Field(ge=16, le=16384)
    height: int = Field(ge=16, le=16384)
    fps: str = Field(pattern=FPS_PATTERN)  # rational string, e.g. "30" or "30000/1001"
    aspect_ratio: str = Field(pattern=ASPECT_PATTERN)
    still_duration_seconds: float = Field(gt=0, le=600)  # default display time for still images


class EditTransition(_Model):
    type: TransitionType
    duration_seconds: float = Field(ge=0, le=5)
    duration_frames: int = Field(ge=0)
    # center: handle >= d/2 each side; start_at_cut: outgoing handle >= d; end_at_cut: incoming handle >= d
    alignment: TransitionAlignment


class EditClip(_Model):
    clip_instance_id: str = Field(pattern=ID_PATTERN)
    asset_id: str = Field(pattern=ASSET_ID_PATTERN)
    moment_id: str = Field(pattern=ID_PATTERN)
    media_type: MediaKind
    anchor: Anchor
    track_role: TrackRole
    audio_policy: AudioPolicy
    requested_duration_seconds: float = Field(gt=0, le=3600)
    source_in_seconds: float | None = Field(ge=0)   # required; null for still images
    source_out_seconds: float | None = Field(ge=0)  # required; null for still images
    source_in_frame: int | None = Field(ge=0)       # sequence frames; null for still images
    source_out_frame: int | None = Field(ge=0)
    timeline_start_frame: int = Field(ge=0)
    duration_frames: int = Field(ge=1)
    transition_in: EditTransition
    resolver_notes: list[str] = Field(max_length=50)
    rationale: str = Field(max_length=2000)
    intentional_repeat_reason: str | None = Field(None, max_length=2000)


class SceneMarker(_Model):
    marker_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=200)
    frame: int = Field(ge=0)
    scene_id: str = Field(pattern=ID_PATTERN)


class EditScene(_Model):
    scene_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(max_length=200)
    purpose: str = Field(max_length=500)
    target_duration_seconds: float = Field(gt=0, le=7200)
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(ge=0)
    clips: list[EditClip] = Field(min_length=1, max_length=500)


class AudioSettings(_Model):
    music_policy: MusicPolicy
    preserve_natural_audio: bool
    duck_music_under_speech: bool


class EditPlan(_Model):
    schema_version: Literal["1.0"]
    plan_hash: str = Field(pattern=HASH_PATTERN)             # sha256 of the canonical plan without this field
    selection_plan_hash: str = Field(pattern=HASH_PATTERN)   # the selection plan this was resolved from
    project: ProjectSettings
    sequence: SequenceSettings
    scenes: list[EditScene] = Field(min_length=1, max_length=200)
    markers: list[SceneMarker]
    audio: AudioSettings
    total_duration_frames: int = Field(ge=1)
    total_duration_seconds: float = Field(gt=0)
    uncertainties: list[str] = Field(max_length=200)


# ---- helpers ----------------------------------------------------------------------------------

def to_json_dict(plan: BaseModel) -> dict[str, Any]:
    return plan.model_dump(mode="json")


def selection_plan_hash(plan: SelectionPlan) -> str:
    return stable_hash(to_json_dict(plan))


def edit_plan_hash(plan: EditPlan) -> str:
    """sha256 over the canonical JSON of the plan with `plan_hash` excluded."""
    d = to_json_dict(plan)
    d.pop("plan_hash", None)
    return stable_hash(d)


def with_hash(plan: EditPlan) -> EditPlan:
    return plan.model_copy(update={"plan_hash": edit_plan_hash(plan)})


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "selection_plan": SelectionPlan,
    "edit_plan": EditPlan,
}


def json_schema(name: str) -> dict[str, Any]:
    """JSON Schema (2020-12) for 'selection_plan' or 'edit_plan', generated from the pydantic models."""
    model = SCHEMA_MODELS[name]
    schema = model.model_json_schema(mode="validation")
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{SCHEMA_ID_PREFIX}{name}:{SCHEMA_VERSION}", **schema}
    schema["description"] = {
        "selection_plan": "LLM-facing selection plan: moment IDs, target durations, anchors. No source timestamps.",
        "edit_plan": "Resolved edit plan: exact source ranges, frame counts and timeline positions. Application-independent.",
    }[name]
    return json.loads(json.dumps(schema))  # plain data


def schema_filename(name: str) -> str:
    return f"{name}.schema.json"


def write_schemas(directory) -> list:
    """Write both schemas into `directory` (atomic, UTF-8, sorted keys). Returns the paths written."""
    from pathlib import Path

    from trip_vlog.state import atomic_write_json

    out = []
    for name in SCHEMA_MODELS:
        p = Path(directory) / schema_filename(name)
        atomic_write_json(p, json_schema(name))
        out.append(p)
    return out


if __name__ == "__main__":  # python -m trip_vlog.planning.models [dir]
    import sys
    from pathlib import Path

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3] / "schemas"
    for p in write_schemas(target):
        print(p)
