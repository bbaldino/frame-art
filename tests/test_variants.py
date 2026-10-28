import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from frame_art import openrouter, pipeline, publish, variants
from frame_art.catalogue import Catalogue, Slot, VariantState
from frame_art.model import Work
from frame_art.schedules import Schedule, everyday
from frame_art.settings import Settings
from frame_art.variants import HALLOWEEN_PROMPT, assign_slots, needed

LIVE = ["a", "b", "c", "d"]  # oldest first
TODAY = date(2026, 10, 28)


def test_half_mix_takes_newest_works():
    assert assign_slots(LIVE, {"hw": 2}, {}, set()) == {
        "c": Slot("hw", False),
        "d": Slot("hw", False),
    }


def test_slots_stick_and_prefer_works_with_variants():
    cur = {"a": Slot("hw", False)}
    got = assign_slots(LIVE, {"hw": 2}, cur, {("b", "hw")})
    assert set(got) == {"a", "b"}


def test_type_leaving_the_mix_clears_its_slots_even_manual():
    assert assign_slots(LIVE, {}, {"a": Slot("hw", True)}, set()) == {}


def test_shrinking_share_keeps_manual_and_drops_variantless_first():
    cur = {k: Slot("hw", k == "a") for k in LIVE}
    got = assign_slots(LIVE, {"hw": 2}, cur, {("a", "hw"), ("c", "hw"), ("d", "hw")})
    assert set(got) == {"a", "d"}  # b has no variant; then oldest (c) goes


def test_retired_works_lose_their_slot():
    got = assign_slots(["a"], {"hw": 1}, {"z": Slot("hw", False)}, set())
    assert got == {"a": Slot("hw", False)}


def test_ready_only_shows_only_made_versions():
    cur = {"a": Slot("hw", False), "b": Slot("hw", True)}
    got = assign_slots(
        LIVE, {"hw": 3}, cur, {("c", "hw"), ("b", "hw")}, ready_only=True
    )
    assert got == {"b": Slot("hw", True), "c": Slot("hw", False)}


def test_ready_only_narrower_type_takes_works_from_a_wider_one():
    # Listed narrowest first: birthday's ready versions are on works Halloween holds.
    cur = {k: Slot("hw", False) for k in LIVE}
    usable = {(k, "hw") for k in LIVE} | {("c", "bd"), ("d", "bd")}
    got = assign_slots(LIVE, {"bd": 2, "hw": 2}, cur, usable, ready_only=True)
    assert got == {
        "a": Slot("hw", False),
        "b": Slot("hw", False),
        "c": Slot("bd", False),
        "d": Slot("bd", False),
    }


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

    def fake_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
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


@pytest.fixture
def vsetup4(tmp_path, monkeypatch):
    """Four live works (s1:1 oldest ... s1:4 newest), all Halloween, two edits a night."""
    cat = Catalogue(tmp_path / "db.sqlite")
    works = [make_work(i) for i in range(1, 5)]
    cat.upsert(works)
    dest = tmp_path / "frame_upload"
    (dest / "full").mkdir(parents=True)
    sidecar = {}
    for i, w in enumerate(works, 1):
        name = pipeline.filename(w)
        pipeline.write_atomic(solid("navy"), dest / "full" / name, dest / ".staging")
        cat.record_publish(name, "full", w.key, ORIGINAL_PHASH + i)
        # Distinct times, so the publish order is the one above.
        cat.db.execute(
            "update published set published_at = ? where filename = ?", (i, name)
        )
        sidecar[name] = {"content_id": f"MY_F000{i}", "modified": 1.0}
    (dest / "full" / publish.SIDECAR).write_text(json.dumps(sidecar))
    cat.add_variant_type("halloween", "Halloween", HALLOWEEN_PROMPT)

    def fake_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        return openrouter.Edit(solid("orange"), 0.12, "gen-x")

    monkeypatch.setattr(openrouter, "edit_image", fake_edit)
    monkeypatch.setattr(pipeline, "load_work", lambda http, cand: solid("navy"))
    pub = publish.Publisher(cat, None, dest, "blur")
    settings = Settings(schedules=[everyday({"halloween": 100})], variant_limit=2)
    return cat, tmp_path / "data", pub, settings


def test_nightly_shows_only_ready_versions(vsetup4):
    cat, data, pub, settings = vsetup4
    out = variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    assert out["generated"] == ["s1:4", "s0:3"]  # newest first
    assert out["slots"] == {"halloween": 2} and len(out["swapped"]) == 2
    assert out["gen_failed"] == [] and out["cost"] == 0.24
    assert set(cat.slots()) == {"s1:4", "s0:3"}  # the rest stay originals
    out = variants.nightly(cat, None, data, pub, settings, "k", date(2026, 10, 29))
    assert out["generated"] == ["s2:2", "s1:1"]  # same works kept, two more made
    assert out["slots"] == {"halloween": 4}


def _with_birthday(cat, settings, *schedules, limit=2):
    cat.add_variant_type("birthday", "Birthday", "make it a birthday")
    return dataclasses.replace(settings, schedules=list(schedules), variant_limit=limit)


