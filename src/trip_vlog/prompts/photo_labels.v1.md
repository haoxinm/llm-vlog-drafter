---
id: photo_labels
version: 1
---
Task: label still photos for a travel-vlog editor. Each photo is announced by a text line giving its ID (segment_id); the photo follows.

Rules:
- Return exactly one entry per photo, in the order given, copying the segment_id exactly. Never invent, merge or skip photos.
- description: one or two plain sentences about what is visible (subject, setting, composition). No names, places, brands or activities that cannot be seen.
- visible_entities: short lower-case nouns for clearly visible things or people. Empty list if nothing is clear.
- event: a short phrase for what is happening, or null when nothing is happening or it is unclear.
- editorial_tags: one to four values from the allowed list that describe the editorial role of the photo.
- quality: sharpness and exposure each "low", "ok" or "good"; stability is always null for a photo. Do not discount a meaningful photo only because it is imperfect.
- interest: 0 (dull, filler) to 1 (strong, memorable); confidence: 0 to 1, how sure you are of the description.
- uncertain: true when the photo is too dark, blurry or ambiguous to describe reliably; then explain briefly in uncertainty_note, otherwise null.
- Never output timestamps. Faces and people may be counted or described generically; do not identify anyone.
