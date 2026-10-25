"""Compare a dumped Resolve timeline with an expected timeline (the checks of DESIGN 15.2 / 15.4)."""
from __future__ import annotations

import os
from typing import Any

from trip_vlog.resolve.api import to_number
from trip_vlog.resolve.dump import IMAGE_EXTS, SEQ_RE, TOL_FRAMES
from trip_vlog.resolve.paths import paths_equal

REQUIRED_CHECKS = ("import", "fps", "duration", "clip_order", "record_inout", "source_in", "media_online",
                   "image_sequence_misdetected")


def _r(name: str, status: str, detail: str = "", required: bool | None = None) -> dict:
    return {"check": name, "status": status, "detail": detail,
            "required": (name in REQUIRED_CHECKS) if required is None else required}


def _within(a: float | None, b: float | None, tol: int = TOL_FRAMES) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _item_key(name: Any, path: Any) -> set[str]:
    keys = set()
    if name:
        keys.add(str(name).lower())
    if path:
        keys.add(os.path.basename(str(path).replace("\\", "/")).lower())
    return keys


def match_sequence(clip: dict, item: dict) -> dict | None:
    """If Resolve read an expected single file as an image sequence (still_1080p.png -> still_[1080-1229]p.png,
    the digits taken as a frame number), describe the match, else None. Matches by name (prefix + a number in the
    bracket range + suffix equals the expected file name), or, failing that, by an image extension equal to the
    expected one at the expected record range."""
    exp = os.path.basename(str(clip.get("source_path") or clip.get("name") or "").replace("\\", "/"))
    if not exp:
        return None
    cands = []
    for src in (item.get("media_path"), item.get("name")):
        if src:
            base = os.path.basename(str(src).replace("\\", "/"))
            m = SEQ_RE.match(base)
            if m:
                cands.append((base, m))
    if not cands:
        return None
    low = exp.lower()
    for base, m in cands:
        pre, lo, hi, suf = m.group(1).lower(), int(m.group(2)), int(m.group(3)), m.group(4).lower()
        if len(low) > len(pre) + len(suf) and low.startswith(pre) and low.endswith(suf):
            mid = low[len(pre):len(low) - len(suf)]
            if mid.isdigit() and lo <= int(mid) <= hi:
                return {"how": "name", "expected_file": exp, "resolve_name": item.get("name"),
                        "resolve_path": item.get("media_path"), "pattern": base}
    ext = os.path.splitext(exp)[1].lower()
    if ext in IMAGE_EXTS and os.path.splitext(cands[0][0])[1].lower() == ext \
            and _within(clip.get("record_start_frame"), item.get("start")) and _within(clip.get("record_end_frame"), item.get("end")):
        return {"how": "position", "expected_file": exp, "resolve_name": item.get("name"),
                "resolve_path": item.get("media_path"), "pattern": cands[0][0]}
    return None


def align_tracks(expected: dict, actual: dict) -> dict[str, Any]:
    """Pair every populated expected track with the actual track at the same position among populated tracks of
    its kind. Per pair: media clips (``required``), optional no-media clips (``optional``, titles/graphics) and the
    actual items (``rest``) that should line up with ``required`` by order. Transitions are split off first."""
    exp_by_kind: dict[str, list[dict]] = {"video": [], "audio": []}
    for t in expected.get("tracks", []):
        exp_by_kind.setdefault(t.get("kind", "video"), []).append(t)
    act_by_kind: dict[str, list[dict]] = {"video": [], "audio": []}
    found_tr: list[dict] = []
    for t in actual.get("tracks", []):
        # transitions are items too, but are neither clips nor media: split them off
        for it in t["items"]:
            if it.get("kind") == "transition":
                found_tr.append({**it, "track": f"{t['kind'][0].upper()}{t.get('index', '?')}"})
        act_by_kind.setdefault(t["kind"], []).append({**t, "items": [i for i in t["items"] if i.get("kind") != "transition"]})
    struct = []
    for kind in ("video", "audio"):
        ne = sum(1 for t in exp_by_kind.get(kind, []) if t.get("clips"))
        na = sum(1 for t in act_by_kind.get(kind, []) if t["items"])
        struct.append((kind, ne, na))
    pairs: list[dict] = []
    for kind in ("video", "audio"):
        etracks = [t for t in exp_by_kind.get(kind, []) if t.get("clips")]
        atracks = [t for t in act_by_kind.get(kind, []) if t["items"]]
        for i, et in enumerate(etracks):
            at = atracks[i] if i < len(atracks) else None
            optional = [c for c in et["clips"] if not c.get("source_path")]
            required = [c for c in et["clips"] if c.get("source_path")]
            items = list(at["items"]) if at else []
            opt_names = {str(c.get("name", "")).lower() for c in optional}
            rest = [it for it in items if not (it["name"] and str(it["name"]).lower() in opt_names and not it.get("media_path"))]
            pairs.append({"label": f"{kind[0].upper()}{i + 1}", "kind": kind, "tindex": at.get("index") if at else None,
                          "optional": optional, "required": required, "items": items, "rest": rest})
    return {"pairs": pairs, "transitions": found_tr, "struct": struct}


