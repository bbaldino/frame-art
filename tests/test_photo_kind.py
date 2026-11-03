import io

import pytest
from PIL import Image

from frame_art import openrouter, photo_kind
from frame_art.photos import PhotoStore


def jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return buf.getvalue()


def test_suggest_parses_json_and_fenced_json(monkeypatch):
    replies = iter(['{"kind": "scene", "reason": "2 people, water, outdoors"}',
                    '```json\n{"kind": "portrait", "reason": "close portrait, 1 person"}\n```'])  # fmt: skip
    monkeypatch.setattr(openrouter, "describe", lambda *a, **k: next(replies))
    im = Image.new("RGB", (10, 10))
    assert photo_kind.suggest(None, "k", im) == ("scene", "2 people, water, outdoors")
    assert photo_kind.suggest(None, "k", im) == ("portrait", "close portrait, 1 person")


@pytest.mark.parametrize("reply", ['{"kind": "landscape"}', "It is a portrait."])
def test_suggest_refuses_junk(monkeypatch, reply):
    monkeypatch.setattr(openrouter, "describe", lambda *a, **k: reply)
    with pytest.raises(ValueError):
        photo_kind.suggest(None, "k", Image.new("RGB", (10, 10)))


def test_suggest_trims_a_long_reason(monkeypatch):
    long = '{"kind": "scene", "reason": "' + "x" * 200 + '"}'
    monkeypatch.setattr(openrouter, "describe", lambda *a, **k: long)
    assert len(photo_kind.suggest(None, "k", Image.new("RGB", (10, 10)))[1]) == 80


def test_classify_new(tmp_path, monkeypatch):
    st = PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")
    a = st.add("upload", "a", jpeg("red"), "a.jpg")
    b = st.add("upload", "b", jpeg("blue"), "b.jpg")
    c = st.add("upload", "c", jpeg("green"), "c.jpg")
    st.set_kind(c.id, "portrait", None, False)  # Brian's own choice stays

    def fake(http, key, im, prompt, model):
        assert model == photo_kind.VISION_MODEL
        if im.getpixel((0, 0))[2] > 200:  # blue
            raise openrouter.EditFailed("OpenRouter returned 402: low balance")
        return '{"kind": "scene", "reason": "outdoors"}'

    monkeypatch.setattr(openrouter, "describe", fake)
    res = photo_kind.classify_new(st, None, "k")
    assert res["classified"] == 1
    assert res["failed"] == [
        {"id": b.id, "error": "OpenRouter returned 402: low balance"}
    ]
    assert (st.get(a.id).kind, st.get(a.id).kind_auto) == ("scene", True)
    assert st.get(b.id).kind is None
    assert (st.get(c.id).kind, st.get(c.id).kind_auto) == ("portrait", False)
