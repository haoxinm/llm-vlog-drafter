---
id: timeline_planner_input
version: 1
---
Choose the clips for this approved outline of a travel vlog.

## Project settings
{{settings}}

## Rules
{{constraints}}

## What the editor can do
{{capabilities}}

## Approved outline ({{scene_count}} scenes)
Scene budgets add up to {{target_seconds}} seconds. Each scene lists its candidate moments, in the outline's order; use only those. Each moment line is a JSON header with fields: id (use this exactly), kind (video or photo), when (local capture time), day (day of the trip), at (seconds into the clip), dur (length of the moment in seconds), use (video only: the longest clip you may take from it, in seconds), best (video only: length of its best sub-range), orient, interest and conf (0 to 1), quality, tags, audio (measured audio hints), dup (moments sharing a dup value are near-duplicates), uncertain, unlabeled, speech (the moment has spoken words). A block that follows a header holds text taken from the media for that moment.

{{untrusted_preamble}}

{{outline}}

## What to return
A JSON selection for a vlog of about {{target_seconds}} seconds (the primary_video clips of all scenes add up to {{target_seconds}} seconds, within {{tolerance_seconds}} s; each scene within its own tolerance).
