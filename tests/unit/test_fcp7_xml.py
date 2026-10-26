"""FCP7 XML parser and the XML <-> timeline round-trip comparer (Phase 5a). Hand-written XML: Windows and macOS paths, URL encoding, NTSC."""
from __future__ import annotations

from fractions import Fraction

import pytest
from xml_helpers import clipitem, to_url, transitionitem, xml_for_spec, xmeml

from trip_vlog.filmcraft.timeline_spec import MarkerSpec, Placement, TimelineSpec, TitleSpec, TransitionIn
from trip_vlog.handoff import xml_roundtrip as X
from trip_vlog.handoff.fcp7_xml import XmlReadError, decode_pathurl, parse_fcp7_xml, parse_rate

WIN = "C:\\Users\\Me\\Videos\\My Clip & More.mp4"
WIN2 = "C:\\Users\\Me\\Pictures\\IMG_1042.JPG"
MUSIC = "C:\\Users\\Me\\Music\\song.wav"
MAC = "/Users/me/Movies/trip 1/clip #1.mov"


# ---- parser ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("file://localhost/C%3a/Users/Me/Videos/My%20Clip%20%26%20More.mp4", "C:/Users/Me/Videos/My Clip & More.mp4"),
    ("file:///C:/Users/Me/a%20b.mp4", "C:/Users/Me/a b.mp4"),
    ("file://localhost/Users/me/Movies/trip%201/clip%20%231.mov", "/Users/me/Movies/trip 1/clip #1.mov"),
    ("file:///Users/me/Movies/a.mov", "/Users/me/Movies/a.mov"),
    ("file://server/share/clips/a%20b.mov", "//server/share/clips/a b.mov"),
    ("C:\\Users\\Me\\a.mp4", "C:\\Users\\Me\\a.mp4"),
    ("", None),
])
def test_decode_pathurl_windows_macos_and_unc(url, expected):
    assert decode_pathurl(url) == expected


def test_to_url_helper_matches_what_the_parser_reads():
    for p in (WIN, MAC):
        assert decode_pathurl(to_url(p)) == p.replace("\\", "/")


def test_rates_ntsc_and_integer():
    assert parse_rate(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring("<a><rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate></a>")) == Fraction(30000, 1001)
    assert parse_rate(__import__("xml.etree.ElementTree", fromlist=["x"]).fromstring("<a><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate></a>")) == 25
    with pytest.raises(Exception):
        parse_rate(None)


def test_parse_clips_titles_gain_transitions_markers():
    text = xmeml(duration=300, tb=30, ntsc=True, width=3840, height=2160,
                 video=[[clipitem(1, "a.mp4", 0, 150, 15, 165, path=WIN, file_id="f1", tb=30, ntsc=True),
                         transitionitem(142, 158, tb=30, ntsc=True),
                         clipitem(2, "b.mov", 150, 300, 0, 150, path=MAC, file_id="f2", tb=30, ntsc=True)],
                        [clipitem(3, "Hello", 0, 90, 0, 90, text="Hello", tb=30, ntsc=True)]],
                 audio=[[clipitem(4, "a.mp4", 0, 150, 15, 165, path=WIN, file_id="f1", full_file=False, gain=-12.0, tb=30, ntsc=True)]],
                 markers=[("Scene 1", "scene_001", 0)])
    tl = parse_fcp7_xml(text)
    assert (tl.fps, tl.duration, tl.width, tl.height, tl.name) == (Fraction(30000, 1001), 300, 3840, 2160, "Main Draft")
    v1, v2, a1 = tl.track_clips("video", 1), tl.track_clips("video", 2), tl.track_clips("audio", 1)
    assert [c.path for c in v1] == ["C:/Users/Me/Videos/My Clip & More.mp4", "/Users/me/Movies/trip 1/clip #1.mov"]
    assert (v1[0].start, v1[0].end, v1[0].source_in, v1[0].source_out, v1[0].rate) == (0, 150, 15, 165, Fraction(30000, 1001))
    assert v2[0].is_generator and v2[0].text == "Hello" and v2[0].path is None
    assert a1[0].path == "C:/Users/Me/Videos/My Clip & More.mp4"        # <file id="f1"/> resolved through the id
    assert a1[0].gain_db == pytest.approx(-12.0, abs=1e-6)
    assert [(t.track_name, t.start, t.end, t.centre, t.frames) for t in tl.transitions] == [("V1", 142, 158, 150.0, 16)]
    assert [(m.name, m.comment, m.frame) for m in tl.markers] == [("Scene 1", "scene_001", 0)]
    assert tl.counts() == {"video_clipitems": 2, "title_items": 1, "audio_clipitems": 1, "transitions": 1, "markers": 1, "gain_filters": 1, "speed_items": 0}


