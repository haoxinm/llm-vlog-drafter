from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from trip_vlog.media.scan import find_case_collisions, media_type_for_ext, scan_media


def touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def paths(res):
    return [f.rel_path for f in res.files]


def reasons(res):
    return {s.rel_path: s.reason for s in res.skipped}


def test_extensions_case_insensitive_and_types():
    assert media_type_for_ext(".MOV") == "video" and media_type_for_ext(".jpg") == "image"
    assert media_type_for_ext(".WAV") == "audio" and media_type_for_ext(".txt") is None


def test_recursive_enumeration_and_unsupported_report(tmp_path):
    touch(tmp_path / "a.MP4"); touch(tmp_path / "Day 2" / "b.JpG"); touch(tmp_path / "Day 2" / "deep" / "c.wav")
    touch(tmp_path / "notes.txt"); touch(tmp_path / "noext")
    res = scan_media(tmp_path)
    assert paths(res) == ["Day 2/b.JpG", "Day 2/deep/c.wav", "a.MP4"]
    assert {f.rel_path: f.media_type_hint for f in res.files}["a.MP4"] == "video"
    assert reasons(res) == {"notes.txt": "unsupported_extension", "noext": "unsupported_extension"}
    assert res.skipped_counts() == {"unsupported_extension": 2}


def test_non_recursive(tmp_path):
    touch(tmp_path / "a.mp4"); touch(tmp_path / "sub" / "b.mp4")
    res = scan_media(tmp_path, recursive=False)
    assert paths(res) == ["a.mp4"] and reasons(res) == {"sub": "not_recursive"}


def test_hidden_and_system_files_skipped(tmp_path):
    touch(tmp_path / "keep.mp4")
    for name in (".hidden.mp4", ".DS_Store", "._keep.mp4", "Thumbs.db", "desktop.ini", "THUMBS.DB"):
        touch(tmp_path / name)
    touch(tmp_path / "__MACOSX" / "x.mp4"); touch(tmp_path / ".Trashes" / "y.jpg"); touch(tmp_path / ".git" / "z.png")
    touch(tmp_path / "sub" / "._IMG_1.JPG"); touch(tmp_path / "sub" / "real.JPG")
    res = scan_media(tmp_path)
    assert paths(res) == ["keep.mp4", "sub/real.JPG"]
    r = reasons(res)
    assert r["._keep.mp4"] == "hidden" and r[".DS_Store"] == "system_file"
    assert r["Thumbs.db"] == "system_file" and r["desktop.ini"] == "system_file" and r["__MACOSX"] == "system_file"
    assert r["sub/._IMG_1.JPG"] == "hidden" and r[".git"] == "hidden"
    assert "__MACOSX/x.mp4" not in r  # the whole directory is skipped, not walked


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinks_not_followed_by_default(tmp_path):
    media, other = tmp_path / "media", tmp_path / "other"
    touch(media / "real.mp4"); touch(other / "outside.mp4")
    (media / "link.mp4").symlink_to(media / "real.mp4")
    (media / "linkdir").symlink_to(other, target_is_directory=True)
    res = scan_media(media)
    assert paths(res) == ["real.mp4"]
    assert reasons(res) == {"link.mp4": "symlink", "linkdir": "symlink"}
    res2 = scan_media(media, follow_symlinks=True)
    assert paths(res2) == ["link.mp4", "linkdir/outside.mp4", "real.mp4"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_loop_guard(tmp_path):
    touch(tmp_path / "a.mp4")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    res = scan_media(tmp_path, follow_symlinks=True)
    assert paths(res) == ["a.mp4"] and "symlink_loop" in res.skipped_counts()


def test_find_case_collisions():
    assert find_case_collisions(["a/IMG.jpg", "a/img.JPG", "b/x.jpg", "B/x.jpg", "c.jpg"]) == [
        ["B/x.jpg", "b/x.jpg"], ["a/IMG.jpg", "a/img.JPG"]]
    assert find_case_collisions(["a.jpg", "b.jpg"]) == []


def test_case_collision_reported_by_scan(tmp_path):
    touch(tmp_path / "IMG_1.jpg")
    try:
        touch(tmp_path / "img_1.JPG", b"yy")
    except OSError:
        pass
    if len(list(tmp_path.iterdir())) < 2:
        pytest.skip("case-insensitive filesystem cannot hold both files")
    res = scan_media(tmp_path)
    assert res.case_collisions == [["IMG_1.jpg", "img_1.JPG"]]
    assert len({f.asset_id for f in res.files}) == 2  # both indexed, IDs stay distinct (case is never folded)


def test_unicode_nfd_names_become_nfc_and_collide(tmp_path):
    nfc_name = unicodedata.normalize("NFC", "caf\u00e9.jpg")
    nfd_name = unicodedata.normalize("NFD", "caf\u00e9.jpg")
    touch(tmp_path / nfd_name)
    res = scan_media(tmp_path)
    assert paths(res) == [nfc_name]
    touch(tmp_path / nfc_name, b"other")
    if len(list(tmp_path.iterdir())) < 2:
        pytest.skip("filesystem normalises names")
    res = scan_media(tmp_path)
    assert len(res.files) == 1 and "unicode_collision" in res.skipped_counts()


def test_scan_fingerprints_and_never_modifies(tmp_path):
    p = touch(tmp_path / "a.mp4", b"abc" * 100)
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    res = scan_media(tmp_path)
    f = res.files[0]
    assert f.content_version.startswith("fast:") and f.size_bytes == 300 and f.error is None
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before
    assert scan_media(tmp_path, fingerprint="sha256").files[0].content_version.startswith("sha256:")


def test_fingerprint_invalidation_on_change_keeps_asset_id(tmp_path):
    p = touch(tmp_path / "a.mp4", b"abc" * 100)
    f1 = scan_media(tmp_path).files[0]
    p.write_bytes(b"abd" * 100)
    f2 = scan_media(tmp_path).files[0]
    assert f1.asset_id == f2.asset_id and f1.content_version != f2.content_version


def test_touching_mtime_changes_fast_version_but_not_sha256(tmp_path):
    p = touch(tmp_path / "a.mp4", b"abc" * 100)
    a = scan_media(tmp_path).files[0], scan_media(tmp_path, fingerprint="sha256").files[0]
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    b = scan_media(tmp_path).files[0], scan_media(tmp_path, fingerprint="sha256").files[0]
    assert a[0].content_version != b[0].content_version
    assert a[1].content_version == b[1].content_version


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions and non-root")
def test_unreadable_file_reported_not_raised(tmp_path):
    p = touch(tmp_path / "locked.mp4")
    touch(tmp_path / "ok.mp4")
    p.chmod(0)
    try:
        res = scan_media(tmp_path)
    finally:
        p.chmod(0o644)
    assert {f.rel_path: f.error is not None for f in res.files} == {"locked.mp4": True, "ok.mp4": False}
