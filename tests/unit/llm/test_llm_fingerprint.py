from __future__ import annotations

import hashlib
import shutil

from llm_helpers import img, ScriptedProvider, make_image, make_request, presp
from PIL import Image

from trip_vlog.llm import Gateway
from trip_vlog.llm.gateway import fingerprint_of, request_summary
from trip_vlog.llm.images import IMAGE_PREP_VERSION, prepare_image
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
    assert s["parts"][0] == {"type": "image", "source_sha256": hashlib.sha256(img.read_bytes()).hexdigest(), "max_side": 1568,
                             "quality": 85, "prep_version": IMAGE_PREP_VERSION}
    assert set(s) == {"fingerprint_version", "template_id", "template_version", "system", "schema", "parts", "model", "max_tokens",
                      "effort", "provider"}
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


def test_fingerprint_hashes_source_bytes_not_reencoded_bytes(img, monkeypatch):
    base = fp(make_request([img]))
    sent = prepare_image(ImagePart(img)).data
    real_save = Image.Image.save

    def other_encoder(self, target, format=None, **kw):  # simulate a different JPEG encoder build on another OS
        return real_save(self, target, format=format, optimize=True, progressive=True, **kw)

    monkeypatch.setattr(Image.Image, "save", other_encoder)
    assert prepare_image(ImagePart(img)).data != sent  # the re-encoded bytes really differ
    assert fp(make_request([img])) == base


def test_provider_settings_are_fingerprinted_but_not_endpoint_location(img):
    def f(extra):
        p = ScriptedProvider()
        p.fingerprint_extra = extra
        return Gateway(p).fingerprint(make_request([img]))

    base = f({"api": "openai_chat", "params": {"temperature": 0}})
    assert f({"api": "openai_chat", "params": {"temperature": 0}}) == base
    assert f({"api": "openai_chat", "params": {"temperature": 1}}) != base
    assert f({"api": "anthropic_messages", "params": {"temperature": 0}}) != base


def test_fingerprint_depends_on_prep_parameters(img):
    assert fp(make_request([img]), image_max_side=700) != fp(make_request([img]))
    assert fp(make_request([img]), image_quality=70) != fp(make_request([img]))


def test_image_is_resized_and_sent_as_jpeg(tmp_path):
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


def test_explicit_identity_replaces_the_byte_hash(tmp_path):
    a = make_image(tmp_path / "a.jpg", color=(10, 10, 10))
    b = make_image(tmp_path / "b.jpg", color=(200, 20, 20))   # different bytes, e.g. another ffmpeg build
    assert fp(make_request([a])) != fp(make_request([b]))

    def with_identity(path, ident):
        req = make_request([path])
        return type(req)(**{**req.__dict__, "parts": tuple(ImagePart(p.path, identity=ident) if isinstance(p, ImagePart) else p for p in req.parts)})

    assert fp(with_identity(a, "cv1:frame:7")) == fp(with_identity(b, "cv1:frame:7"))
    assert fp(with_identity(a, "cv1:frame:7")) != fp(with_identity(a, "cv1:frame:8"))
    assert fp(with_identity(a, "cv1:frame:7")) != fp(make_request([a]))


def test_sampled_frames_carry_a_stable_identity(tmp_path):
    from analysis_helpers import fake_frames  # noqa: F401  (documents that frames are fake files in these tests)
    from trip_vlog.analysis.vision import _identity
    from types import SimpleNamespace

    st = SimpleNamespace(sampling=__import__("trip_vlog.analysis.frames", fromlist=["x"]).SamplingSettings())
    it = SimpleNamespace(asset={"content_version": "cv1"}, sid="s")
    assert _identity(st, it, "frame", 3) == _identity(st, it, "frame", 3)
    assert _identity(st, it, "frame", 3) != _identity(st, it, "frame", 4)
    it2 = SimpleNamespace(asset={"content_version": "cv2"}, sid="s")
    assert _identity(st, it, "frame", 3) != _identity(st, it2, "frame", 3)