def test_minus_one_means_unspecified():
    tl = parse_fcp7_xml(xmeml(duration=60, video=[[clipitem(1, "a.mp4", -1, 60, 0, 60, path=MAC, file_id="f")]]))
    assert tl.clips[0].start is None and tl.clips[0].end == 60


def test_parse_errors():
    with pytest.raises(XmlReadError):
        parse_fcp7_xml("<xmeml><sequence>")
    with pytest.raises(XmlReadError):
        parse_fcp7_xml("<fcpxml version='1.9'/>")
    with pytest.raises(XmlReadError):
        parse_fcp7_xml("<xmeml version='4'/>")


# ---- round trip -----------------------------------------------------------------------------------------------------

def spec(fps=Fraction(30), path=WIN, still=WIN2):
    return TimelineSpec(
        "Main Draft", 1920, 1080, fps, ("V1", "V2", "A1", "A2"),
        placements=(
            Placement("clip_001", path, "video", "V1", 0, 105, 0.5, 4.0, True),
            Placement("clip_002", still, "image", "V1", 105, 90, 0.0, None, False, None, TransitionIn("dissolve", 15)),
            Placement("clip_003", MAC, "video", "V2", 60, 60, 1.0, 3.0, False),
            Placement("clip_004", MUSIC, "audio", "A2", 0, 180, 0.0, 6.0, False, -6.0),
            Placement("clip_005", MAC, "video", "V1", 195, 30, 0.0, 1.0, True, -12.0)),
        markers=(MarkerSpec(60, "Scene A", "scene_001"),), titles=(TitleSpec("Hello Title", 0, 60, "V2"),))


def rt(xml, sp=None, **kw):
    return X.compare_timeline_xml(parse_fcp7_xml(xml), X.expected_from_spec(sp or spec()), **kw)


def test_perfect_xml_matches_everything_windows_and_macos_styles():
    sp = spec()
    for style in ("localhost", "bare"):
        r = rt(xml_for_spec(sp, url_style=style), sp)
        assert r.ok, [str(m) for m in r.mismatches]
        assert r.expected["cuts"] == r.matched["cuts"] == 4 and r.matched["stills"] == r.matched["overlays_v2"] == r.matched["titles"] == 1
        assert r.matched["dissolves"] == 1 and r.matched["markers"] == 1 and r.matched["music_track"] == 1 and r.matched["audio_gain"] == 2
        assert r.expected["cuts_boundaries"] == r.matched["cuts_boundaries"] == 1   # clip_001->(dissolve)->still is not a hard cut; still->clip_005 is


def test_ntsc_sequence_and_exact_frames():
    sp = spec(Fraction(30000, 1001))
    xml = xml_for_spec(sp)
    assert "<ntsc>TRUE</ntsc>" in xml and rt(xml, sp).ok
    bad = rt(xml, spec(Fraction(30)))
    assert [m.kind for m in bad.for_feature("timeline_settings")] == ["settings"] and "frame rate" in bad.mismatches[0].detail