def compare_timeline(expected: dict, actual: dict, fold_case: bool | None = None,
                     path_aliases: dict[str, str] | None = None) -> list[dict]:
    """Return list of check results (pass/fail/info) for one imported timeline.

    ``path_aliases`` maps an isolated copy (staged path) to the expected source path it stands for: a clip whose Resolve
    media path is such a copy of its expected source counts as a media_path match."""
    res: list[dict] = []
    # fps
    ef, af = to_number(expected.get("fps")), actual.get("fps")
    if ef is None or af is None:
        res.append(_r("fps", "fail", f"expected {ef}, Resolve reported {af}"))
    else:
        res.append(_r("fps", "pass" if abs(ef - af) < 0.01 else "fail", f"expected {ef:g}, got {af:g}"))
    # resolution
    ew, eh = expected.get("width"), expected.get("height")
    if ew and eh:
        ok = actual.get("width") == ew and actual.get("height") == eh
        res.append(_r("resolution", "pass" if ok else "fail",
                      f"expected {ew}x{eh}, got {actual.get('width')}x{actual.get('height')}"))
    else:
        res.append(_r("resolution", "info", f"no expected resolution; Resolve has {actual.get('width')}x{actual.get('height')}"))
    # duration
    ed, ad = expected.get("duration_frames"), actual.get("duration_frames")
    if ed is None:
        res.append(_r("duration", "info", f"no expected duration; Resolve has {ad}"))
    else:
        res.append(_r("duration", "pass" if _within(ed, ad) else "fail", f"expected {ed}, got {ad} (tolerance +/-{TOL_FRAMES})"))

    # tracks
    aligned = align_tracks(expected, actual)
    found_tr, struct = aligned["transitions"], aligned["struct"]
    ok = all(ne == na for _, ne, na in struct)
    res.append(_r("track_structure", "pass" if ok else "fail",
                  "; ".join(f"{k}: expected {ne} populated track(s), got {na}" for k, ne, na in struct)))

    order_bad, io_bad, src_bad, path_bad, online_bad, info_titles = [], [], [], [], [], []
    seq_bad: list[dict] = []
    via_copy = 0
    n_req = 0
    for pair in aligned["pairs"]:
        label, kind, tindex = pair["label"], pair["kind"], pair["tindex"]
        optional, required, items, rest = pair["optional"], pair["required"], pair["items"], pair["rest"]
        for c in optional:
            found = any(str(it["name"]).lower() == str(c.get("name", "")).lower() for it in items)
            info_titles.append(f"{label} '{c.get('name')}' (no media; titles/graphics): {'present' if found else 'absent'} in Resolve")
        if len(rest) != len(required):
            order_bad.append(f"{label}: expected {len(required)} clip(s), got {len(rest)}")
        for j, c in enumerate(required):
            n_req += 1
            if j >= len(rest):
                order_bad.append(f"{label}[{j}] '{c.get('name')}' missing")
                continue
            it = rest[j]
            seq = match_sequence(c, it)
            if seq:
                seq.update({"track": label, "track_kind": kind, "track_index": tindex, "item_index": it.get("index"),
                            "position": j, "expected_path": c.get("source_path"),
                            "record": [it["start"], it["end"]]})
                seq_bad.append(seq)
            if not seq and not (_item_key(c.get("name"), c.get("source_path")) & _item_key(it["name"], it["media_path"])):
                order_bad.append(f"{label}[{j}] expected '{c.get('name')}', got '{it['name']}'")
            if not (_within(c.get("record_start_frame"), it["start"]) and _within(c.get("record_end_frame"), it["end"])):
                io_bad.append(f"{label}[{j}] '{c.get('name')}' expected {c.get('record_start_frame')}-{c.get('record_end_frame')}, got {it['start']}-{it['end']}")
            ext = os.path.splitext(str(c.get("source_path")))[1].lower()
            if ext in IMAGE_EXTS:
                pass  # stills have no meaningful source in-point
            elif not _within(c.get("source_in_frame"), it["source_in"]):
                src_bad.append(f"{label}[{j}] '{c.get('name')}' expected {c.get('source_in_frame')}, got {it['source_in']}")
            if not it["media_path"]:
                path_bad.append(f"{label}[{j}] '{c.get('name')}' Resolve reports no File Path")
            elif paths_equal(c.get("source_path"), it["media_path"], fold_case=fold_case):
                pass
            elif any(paths_equal(staged, it["media_path"], fold_case=fold_case) and paths_equal(src, c.get("source_path"), fold_case=fold_case)
                     for staged, src in (path_aliases or {}).items()):
                via_copy += 1
            else:
                path_bad.append(f"{label}[{j}] expected {c.get('source_path')}, got {it['media_path']}")
            if not it["online"]:
                online_bad.append(f"{label}[{j}] '{c.get('name')}' offline ({it['media_path'] or 'no media pool item/path'})")
    res.append(_r("clip_order", "fail" if order_bad else "pass", "; ".join(order_bad) or f"{n_req} media clip(s) in expected order"))
    res.append(_r("record_inout", "fail" if io_bad else "pass", "; ".join(io_bad) or f"record in/out within +/-{TOL_FRAMES} frame"))
    res.append(_r("source_in", "fail" if src_bad else "pass", "; ".join(src_bad) or f"source in within +/-{TOL_FRAMES} frame (stills skipped)"))
    res.append(_r("media_path", "fail" if path_bad else "pass", "; ".join(path_bad) or "media paths match"
                  + (f" ({via_copy} via isolated copies in resolve_handoff/stills)" if via_copy else "")))
    res.append(_r("media_online", "fail" if online_bad else "pass", "; ".join(online_bad) or "all media online"))
    sq = _r("image_sequence_misdetected", "fail" if seq_bad else "pass",
            "; ".join(f"{x['track']}[{x['position']}] expected file '{x['expected_file']}' but Resolve read it as image sequence "
                      f"'{x['resolve_name']}' (matched by {x['how']}; path {x['resolve_path']}); the digits in the file name were taken as a frame number"
                      for x in seq_bad) or "no still was turned into an image sequence")
    sq["misdetections"] = seq_bad
    res.append(sq)
    # markers
    em, am = expected.get("markers") or [], actual.get("markers") or []
    if not em and not am:
        res.append(_r("markers", "pass", "no markers expected or found"))
    else:
        miss = [m for m in em if not any(_within(m.get("frame"), a["frame"]) for a in am)]
        res.append(_r("markers", "pass" if not miss and len(em) == len(am) else "fail",
                      f"expected {len(em)}, got {len(am)}" + (f"; missing {[(m.get('name'), m.get('frame')) for m in miss]}" if miss else "")))
    et_ = expected.get("transitions")
    found_tr.sort(key=lambda x: (x["track"], x.get("start") or 0))
    desc = "; ".join(
        f"{x['track']} '{x.get('name')}' {x.get('start'):g}-{x.get('end'):g} centre {x['centre']:g}"
        + (f" (nearest cut {x['nearest_cut']:g}, offset {x['centre_offset']:+g})" if x.get("nearest_cut") is not None else " (no cut found)")
        for x in found_tr)
    if et_ is None:
        tr = _r("transitions", "info", f"no expected count; found {len(found_tr)}" + (f": {desc}" if desc else ""))
    else:
        tr = _r("transitions", "pass" if len(found_tr) == et_ else "fail",
                f"expected {et_}, found {len(found_tr)}" + (f": {desc}" if desc else ""), required=False)
    tr["found"] = [{k: x.get(k) for k in ("track", "name", "start", "end", "centre", "nearest_cut", "centre_offset")} for x in found_tr]
    res.append(tr)
    res.append(_r("titles", "info", "; ".join(info_titles) or "no title/graphic clips expected; check visually"))
    if actual.get("errors"):
        res.append(_r("api_errors", "info", f"{len(actual['errors'])} API call(s) failed/None, first: {actual['errors'][0]}"))
    return res


def format_passed(checks: list[dict]) -> bool:
    return all(c["status"] == "pass" for c in checks if c["required"])

