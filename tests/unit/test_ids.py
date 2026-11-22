from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from trip_vlog.media.ids import FAST_CHUNK, asset_id_for, compute_content_version, fast_fingerprint, fingerprint_algorithm
from trip_vlog.paths import long_path, normalize_rel_path, safe_filename


def test_asset_id_format_and_formula():
    aid = asset_id_for("day-02/IMG_1042.MOV")
    assert aid == "asset_" + hashlib.sha256(b"day-02/IMG_1042.MOV").hexdigest()[:16]
    assert len(aid) == len("asset_") + 16


def test_asset_id_stable_and_path_based():
    assert asset_id_for("a/IMG_1.MOV") == asset_id_for("a/IMG_1.MOV")
    assert asset_id_for("a/IMG_1.MOV") != asset_id_for("b/IMG_1.MOV")  # not the bare filename
    assert asset_id_for("a/img_1.mov") != asset_id_for("a/IMG_1.MOV")  # case is never folded


def test_windows_and_posix_separators_give_same_id():
    win = normalize_rel_path(PureWindowsPath("day-02\\IMG_1042.MOV"))
    posix = normalize_rel_path(PurePosixPath("day-02/IMG_1042.MOV"))
    assert win == posix == "day-02/IMG_1042.MOV"
    assert asset_id_for(win) == asset_id_for(posix)


def test_nfc_normalisation_makes_nfd_and_nfc_equal():
    nfc_name = unicodedata.normalize("NFC", "caf\u00e9/photo.jpg")
    nfd_name = unicodedata.normalize("NFD", "caf\u00e9/photo.jpg")
    assert nfc_name != nfd_name
    a = normalize_rel_path(PurePosixPath(nfd_name))
    b = normalize_rel_path(PureWindowsPath(nfc_name.replace("/", "\\")))
    assert a == b == nfc_name
    assert asset_id_for(a) == asset_id_for(b)


@pytest.mark.parametrize("bad", [PureWindowsPath("C:\\x\\y.mp4"), PureWindowsPath("\\\\srv\\share\\y.mp4"),
                                 PurePosixPath("/abs/y.mp4"), PurePosixPath("../y.mp4"), PurePosixPath(".")])
def test_normalize_rejects_absolute_and_escaping(bad):
    with pytest.raises(ValueError):
        normalize_rel_path(bad)


def test_fast_fingerprint_changes_with_content_size_and_mtime(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello world" * 100)
    st = p.stat()
    base = fast_fingerprint(p, st.st_size, st.st_mtime_ns)
    assert base == fast_fingerprint(p, st.st_size, st.st_mtime_ns)
    assert base != fast_fingerprint(p, st.st_size, st.st_mtime_ns + 1)
    p.write_bytes(b"hello worle" + b"hello world" * 99)
    assert base != fast_fingerprint(p, p.stat().st_size, st.st_mtime_ns)


def test_fast_fingerprint_sees_head_and_tail_but_not_middle(tmp_path):
    size = FAST_CHUNK * 3
    data = bytearray(os.urandom(1024)) * (size // 1024)
    p = tmp_path / "big.bin"
    p.write_bytes(bytes(data))
    mt = 1_700_000_000_000_000_000
    base = fast_fingerprint(p, size, mt)
    for pos, expect_change in ((0, True), (size - 1, True), (size // 2, False)):
        mod = bytearray(data)
        mod[pos] ^= 0xFF
        p.write_bytes(bytes(mod))
        assert (fast_fingerprint(p, size, mt) != base) is expect_change, pos


def test_content_version_modes(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"abc")
    st = p.stat()
    cv, sha = compute_content_version(p, "sha256", st.st_size, st.st_mtime_ns)
    assert cv == "sha256:" + hashlib.sha256(b"abc").hexdigest() and sha == hashlib.sha256(b"abc").hexdigest()
    cv2, sha2 = compute_content_version(p, "fast", st.st_size, st.st_mtime_ns)
    assert cv2.startswith("fast:") and sha2 is None
    assert fingerprint_algorithm(cv) == "sha256" and fingerprint_algorithm(cv2) == "fast"
    with pytest.raises(ValueError):
        compute_content_version(p, "md5", 0, 0)


def test_empty_file_fingerprint(tmp_path):
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    assert fast_fingerprint(p, 0, 0)


def test_safe_filename():
    assert safe_filename('a<b>:c"d|e?f*') == "a_b__c_d_e_f_"
    assert safe_filename("CON") == "_CON"
    assert safe_filename("nul.txt") == "_nul.txt"
    assert safe_filename("name. ") == "name"
    assert safe_filename("") == "_"
    assert len(safe_filename("x" * 200)) <= 40


def test_long_path_is_noop_off_windows(tmp_path):
    if os.name != "nt":
        assert long_path(tmp_path) == tmp_path
