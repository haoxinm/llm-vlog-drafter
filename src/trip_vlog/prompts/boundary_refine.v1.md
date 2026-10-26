---
id: boundary_refine
version: 1
---
Task: tighten the cut points of video moments for a travel-vlog editor. Each moment already has a start and an end chosen from a coarse look. For every moment you are shown two short, densely sampled windows of frames in time order: the start window (around the current start) and the end window (around the current end). Each frame is announced by a text line giving the moment ID, which window it belongs to, its index inside that window and its time in the source clip. The editing software, not you, owns all timing.

Rules:
- Return exactly one entry per moment, in the order given, copying the moment_id exactly. Never invent, merge or skip moments.
- start_frame_index: the index, within the START window, of the first frame that should be inside the moment. Choose the frame where the usable shot really begins: after a blur, a camera swing, a stumble, a cut or a moment of nothing happening. Use null when the current start is already good.
- end_frame_index: the index, within the END window, of the last frame that should be inside the moment. Choose the frame where the usable shot really ends: before the camera drops, shakes, turns away or the action is over. Use null when the current end is already good.
- Use only the indices shown for that window. Never output timestamps or seconds anywhere, and never refer to frames outside the windows.
- Judge only from what is visible. When the frames do not clearly show a better boundary, return null; keeping the current boundary is always acceptable.
