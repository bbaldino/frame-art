import io
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from frame_art import library, openrouter, variants
from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.photos import PhotoStore
from frame_art.providers import OpenRouterProvider
from frame_art.schedules import Schedule, Slice, everyday
from frame_art.settings import Settings
from frame_art.studio import Painter, StudioStore
from frame_art.variants import HALLOWEEN_PROMPT, needed

TODAY = date(2026, 10, 28)
XMAS = "ai-variant:christmas"
HW = "ai-variant:halloween"


def make_work(i: int) -> Work:
    return Work(
        source="s", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=4000, height=2250,
    )  # fmt: skip


def solid(color, size=(3840, 2160)) -> Image.Image:
    return Image.new("RGB", size, color)


def sched(id: str, start: str, end: str, tag: str, share: int) -> Schedule:
    return Schedule(id, id, start, end, [Slice([tag], share)])


@pytest.fixture
def lib(tmp_path):
    """Four museum works s:1 (oldest) .. s:4 (newest), all in the Library."""
    db = tmp_path / "db.sqlite"
    cat = Catalogue(db)
    cat.upsert([make_work(i) for i in range(1, 5)])
    cat.set_library([f"s:{i}" for i in range(1, 5)], "in")
    for i in range(1, 5):
        cat.db.execute("update library set added_at = ? where key = ?", (i, f"s:{i}"))
    cat.add_variant_type("halloween", "Halloween", HALLOWEEN_PROMPT)
    cat.add_variant_type("christmas", "Christmas", "make it Christmas")
    ph = PhotoStore(db, tmp_path / "photos")
    st = StudioStore(db, tmp_path / "studio")
    return cat, ph, st


def all_items(lib):
    return library.items(*lib)


def keys_of(pairs):
    return [k for k, _ in pairs]


def test_needed_fills_a_short_slice(lib):
    cat = lib[0]
    s = [everyday([Slice([HW], 50)])]
    its = all_items(lib)
    # 50% of 4 works: two wanted; newest first.
    assert needed(cat, its, s, 4, [TODAY]) == [
        ("s:4", "halloween"),
        ("s:3", "halloween"),
    ]
    v = cat.add_variant("s:4", "halloween", 1, "p", "m", None, "/v/1.jpg")
    its = all_items(lib)
    assert needed(cat, its, s, 4, [TODAY]) == [("s:3", "halloween")]
    cat.add_variant("s:3", "halloween", 1, "p", "m", None, "/v/2.jpg")
    assert needed(cat, all_items(lib), s, 4, [TODAY]) == []
    assert v.id  # sanity


def test_needed_caps_the_pool_at_the_eligible_roots(lib):
    cat = lib[0]
    s = [everyday([Slice([HW], 100)])]
    assert len(needed(cat, all_items(lib), s, 150, [TODAY])) == 4


def test_needed_looks_a_week_ahead(lib):
    cat = lib[0]
    s = [everyday([Slice([HW], 25)]), sched("x", "11-02", "11-02", XMAS, 50)]
    days = [date.fromordinal(TODAY.toordinal() + i) for i in range(8)]
    its = all_items(lib)
    assert needed(cat, its, s, 4, [TODAY]) == [("s:4", "halloween")]
    # Today's pair comes first; Nov 2's christmas pairs follow, deduplicated.
    assert needed(cat, its, s, 4, days) == [
        ("s:4", "halloween"),
        ("s:4", "christmas"),
        ("s:3", "christmas"),
    ]
    assert needed(cat, its, s, 4, days, type="christmas") == [
        ("s:4", "christmas"),
        ("s:3", "christmas"),
    ]


def test_needed_narrowest_schedule_first(lib):
    cat = lib[0]
    wide = everyday([Slice([HW], 25)])
    narrow = sched("x", "10-28", "10-28", XMAS, 25)
    got = needed(cat, all_items(lib), [wide, narrow], 4, [TODAY])
    assert got == [("s:4", "christmas"), ("s:4", "halloween")]


def test_needed_skips_removed_and_failing(lib):
    cat = lib[0]
    s = [everyday([Slice([HW], 100)])]
    v = cat.add_variant("s:4", "halloween", 1, "p", "m", None, "/v/1.jpg")
    cat.set_library([f"variant:{v.id}"], "removed")  # a removed version: not a version
    cat.set_library(["s:3"], "removed")  # a removed work
    for _ in range(variants.NIGHTLY_FAILURES):
        cat.note_variant_failure("s:2", "halloween")
    cat.note_variant_failure("s:1", "halloween")  # under the limit: still tried
    got = needed(cat, all_items(lib), s, 4, [TODAY])
    # s:4 has a removed version: Brian uses Regenerate for that, so no retry.
    assert keys_of(got) == ["s:1"]