def test_shortfall_gives_the_rest_to_the_wider_schedule(vsetup4):
    cat, data, pub, settings = vsetup4
    for key in ("s1:1", "s2:2", "s0:3", "s1:4"):
        variants.generate(cat, None, data, "k", pub, key, "halloween")
    bday = Schedule("b", "Birthday", "10-28", "10-28", {"birthday": 50})
    s = _with_birthday(cat, settings, everyday({"halloween": 100}), bday, limit=0)
    out = variants.nightly(cat, None, data, pub, s, "k", TODAY)
    # No birthday versions yet: all four keep Halloween, none revert to originals.
    assert out["generated"] == [] and out["slots"] == {"halloween": 4}
    assert {sl.type for sl in cat.slots().values()} == {"halloween"}


NOV2 = Schedule("b", "Birthday", "11-02", "11-02", {"birthday": 50})


def test_lookahead_makes_but_does_not_show(vsetup4):
    cat, data, pub, settings = vsetup4
    s = _with_birthday(cat, settings, NOV2)
    out = variants.nightly(cat, None, data, pub, s, "k", TODAY)
    assert out["generated"] == ["s1:4", "s0:3"]
    assert out["slots"] == {} and cat.slots() == {} and out["swapped"] == []
    out = variants.nightly(cat, None, data, pub, s, "k", date(2026, 11, 2))
    assert out["slots"] == {"birthday": 2} and out["generated"] == []
    assert set(cat.slots()) == {"s1:4", "s0:3"}


def test_lookahead_keeps_making_for_the_same_works(vsetup4):
    cat, data, pub, settings = vsetup4
    s = _with_birthday(cat, settings, NOV2)
    variants.nightly(cat, None, data, pub, s, "k", TODAY)
    out = variants.nightly(cat, None, data, pub, s, "k", date(2026, 10, 29))
    assert out["generated"] == [] and len(cat.variants()) == 2


def test_lookahead_inside_a_wider_schedule_shows_on_its_first_day(vsetup4):
    cat, data, pub, settings = vsetup4
    s = _with_birthday(cat, settings, everyday({"halloween": 100}), NOV2)
    for day in (20, 21):  # Halloween fills all four works before the lookahead
        variants.nightly(cat, None, data, pub, s, "k", date(2026, 10, day))
    assert cat.slots() and {sl.type for sl in cat.slots().values()} == {"halloween"}
    out = variants.nightly(cat, None, data, pub, s, "k", TODAY)
    assert {v.type for v in cat.variants()} == {"halloween", "birthday"}
    assert out["slots"] == {"halloween": 4}  # not shown before Nov 2
    off = dataclasses.replace(s, variant_limit=0)
    out = variants.nightly(cat, None, data, pub, off, "k", date(2026, 11, 2))
    assert out["slots"] == {"birthday": 2, "halloween": 2}


def test_budget_goes_to_the_narrowest_schedule_first(vsetup4):
    cat, data, pub, settings = vsetup4
    s = _with_birthday(cat, settings, everyday({"halloween": 100}), NOV2)
    out = variants.nightly(cat, None, data, pub, s, "k", TODAY)
    assert out["generated"] == ["s1:4", "s0:3"]
    assert {v.type for v in cat.variants()} == {"birthday"}
    assert out["slots"] == {}  # no Halloween versions ready, so originals today


def test_nightly_without_key_or_limit_still_reverts(vsetup4):
    cat, data, pub, settings = vsetup4
    variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    assert len(cat.published_variants()) == 2
    off = dataclasses.replace(settings, schedules=[], variant_limit=0)
    out = variants.nightly(cat, None, data, pub, off, None, TODAY)
    assert len(out["swapped"]) == 2 and out["generated"] == []
    assert out["slots"] == {}
    live = {p.filename for p in cat.published(active=True)}
    assert not live & set(cat.published_variants())


def test_nightly_without_key_generates_nothing(vsetup4):
    cat, data, pub, settings = vsetup4
    out = variants.nightly(cat, None, data, pub, settings, None, TODAY)
    assert out["generated"] == out["gen_failed"] == out["swapped"] == []
    assert cat.variant_states() == {}  # no attempt, so no failure counted


def test_nightly_ignores_types_that_do_not_exist(vsetup4):
    cat, data, pub, settings = vsetup4
    mix = {"halloween": 50, "winter": 50}
    s = dataclasses.replace(settings, schedules=[everyday(mix)])
    out = variants.nightly(cat, None, data, pub, s, "k", TODAY)
    assert out["slots"] == {"halloween": 2}


