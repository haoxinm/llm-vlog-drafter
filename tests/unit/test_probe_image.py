from __future__ import annotations

from PIL import Image

from trip_vlog.media.probe import probe_file, probe_image


def make_jpeg(path, size=(40, 20), orientation=None, dto=None, offset=None, gps=None):
    im = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if dto or offset:
        ifd = exif.get_ifd(0x8769)
        if dto:
            ifd[0x9003] = dto
        if offset:
            ifd[0x9011] = offset
    if gps:
        g = exif.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = gps["latref"], gps["lat"], gps["lonref"], gps["lon"]
    im.save(path, "JPEG", exif=exif)
    return path


def test_plain_image(tmp_path):
    r = probe_image(make_jpeg(tmp_path / "a.jpg"))
    assert r.status == "ok" and r.media_type == "image" and (r.width, r.height) == (40, 20)
    assert (r.display_width, r.display_height) == (40, 20) and r.rotation_degrees == 0 and r.capture_time is None


def test_exif_orientation_swaps_display_dims(tmp_path):
    r = probe_image(make_jpeg(tmp_path / "a.jpg", orientation=6))
    assert r.rotation_degrees == 90 and (r.width, r.height, r.display_width, r.display_height) == (40, 20, 20, 40)
    assert probe_image(make_jpeg(tmp_path / "b.jpg", orientation=3)).rotation_degrees == 180
    assert probe_image(make_jpeg(tmp_path / "c.jpg", orientation=8)).rotation_degrees == 270


def test_exif_capture_time_with_and_without_offset(tmp_path):
    r = probe_image(make_jpeg(tmp_path / "a.jpg", dto="2026:10:31 16:22:10", offset="-10:00"))
    assert r.capture_time == "2026-10-31T16:22:10-10:00" and r.capture_time_source == "exif:DateTimeOriginal"
    r = probe_image(make_jpeg(tmp_path / "b.jpg", dto="2026:10:31 16:22:10"))
    assert r.capture_time == "2026-10-31T16:22:10"  # naive: not converted to UTC
    assert any("no timezone" in n for n in r.notes)


def test_exif_gps(tmp_path):
    r = probe_image(make_jpeg(tmp_path / "a.jpg", gps={"latref": "S", "lat": (33.0, 52.0, 7.68), "lonref": "E", "lon": (151.0, 12.0, 33.48)}))
    assert round(r.gps["latitude"], 4) == -33.8688 and round(r.gps["longitude"], 4) == 151.2093


def test_png_and_corrupt_image(tmp_path):
    Image.new("RGBA", (8, 8)).save(tmp_path / "a.png")
    assert probe_image(tmp_path / "a.png").status == "ok"
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0not really a jpeg")
    r = probe_file(bad, "image", None)
    assert r.status == "failed" and r.error


def test_video_without_ffprobe_fails_cleanly(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    r = probe_file(f, "video", None)
    assert r.status == "failed" and "ffprobe not found" in r.error