def test_windows_path_case_and_separators():
    sp = spec(path="c:\\users\\me\\videos\\my clip & more.mp4")      # same file, different case, as the media manifest may spell it
    xml = xml_for_spec(spec())
    assert rt(xml, sp, fold_case=True).ok
    r = rt(xml, sp, fold_case=False)
    assert not r.ok and {m.kind for m in r.mismatches} >= {"missing", "unexpected"}
    assert rt(xml_for_spec(sp), sp, fold_case=False).ok                # exactly equal text is fine on a case-sensitive file system too


def test_record_and_source_frames_are_compared_exactly():
    def shift(doc):
        c = next(i for i in doc["items"] if i["name"] == "My Clip & More.mp4" and i["kind"] == "video")
        c["end"] += 1
        c["out"] -= 1
    r = rt(xml_for_spec(spec(), mutate=shift))
    kinds = sorted((m.feature, m.kind) for m in r.mismatches)
    assert ("cuts", "record") in kinds and ("cuts", "source") in kinds
    assert rt(xml_for_spec(spec(), mutate=shift), tolerance_frames=1).ok      # the tolerance is opt-in and applies to these fields
    assert not rt(xml_for_spec(spec(), mutate=shift), tolerance_frames=0).ok


def test_off_by_one_sequence_duration_is_caught_at_tolerance_zero():
    for delta in (1, -1):
        r = rt(xml_for_spec(spec(), mutate=lambda d, delta=delta: d.update(duration=d["duration"] + delta)))
        assert [m.kind for m in r.for_feature("timeline_settings")] == ["settings"] and "duration" in r.mismatches[0].detail
        assert rt(xml_for_spec(spec(), mutate=lambda d, delta=delta: d.update(duration=d["duration"] + delta)), tolerance_frames=1).ok


def test_transition_span_is_exact_and_off_by_one_is_caught():
    def moved(ds, de):
        def f(doc):
            doc["transitions"][0]["start"] += ds
            doc["transitions"][0]["end"] += de
        return f

    assert rt(xml_for_spec(spec(), mutate=moved(0, 0))).ok
    # the 15-frame dissolve on the cut at 105 is written 97..112 by FilmCraft (the odd frame goes before the cut)
    assert X.centred_transition_span(105, 15) == (97, 112) and X.centred_transition_span(105, 16) == (97, 113)
    assert X.centred_transition_span(105, 10) == (100, 110) and X.centred_transition_span(105, 3) == (103, 106) and X.centred_transition_span(105, 2) == (104, 106)
    for ds, de, what in ((1, 1, "centre shifted by one frame"), (-1, -1, "centre shifted by one frame"), (0, 1, "one frame longer"), (0, -1, "one frame shorter"),
                         (1, 0, "one frame shorter"), (-1, 0, "one frame longer")):
        r = rt(xml_for_spec(spec(), mutate=moved(ds, de)))
        assert [m.kind for m in r.for_feature("dissolves")] == ["transition", "transition"], what    # the planned one is missing, this one is unplanned
        assert rt(xml_for_spec(spec(), mutate=moved(ds, de)), tolerance_frames=1).ok, what


def test_hard_cut_bookkeeping_is_exact():
    r = rt(xml_for_spec(spec()))
    assert r.expected["cuts_boundaries"] == r.matched["cuts_boundaries"] == 1

    def gap(doc):                # clip_005 starts one frame late: the cut is no longer contiguous
        c = next(i for i in doc["items"] if i["start"] == 195 and i["kind"] == "video")
        c["start"] += 1
        c["end"] += 1
    bad = rt(xml_for_spec(spec(), mutate=gap))
    assert bad.matched["cuts_boundaries"] == 0 and any(m.kind == "record" for m in bad.mismatches)


