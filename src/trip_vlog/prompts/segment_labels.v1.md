---
id: segment_labels
version: 1
---
Task: label short video segments for a travel-vlog editor. Each segment is shown as several still frames sampled in time order; each frame is announced by a text line giving the segment ID, the frame index and its time in the source clip. The editing software, not you, owns all timing.

Rules:
- Return exactly one entry per segment, in the order given, copying the segment_id exactly. Never invent, merge or skip segments.
- description: one or two plain sentences about what is visible across the frames (subject, setting, camera view, apparent motion between frames). No names, places, brands or activities that cannot be seen. Do not describe sound.
- visible_entities: short lower-case nouns for clearly visible things or people (for example "ocean", "street market", "two people"). Empty list if nothing is clear.
- event: a short phrase for what is happening across the frames, or null when nothing is happening or it is unclear.
- editorial_tags: one to four values from the allowed list that describe the editorial role of the segment.
- quality: sharpness, stability and exposure each "low", "ok" or "good", judged from the frames; null when you cannot judge (for example stability from a single frame).
- interest: 0 (dull, filler) to 1 (strong, memorable); confidence: 0 to 1, how sure you are of the description.
- uncertain: true when the frames are too dark, blurry, ambiguous or too few to describe reliably; then explain briefly in uncertainty_note, otherwise null.
- best_frames: when only part of the segment is clearly the best moment, give first_index and last_index of the best consecutive frames, using only the frame indices shown for that segment; null when the whole segment is equally good. Never output timestamps or seconds anywhere.
