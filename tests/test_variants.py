import json
from pathlib import Path

import pytest
from PIL import Image

from frame_art import openrouter, pipeline, publish, variants
from frame_art.catalogue import Catalogue, Slot, VariantState
from frame_art.model import Work
from frame_art.variants import HALLOWEEN_PROMPT, assign_slots, needed

LIVE = ["a", "b", "c", "d"]  # oldest first


def test_half_mix_takes_newest_works():
    assert assign_slots(LIVE, {"hw": 50}, {}, set()) == {
        "c": Slot("hw", False),
        "d": Slot("hw", False),
    }


def test_slots_stick_and_prefer_works_with_variants():
    cur = {"a": Slot("hw", False)}
    got = assign_slots(LIVE, {"hw": 50}, cur, {("b", "hw")})
    assert set(got) == {"a", "b"}


def test_type_leaving_the_mix_clears_its_slots_even_manual():
    assert assign_slots(LIVE, {}, {"a": Slot("hw", True)}, set()) == {}


def test_shrinking_share_keeps_manual_and_drops_variantless_first():
    cur = {k: Slot("hw", k == "a") for k in LIVE}
    got = assign_slots(LIVE, {"hw": 50}, cur, {("a", "hw"), ("c", "hw"), ("d", "hw")})
    assert set(got) == {"a", "d"}  # b has no variant; then oldest (c) goes


def test_retired_works_lose_their_slot():
    got = assign_slots(["a"], {"hw": 100}, {"z": Slot("hw", False)}, set())
    assert got == {"a": Slot("hw", False)}


def test_needed_skips_usable_dont_use_and_repeated_failures():
    slots = {k: Slot("hw", False) for k in LIVE}
    states = {
        ("a", "hw"): VariantState(5, False, 0),
        ("b", "hw"): VariantState(None, True, 0),
        ("c", "hw"): VariantState(None, False, 3),
    }
    assert needed(slots, states, LIVE) == [("d", "hw")]


def make_work(i: int) -> Work:
    # As in test_publish: key "s1:1" for i=1.
    return Work(
        source=f"s{i % 3}", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=4000, height=2250,
    )  # fmt: skip


def solid(color) -> Image.Image:
    # Full panel size: anything smaller needs upscaling and trips TooSmall.
    return Image.new("RGB", (3840, 2160), color)


ORIGINAL_PHASH = 0x1234ABCD


@pytest.fixture
def vsetup(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    w = make_work(1)
    cat.upsert([w])
    dest = tmp_path / "frame_upload"
    name = pipeline.filename(w)
    (dest / "full").mkdir(parents=True)
    pipeline.write_atomic(solid("navy"), dest / "full" / name, dest / ".staging")
    cat.record_publish(name, "full", w.key, ORIGINAL_PHASH)
    # Pretend HA uploaded it.
    sidecar = {name: {"content_id": "MY_F0001", "modified": 1.0}}
    (dest / "full" / publish.SIDECAR).write_text(json.dumps(sidecar))
    cat.add_variant_type("halloween", "Halloween", HALLOWEEN_PROMPT)

    def fake_edit(http, api_key, im, prompt, model=openrouter.MODEL, size="4K"):
        return openrouter.Edit(solid("orange"), 0.12, "gen-x")

    monkeypatch.setattr(openrouter, "edit_image", fake_edit)
    monkeypatch.setattr(pipeline, "load_work", lambda http, cand: solid("navy"))
    return cat, dest, tmp_path / "data", publish.Publisher(cat, None, dest, "blur")


def test_generate_then_swap_and_revert(vsetup):
    cat, dest, data, pub = vsetup
    v = variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert v.n == 1 and Path(v.path).exists() and v.prompt == HALLOWEEN_PROMPT
    assert v.cost == 0.12 and v.model == openrouter.MODEL
    stem = "s1-1-artist-1-work-1"
    assert v.path == str(data / "variants" / "halloween" / stem / "1.jpg")
    assert (data / "sources" / f"{stem}.jpg").exists()
    cat.set_slots({"s1:1": Slot("halloween", False)})
    res = variants.apply_versions(cat, None, data, pub)
    [live] = cat.published(active=True)
    assert live.filename.endswith("--halloween-1.jpg") and res["swapped"]
    assert res["swapped"] == [f"full/{live.filename}"] and not res["failed"]
    assert cat.published_variants()[live.filename] == v.id
    # The variant carries the original's hash, for the duplicate check.
    assert live.phash == ORIGINAL_PHASH
    assert publish.pending(cat, dest)["retire"] == ["MY_F0001"]
    assert not (dest / "full" / "s1-1-artist-1-work-1.jpg").exists()
    index = json.loads((dest / "_meta" / "index.json").read_text())
    assert list(index) == [f"full/{live.filename}"]
    # Nothing to change: nothing swapped.
    assert variants.apply_versions(cat, None, data, pub)["swapped"] == []
    cat.set_slots({})
    variants.apply_versions(cat, None, data, pub)
    [back] = cat.published(active=True)
    assert back.filename == "s1-1-artist-1-work-1-2.jpg"  # never the old name
    assert back.filename not in cat.published_variants()
    assert "MY_F0001" in publish.pending(cat, dest)["retire"]  # still owed a delete


def test_generate_failure_is_counted_and_reraised(vsetup, monkeypatch):
    cat, dest, data, pub = vsetup

    def boom(*a, **k):
        raise openrouter.EditFailed("HTTP 402: Insufficient credits")

    monkeypatch.setattr(openrouter, "edit_image", boom)
    with pytest.raises(openrouter.EditFailed):
        variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert cat.variant_states()[("s1:1", "halloween")].failures == 1
    assert cat.variants() == []


def test_source_is_cached(vsetup, monkeypatch):
    cat, dest, data, pub = vsetup
    calls = []

    def counting_load(http, cand):
        calls.append(cand.work.key)
        return solid("navy")

    monkeypatch.setattr(pipeline, "load_work", counting_load)
    [w] = cat.works()
    first = variants.load_source(None, data, w, pub.prefs_for(w))
    second = variants.load_source(None, data, w, pub.prefs_for(w))
    assert calls == ["s1:1"]
    assert variants.source_path(data, w).exists()
    assert second.size == first.size == (3840, 2160)


def test_failed_swap_leaves_the_work_alone(vsetup):
    cat, dest, data, pub = vsetup
    v = variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    Path(v.path).unlink()  # the variant file has gone missing
    cat.set_slots({"s1:1": Slot("halloween", False)})
    res = variants.apply_versions(cat, None, data, pub)
    assert res == {"swapped": [], "failed": ["s1:1"]}
    [live] = cat.published(active=True)
    assert live.filename == "s1-1-artist-1-work-1.jpg"
    assert publish.pending(cat, dest)["retire"] == []


def test_add_variant_failure_is_counted(vsetup, monkeypatch):
    cat, dest, data, pub = vsetup

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cat, "add_variant", broken)
    with pytest.raises(RuntimeError):
        variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert cat.variant_states()[("s1:1", "halloween")].failures == 1