def test_needed_skips_works_with_a_version_already(lib):
    cat = lib[0]
    s = [everyday([Slice([HW], 100)])]
    cat.add_variant("s:4", "halloween", 1, "p", "m", None, "/v/1.jpg")
    # 3 wanted beyond the one that exists? want 4, have 1, missing 3.
    assert keys_of(needed(cat, all_items(lib), s, 4, [TODAY])) == ["s:3", "s:2", "s:1"]


def test_needed_prefers_live_originals(lib):
    cat = lib[0]
    cat.record_publish("s-1-artist-1-work-1.jpg", "full", "s:1", 1)
    s = [everyday([Slice([HW], 25)])]
    assert keys_of(needed(cat, all_items(lib), s, 4, [TODAY])) == ["s:1"]


def test_needed_uses_photos_and_portraits_too(lib, tmp_path):
    cat, ph, st = lib
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    photo = ph.add("upload", "a.jpg", buf.getvalue(), "a.jpg")
    cat.set_library([f"photo:{photo.id}"], "in")
    s = [everyday([Slice([HW], 20)])]
    got = needed(cat, library.items(cat, ph, st), s, 150, [TODAY])
    assert keys_of(got) == [f"photo:{photo.id}"]  # newest Library add


@pytest.fixture
def provider(monkeypatch):
    calls = []

    def fake_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        calls.append((im.size, prompt, aspect))
        return openrouter.Edit(solid("orange"), 0.12, "gen-x")

    monkeypatch.setattr(openrouter, "edit_image", fake_edit)
    p = OpenRouterProvider(None, "k")
    p.calls = calls
    return p


def fake_load(monkeypatch, size=(3840, 2160)):
    from frame_art import pipeline

    monkeypatch.setattr(pipeline, "load_work", lambda http, cand: solid("navy", size))


def test_generate_a_museum_version(lib, tmp_path, provider, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)
    data = tmp_path / "data"
    orig = all_items(lib)["s:1"]
    v = variants.generate(cat, None, data, provider, orig, "halloween", "strict")
    assert v.n == 1 and v.prompt == HALLOWEEN_PROMPT and v.cost == 0.12
    assert v.path == str(data / "variants" / "halloween" / "s-1" / "1.jpg")
    assert Path(v.path).exists() and v.key == "s:1"
    assert provider.calls[0][2] == "16:9"  # fills the screen
    assert f"variant:{v.id}" in all_items(lib)


def test_generate_from_photo_and_portrait(lib, tmp_path, provider):
    cat, ph, st = lib
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "red").save(buf, "JPEG")
    photo = ph.add("upload", "a.jpg", buf.getvalue(), "a.jpg")
    t = st.start_try(
        photo.id, Painter("hopper", "Edward Hopper", "Flat.", ("portrait",)),
        "keep", "fill", [], None,
    )  # fmt: skip
    st.finish_try(t.id, Image.new("RGB", (320, 180), "orange"), 0.1)
    st.save(t.id)
    its = library.items(cat, ph, st)
    data = tmp_path / "data"
    v1 = variants.generate(
        cat, None, data, provider, its["photo:" + photo.id], "halloween", "strict"
    )
    v2 = variants.generate(
        cat, None, data, provider, its["portrait:1"], "halloween", "strict"
    )
    assert v1.key == "photo:" + photo.id and v2.key == "portrait:1"
    assert v1.path == str(
        data / "variants" / "halloween" / f"photo-{photo.id}" / "1.jpg"
    )
    assert Path(v1.path).exists() and Path(v2.path).exists()
    assert provider.calls[0][0] == (400, 300) and provider.calls[0][2] is None
    assert provider.calls[1][0] == (320, 180)


def test_generate_failure_is_counted_and_reraised(lib, tmp_path, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)

    def boom(*a, **k):
        raise openrouter.EditFailed("HTTP 402: Insufficient credits")

    monkeypatch.setattr(openrouter, "edit_image", boom)
    with pytest.raises(openrouter.EditFailed):
        variants.generate(
            cat, None, tmp_path, OpenRouterProvider(None, "k"),
            all_items(lib)["s:1"], "halloween", "strict",
        )  # fmt: skip
    assert cat.variant_states()[("s:1", "halloween")].failures == 1
    assert cat.variants() == []


