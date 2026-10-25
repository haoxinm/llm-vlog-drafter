from __future__ import annotations

import shutil

from llm_helpers import img, ScriptedProvider, make_image, make_request, presp
from PIL import Image

from trip_vlog.llm import Gateway
from trip_vlog.llm.gateway import fingerprint_of, request_summary
from trip_vlog.llm.images import prepare_image
from trip_vlog.llm.types import ImagePart


def fp(req, model="test-model", **kw):
    return Gateway(ScriptedProvider(model=model), **kw).fingerprint(req)


def test_same_inputs_different_absolute_paths_same_fingerprint(tmp_path):
    (tmp_path / "one").mkdir()
    a = make_image(tmp_path / "one" / "IMG_1.jpg")
    (tmp_path / "two" / "deep").mkdir(parents=True)
    b = tmp_path / "two" / "deep" / "other_name.jpg"
    shutil.copyfile(a, b)
    assert fp(make_request([a])) == fp(make_request([b]))


def test_summary_has_no_paths_or_bytes(img):
    gw = Gateway(ScriptedProvider())
    prepared = [prepare_image(ImagePart(img))]
    s = request_summary(make_request([img]), prepared + [], "test-model")
    text = str(s)
    assert str(img.parent) not in text and img.name not in text
    assert s["parts"][0] == {"type": "image", "media_type": "image/jpeg", "sha256": prepared[0].sha256}
    assert set(s) == {"fingerprint_version", "template_id", "template_version", "system", "schema", "parts", "model", "max_tokens", "effort"}
    assert len(fingerprint_of(s)) == 64 and gw.model == "test-model"


def test_changes_alter_fingerprint(tmp_path, img):
    base = fp(make_request([img]))
    assert fp(make_request([img])) == base
    other = make_image(tmp_path / "other.jpg", color=(1, 2, 3))
    assert fp(make_request([other])) != base                     # image bytes
    assert fp(make_request([img], version="2")) != base          # template version
    assert fp(make_request([img]), model="test-model-2") != base # model
    assert fp(make_request([img], text="other")) != base         # text part
    assert fp(make_request([img], system="other")) != base       # system text
    assert fp(make_request([img], max_tokens=501)) != base
    assert fp(make_request([img], effort="high")) != base
    assert fp(make_request([img], schema={"type": "object"})) != base
    assert fp(make_request([img, img])) != base                  # part list


def test_dict_key_order_and_newline_style_do_not_matter(img):
    s1 = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    s2 = {"properties": {"b": {"type": "string"}, "a": {"type": "string"}}, "type": "object"}
    assert fp(make_request([img], schema=s1)) == fp(make_request([img], schema=s2))
    assert fp(make_request([img], text="a\r\nb")) == fp(make_request([img], text="a\nb"))


def test_fingerprint_uses_bytes_actually_sent(tmp_path):
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(big)
    gw = Gateway(ScriptedProvider(), image_max_side=800)
    prepared = prepare_image(ImagePart(big, "image/png"), max_side=800)
    with Image.open(__import__("io").BytesIO(prepared.data)) as im:
        assert max(im.size) == 800 and im.format == "JPEG"
    assert gw.fingerprint(make_request([big])) != Gateway(ScriptedProvider(), image_max_side=1200).fingerprint(make_request([big]))


def test_image_prep_strips_exif_and_caches(tmp_path):
    src = tmp_path / "gps.jpg"
    im = Image.new("RGB", (100, 50), (5, 5, 5))
    exif = Image.Exif()
    exif[0x010F] = "SecretMaker"  # Make
    im.save(src, exif=exif)
    assert b"SecretMaker" in src.read_bytes()
    cache = tmp_path / "imgcache"
    out = prepare_image(ImagePart(src), cache_dir=cache)
    assert b"SecretMaker" not in out.data and b"Exif" not in out.data
    assert len(list(cache.glob("*.jpg"))) == 1
    assert prepare_image(ImagePart(src), cache_dir=cache) == out


def test_unreadable_or_bad_image_is_typed_error(tmp_path):
    import pytest

    from trip_vlog.llm.types import LLMInputError

    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(LLMInputError):
        prepare_image(ImagePart(bad))
    with pytest.raises(LLMInputError):
        prepare_image(ImagePart(tmp_path / "missing.jpg"))
    with pytest.raises(LLMInputError):
        ImagePart(bad, "application/pdf")