def test_nightly_carries_on_after_a_failed_edit(vsetup4, monkeypatch):
    cat, data, pub, settings = vsetup4
    calls = []

    def flaky(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        calls.append(prompt)
        if len(calls) == 1:
            raise openrouter.EditFailed("HTTP 502: upstream error")
        return openrouter.Edit(solid("orange"), 0.12, "gen-x")

    monkeypatch.setattr(openrouter, "edit_image", flaky)
    out = variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    assert len(out["gen_failed"]) == 1 and len(out["generated"]) == 1
    assert out["gen_failed"] == ["s1:4"] and out["generated"] == ["s0:3"]
    assert out["cost"] == 0.12 and len(out["swapped"]) == 1
    assert cat.variant_states()[("s1:4", "halloween")].failures == 1


def test_progress_lists_stuck_works(vsetup4):
    cat, data, pub, settings = vsetup4
    variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    for _ in range(variants.NIGHTLY_FAILURES):
        cat.note_variant_failure("s2:2", "halloween")
    [p] = variants.progress(cat, settings, TODAY)
    assert p["slug"] == "halloween" and p["name"] == "Halloween"
    assert p["prompt"] == HALLOWEEN_PROMPT
    assert p["share"] == 100 and p["live_share"] == 50
    assert p["slotted"] == 4 and p["ready"] == 2
    assert p["needed"] == 1 and p["stuck"] == ["s2:2"]
    assert p["cost"] == 0.24


def test_progress_counts(vsetup4):
    cat, data, pub, settings = vsetup4
    variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    [p] = variants.progress(cat, settings, TODAY)
    assert (p["ready"], p["needed"], p["stuck"], p["cost"]) == (2, 2, [], 0.24)


def test_seed_types_once_and_only_when_empty(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    variants.seed_types(cat)
    assert [t.slug for t in cat.variant_types()] == ["halloween"]
    cat.db.execute("delete from variant_types")
    cat.db.commit()
    variants.seed_types(cat)  # Brian deleted it: it stays gone
    assert cat.variant_types() == []
    other = Catalogue(tmp_path / "other.sqlite")
    other.add_variant_type("winter", "Winter", "make it snowy")
    variants.seed_types(other)
    assert [t.slug for t in other.variant_types()] == ["winter"]


def test_live_share_counts_only_files_on_the_tv(vsetup4):
    cat, data, pub, settings = vsetup4
    variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    assert variants.progress(cat, settings, TODAY)[0]["live_share"] == 50
    off = dataclasses.replace(settings, schedules=[], variant_limit=0)
    variants.nightly(cat, None, data, pub, off, None, TODAY)  # back to originals
    assert variants.progress(cat, off, TODAY)[0]["live_share"] == 0


def test_swap_keeps_the_age_so_retirement_takes_the_oldest(vsetup4):
    cat, data, pub, settings = vsetup4
    variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    cat.set_slots({"s1:1": Slot("halloween", False)})
    [before] = [p for p in cat.published(active=True) if p.key == "s1:1"]
    assert variants.apply_versions(cat, None, data, pub)["swapped"]
    [after] = [p for p in cat.published(active=True) if p.key == "s1:1"]
    assert after.filename != before.filename
    assert after.published_at == before.published_at == 1
    retired = pub._retire(pool=3)
    assert retired == [f"full/{after.filename}"]  # the oldest work, though just swapped


def test_full_screen_source_gets_a_full_screen_variant(vsetup, monkeypatch):
    """A 1.6:1 work crops to fill the screen; its variant must too.

    The nearest edit aspect (3:2) would need more crop than strict allows.
    """
    cat, dest, data, _ = vsetup
    monkeypatch.setattr(
        pipeline, "load_work", lambda http, cand: Image.new("RGB", (4000, 2500))
    )
    asked = []

    def fake_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        aspect = aspect or openrouter.nearest_aspect(*im.size)
        asked.append(aspect)
        size = {"3:2": (3840, 2560), "16:9": (3840, 2160)}[aspect]
        return openrouter.Edit(Image.new("RGB", size, "orange"), 0.12, "gen-x")

    monkeypatch.setattr(openrouter, "edit_image", fake_edit)
    # Matte: a padded render would land in matted/.
    pub = publish.Publisher(cat, None, dest, "matte")
    variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert asked == ["16:9"]
    cat.set_slots({"s1:1": Slot("halloween", False)})
    [swapped] = variants.apply_versions(cat, None, data, pub)["swapped"]
    assert swapped.startswith("full/")


def test_swap_keeps_a_live_work_that_no_longer_fills_the_screen(vsetup, monkeypatch):
    """New-pick gates (full screen only) don't apply to a work already live."""
    cat, dest, data, _ = vsetup

    def square_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        return openrouter.Edit(Image.new("RGB", (2400, 2400), "orange"), 0.1, "g")

    monkeypatch.setattr(openrouter, "edit_image", square_edit)
    prefs = pipeline.Prefs(full_screen_only=True)
    pub = publish.Publisher(cat, None, dest, "matte", prefs=prefs)
    variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    cat.set_slots({"s1:1": Slot("halloween", False)})
    res = variants.apply_versions(cat, None, data, pub)
    assert res["failed"] == [] and res["swapped"][0].startswith("matted/")


def test_nightly_reports_failed_swaps(vsetup4, monkeypatch, caplog):
    cat, data, pub, settings = vsetup4

    def broken(p, im, variant):
        raise OSError("disk full")

    monkeypatch.setattr(pub, "swap", broken)
    out = variants.nightly(cat, None, data, pub, settings, "k", TODAY)
    assert out["swap_failed"] == ["s0:3", "s1:4"] and out["swapped"] == []
    assert "s1:4: could not swap versions: OSError: disk full" in caplog.text