def test_regenerate_removes_the_old_version(lib, tmp_path, provider, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)
    old = variants.generate(
        cat, None, tmp_path, provider, all_items(lib)["s:1"], "halloween", "strict"
    )
    new = variants.regenerate(
        cat, None, tmp_path, provider, all_items(lib), old.id, "strict"
    )
    assert new.n == 2 and new.key == "s:1" and new.type == "halloween"
    lib_state = cat.library()
    assert lib_state[f"variant:{old.id}"][0] == "removed"
    keys = {i.key for i in library.eligible(cat, all_items(lib))}
    assert f"variant:{new.id}" in keys and f"variant:{old.id}" not in keys


def test_regenerate_failure_keeps_the_old_version(lib, tmp_path, provider, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)
    old = variants.generate(
        cat, None, tmp_path, provider, all_items(lib)["s:1"], "halloween", "strict"
    )
    monkeypatch.setattr(
        openrouter,
        "edit_image",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    with pytest.raises(RuntimeError):
        variants.regenerate(
            cat, None, tmp_path, provider, all_items(lib), old.id, "strict"
        )
    assert f"variant:{old.id}" not in cat.library()


def settings_with(*sch, limit=2):
    return Settings(schedules=list(sch), variant_limit=limit)


def test_nightly_respects_the_limit(lib, tmp_path, provider, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)
    monkeypatch.setattr(variants, "OpenRouterProvider", lambda http, key: provider)
    s = settings_with(everyday([Slice([HW], 100)]))
    out = variants.nightly(cat, None, tmp_path, all_items(lib), s, "k", TODAY)
    assert out["generated"] == ["s:4", "s:3"] and out["gen_failed"] == []
    assert out["cost"] == 0.24 and len(cat.variants()) == 2


def test_nightly_counts_failures(lib, tmp_path, provider, monkeypatch):
    cat = lib[0]
    fake_load(monkeypatch)
    monkeypatch.setattr(variants, "OpenRouterProvider", lambda http, key: provider)
    real = variants.generate

    def flaky(cat, http, data, prov, original, type, crop, prompt=None):
        if original.key == "s:4":
            raise RuntimeError("no")
        return real(cat, http, data, prov, original, type, crop, prompt)

    monkeypatch.setattr(variants, "generate", flaky)
    s = settings_with(everyday([Slice([HW], 100)]))
    out = variants.nightly(cat, None, tmp_path, all_items(lib), s, "k", TODAY)
    assert out["gen_failed"] == ["s:4"] and out["generated"] == [
        "s:3"
    ]  # the limit counts the failure


def test_nightly_without_a_key_makes_nothing(lib, tmp_path):
    cat = lib[0]
    s = settings_with(everyday([Slice([HW], 100)]))
    out = variants.nightly(cat, None, tmp_path, all_items(lib), s, None, TODAY)
    assert out == {"generated": [], "gen_failed": [], "cost": 0.0}
    assert cat.variants() == []


def test_make_now_count_uses_next_run(lib):
    cat = lib[0]
    its = all_items(lib)
    s = settings_with(sched("x", "11-02", "11-02", XMAS, 50))
    # Nothing runs today; the first run is Nov 2: 50% of 4 = 2.
    assert variants.make_now_count(cat, its, s, "christmas", TODAY) == 2
    assert variants.make_now_count(cat, its, s, "halloween", TODAY) == 0
    cat.add_variant("s:4", "christmas", 1, "p", "m", None, "/v/1.jpg")
    assert variants.make_now_count(cat, all_items(lib), s, "christmas", TODAY) == 1
    assert variants.make_now_count(cat, its, settings_with(), "christmas", TODAY) == 0


def test_make_now_count_next_year(lib):
    cat = lib[0]
    s = settings_with(sched("x", "03-01", "03-01", XMAS, 25))
    assert variants.make_now_count(cat, all_items(lib), s, "christmas", TODAY) == 1


def test_seed_types(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    variants.seed_types(cat)
    assert [t.slug for t in cat.variant_types()] == ["halloween"]
    variants.seed_types(cat)
    assert len(cat.variant_types()) == 1