def test_audio_of_a_muted_clip_one_frame_off_is_not_waved_through_as_muted_bookkeeping():
    def add(shift):
        def f(doc):
            doc["items"].append({"i": 98, "kind": "audio", "track": 1, "name": "clip #1.mov", "start": 60 + shift, "end": 120 + shift, "in_": 30, "out": 90,
                                 "path": MAC, "text": None, "gain": None})
        return f
    assert [m.feature for m in rt(xml_for_spec(spec(), mutate=add(0))).mismatches] == ["audio_mute"]
    assert [m.feature for m in rt(xml_for_spec(spec(), mutate=add(1))).mismatches] == ["audio_clips"]


@pytest.mark.parametrize("fps,secs,tb", [(Fraction(30), 0.25, 30), (Fraction(25), 0.3, 25)])
def test_exact_half_frame_source_points_accept_either_rounding_and_nothing_else(fps, secs, tb):
    sp = TimelineSpec("S", 1920, 1080, fps, ("V1", "V2", "A1", "A2"),
                      placements=(Placement("c1", MAC, "video", "V1", 0, 60, secs, secs + 2.0, False),))
    exact = Fraction(secs).limit_denominator(10 ** 6) * fps
    assert exact.denominator == 2
    lo, hi = int(exact - Fraction(1, 2)), int(exact + Fraction(1, 2))
    n = int(2 * fps)

    def xml(i):
        return xmeml(duration=60, tb=tb, video=[[clipitem(1, "clip #1.mov", 0, 60, i, i + n, path=MAC, file_id="f", tb=tb)]], audio=[[]])

    assert (lo, hi) == (int(exact), int(exact) + 1)
    assert rt(xml(lo), sp).ok and rt(xml(hi), sp).ok                          # both roundings of an exact half pass
    assert [m.kind for m in rt(xml(lo - 1), sp).mismatches] == ["source"]     # one frame further does not
    assert [m.kind for m in rt(xml(hi + 1), sp).mismatches] == ["source"]
    # a non-half position still has only one acceptable frame
    sp2 = TimelineSpec("S", 1920, 1080, fps, ("V1", "V2", "A1", "A2"),
                       placements=(Placement("c1", MAC, "video", "V1", 0, 60, 1.0, 3.0, False),))
    k = int(fps)
    ok = xmeml(duration=60, tb=tb, video=[[clipitem(1, "clip #1.mov", 0, 60, k, k + n, path=MAC, file_id="f", tb=tb)]], audio=[[]])
    off = xmeml(duration=60, tb=tb, video=[[clipitem(1, "clip #1.mov", 0, 60, k + 1, k + 1 + n, path=MAC, file_id="f", tb=tb)]], audio=[[]])
    assert rt(ok, sp2).ok and not rt(off, sp2).ok


def test_missing_and_unexpected_items_and_wrong_track():
    def drop_still(doc):
        doc["items"] = [i for i in doc["items"] if i["name"] != "IMG_1042.JPG"]

    def extra(doc):
        doc["items"].append({"i": 99, "kind": "video", "track": 3, "name": "x.mp4", "start": 0, "end": 10, "in_": 0, "out": 10, "path": "C:\\other\\x.mp4",
                             "text": None, "gain": None})

    def wrong_track(doc):
        next(i for i in doc["items"] if i["text"])["track"] = 3

    assert [m.kind for m in rt(xml_for_spec(spec(), mutate=drop_still)).for_feature("stills")] == ["missing"]
    unexpected = [m for m in rt(xml_for_spec(spec(), mutate=extra)).mismatches if m.kind == "unexpected"]
    assert len(unexpected) == 1 and unexpected[0].feature == "overlays_v2" and "x.mp4" in unexpected[0].ref
    kinds = [(m.feature, m.kind) for m in rt(xml_for_spec(spec(), mutate=wrong_track)).mismatches]
    assert ("titles", "missing") in kinds and ("titles", "unexpected") in kinds


