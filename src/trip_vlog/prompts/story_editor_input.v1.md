---
id: story_editor_input
version: 1
---
Plan the story outline for this trip vlog.

## Project settings
{{settings}}

## Constraints
{{constraints}}

## Candidate moments ({{moment_count}} shown)
Each line is a JSON header with fields: id (use this exactly), n (chronological position), kind (video or photo), when (local capture time), day (day of the trip), at (seconds into the clip), dur (seconds; photos show for about that long), orient, interest and conf (0 to 1), quality, tags, audio (measured audio hints), dup (moments sharing a dup value are near-duplicates), uncertain, unlabeled, speech (the moment has spoken words). A block that follows a header holds text taken from the media for that moment.

{{untrusted_preamble}}

{{catalog}}

## What to return
A JSON story outline for a vlog of about {{target_seconds}} seconds (scene budgets summing to {{target_seconds}}, within {{tolerance_seconds}} s).