def test_gain_markers_transitions_and_titles():
    def regain(doc):
        next(i for i in doc["items"] if i["kind"] == "audio" and i["gain"] == -6.0)["gain"] = None

    def no_marker(doc):
        doc["markers"] = []

    def no_transition(doc):
        doc["transitions"] = []

    def late_marker(doc):
        doc["markers"] = [("Scene A", "scene_001", 61)]

    assert [m.kind for m in rt(xml_for_spec(spec(), mutate=regain)).for_feature("audio_gain")] == ["gain"]
    assert [m.kind for m in rt(xml_for_spec(spec(), mutate=no_marker)).for_feature("markers")] == ["marker"]
    assert [m.kind for m in rt(xml_for_spec(spec(), mutate=no_transition)).for_feature("dissolves")] == ["transition"]
    kinds = [m.kind for m in rt(xml_for_spec(spec(), mutate=late_marker)).for_feature("markers")]
    assert sorted(kinds) == ["marker", "marker"]                       # not at frame 60, and one at 61 nobody planned
    assert rt(xml_for_spec(spec(), mutate=late_marker), tolerance_frames=1).ok


def test_linked_audio_missing_and_audio_that_should_have_been_muted():
    def drop_audio(doc):
        doc["items"] = [i for i in doc["items"] if not (i["kind"] == "audio" and i["gain"] is None and i["track"] == 1 and i["start"] == 0)]

    def add_audio_for_muted(doc):   # clip_003 on V2 is placed without audio
        doc["items"].append({"i": 98, "kind": "audio", "track": 1, "name": "clip #1.mov", "start": 60, "end": 120, "in_": 30, "out": 90, "path": MAC,
                             "text": None, "gain": None})

    assert any(m.kind == "missing" and m.feature == "cuts" for m in rt(xml_for_spec(spec(), mutate=drop_audio)).mismatches)
    extra = rt(xml_for_spec(spec(), mutate=add_audio_for_muted)).mismatches
    assert [m.feature for m in extra] == ["audio_mute"] and extra[0].kind == "unexpected"


def test_speed_filters_are_flagged():
    def speed(doc):
        next(i for i in doc["items"] if i["kind"] == "video" and i["track"] == 1 and i["start"] == 0)["speed"] = True
    r = rt(xml_for_spec(spec(), mutate=speed))
    assert [m.feature for m in r.mismatches] == ["speed_changes"]


def test_sequence_duration_and_size():
    def dur(doc):
        doc["duration"] += 5
        doc["width"] = 1280
    r = rt(xml_for_spec(spec(), mutate=dur))
    d = r.for_feature("timeline_settings")[0].detail
    assert "size 1280x1080" in d and "duration" in d


def test_clip_item_rate_is_converted_to_sequence_frames():
    """A clip item carrying its own rate (25) in a 30 fps sequence: in/out are compared in sequence frames."""
    sp = TimelineSpec("S", 1920, 1080, Fraction(30), ("V1", "V2", "A1", "A2"),
                      placements=(Placement("c1", MAC, "video", "V1", 0, 60, 1.0, 3.0, False),))
    text = xmeml(duration=60, video=[[clipitem(1, "clip #1.mov", 0, 60, 25, 75, path=MAC, file_id="f", tb=25)]], audio=[[]])
    assert rt(text, sp).ok
    wrong = xmeml(duration=60, video=[[clipitem(1, "clip #1.mov", 0, 60, 30, 90, path=MAC, file_id="f", tb=25)]], audio=[[]])
    assert [m.kind for m in rt(wrong, sp).mismatches] == ["source"]


def test_clips_report_rows_for_every_expected_item():
    r = rt(xml_for_spec(spec()))
    rows = {(c["clip_instance_id"], c["track"]): c for c in r.clips}
    assert rows[("clip_001", "V1")]["record"] == [0, 105] == rows[("clip_001", "V1")]["xml_record"] and rows[("clip_001", "A1")]["ok"] is True
    assert rows[("clip_001", "V1")]["source"] == [15, 120] == rows[("clip_001", "V1")]["xml_source"]
    assert r.to_dict()["ok"] is True
