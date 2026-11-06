import dataclasses
import io

import pytest
from PIL import Image

from frame_art.catalogue import Catalogue
from frame_art.library import (
    Item,
    add,
    display_tag,
    eligible,
    items,
    live,
    matches,
    put_back,
    remove,
    source_image,
    state,
    work_tags,
)
from frame_art.model import Work
from frame_art.photos import PhotoStore
from frame_art.studio import Painter, StudioStore

HOPPER = Painter("hopper", "Edward Hopper", "Flat planes.", ("portrait",))


def monet() -> Work:
    return Work(
        source="nga", source_id="1", title="Haystacks", artist="Claude Monet",
        date="1890", kind="painting", license="CC0", image_url="http://x/1.jpg",
        subjects=["Haystacks", "Field"], style="Impressionism", genre="landscape",
        place="Giverny",
    )  # fmt: skip


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "db.sqlite"
    cat = Catalogue(db)
    w = monet()
    cat.upsert([w])
    ph = PhotoStore(db, tmp_path / "photos")
    st = StudioStore(db, tmp_path / "studio")
    photo = ph.add("upload", "a.jpg", jpeg(), "a.jpg")
    ph.set_kind(photo.id, "scene", "test", False)
    t = st.start_try(photo.id, HOPPER, "keep", "fill", [], None)
    st.finish_try(t.id, Image.new("RGB", (320, 180), "orange"), 0.1)
    st.save(t.id)
    cat.add_variant_type("halloween", "Halloween", "spooky")
    v1 = cat.add_variant("nga:1", "halloween", 1, "p", "m", None, "/v/1.jpg")
    v2 = cat.add_variant("nga:1", "halloween", 2, "p", "m", None, "/v/2.jpg")
    cat.set_tag(["nga:1"], "mine", True)
    cat.collect("nga:1")
    return cat, ph, st, photo, (v1, v2)


def test_museum_tags():
    assert work_tags(monet()) >= {
        "artist:claude monet",
        "style:impressionism",
        "genre:landscape",
        "place:giverny",
        "subject:haystacks",
        "subject:field",
        "kind:painting",
        "source:nga",
    }


def test_museum_item(setup):
    cat, ph, st, photo, _ = setup
    it = items(cat, ph, st)["nga:1"]
    assert (it.kind, it.title, it.root) == ("museum", "Haystacks", "nga:1")
    assert it.added_at == cat.work_added()["nga:1"] > 0
    assert it.work and it.path is None
    assert {"yours:mine", "yours:favorite", "artist:claude monet"} <= it.tags
    cat.db.execute("delete from work_added")
    assert items(cat, ph, st)["nga:1"].added_at == 0.0


def test_version_inherits_tags_but_not_yours(setup):
    cat, ph, st, photo, (v1, _) = setup
    v = items(cat, ph, st)[f"variant:{v1.id}"]
    assert v.kind == "version" and v.vtype == "halloween" and v.variant_id == v1.id
    assert "ai-variant:halloween" in v.tags and "artist:claude monet" in v.tags
    assert not any(t.startswith("yours:") for t in v.tags)
    assert v.root == "nga:1" and v.title == "Haystacks (Halloween)"
    assert (
        v.path == "/v/1.jpg" and v.added_at == v1.created_at and v.work.key == "nga:1"
    )
    cat.set_tag([v.key], "own", True)
    assert "yours:own" in items(cat, ph, st)[v.key].tags


def test_version_title_falls_back_to_slug(setup):
    cat, ph, st, photo, (v1, _) = setup
    cat.add_variant("nga:1", "gone", 1, "p", "m", None, "/v/3.jpg")
    assert items(cat, ph, st)["variant:3"].title == "Haystacks (gone)"


def test_version_of_missing_original_skipped(setup):
    cat, ph, st, *_ = setup
    cat.add_variant("nga:404", "halloween", 1, "p", "m", None, "/v/9.jpg")
    assert "variant:3" not in items(cat, ph, st)


def test_photo_and_portrait_items(setup):
    cat, ph, st, photo, _ = setup
    its = items(cat, ph, st)
    p = its["photo:" + photo.id]
    assert p.tags >= {"source:photos", "kind:scene"} and p.title == "a.jpg"
    assert p.kind == "photo" and p.added_at == photo.added and p.path == photo.path
    pt = its["portrait:1"]
    assert pt.tags >= {"source:studio", "kind:painting", "artist:edward hopper"}
    assert pt.kind == "portrait" and pt.title == "Portrait by Edward Hopper"
    assert pt.path and pt.root == "portrait:1"


def test_photo_without_kind_has_no_kind_tag(setup):
    cat, ph, st, photo, _ = setup
    ph.set_kind(photo.id, None, None, False)
    tags = items(cat, ph, st)["photo:" + photo.id].tags
    assert not any(t.startswith("kind:") for t in tags)


def test_unknown_painter_falls_back_to_slug(setup):
    cat, ph, st, photo, _ = setup
    st.db.execute("update paintings set painter = 'ghost'")
    it = items(cat, ph, st)["portrait:1"]
    assert "artist:ghost" in it.tags and it.title == "Portrait by ghost"


def test_state():
    lib = {"a": ("in", 1.0), "b": ("removed", 2.0)}
    assert [state(lib, k) for k in "abc"] == ["in", "removed", "out"]


def test_eligibility(setup):
    cat, ph, st, photo, _ = setup
    cat.set_library(["nga:1"], "in")
    cat.set_library(["variant:2"], "removed")
    keys = {i.key for i in eligible(cat, items(cat, ph, st))}
    assert "nga:1" in keys and "variant:1" in keys and "variant:2" not in keys
    assert "photo:" + photo.id not in keys
    cat.set_library(["nga:1"], "removed")
    keys = {i.key for i in eligible(cat, items(cat, ph, st))}
    assert not {"nga:1", "variant:1"} & keys


def test_rejected_is_not_eligible(setup):
    cat, ph, st, *_ = setup
    cat.set_library(["nga:1"], "in")
    cat.record_reject("nga:1", "test")
    keys = {i.key for i in eligible(cat, items(cat, ph, st))}
    assert "nga:1" not in keys


def test_live_maps_version_files(setup):
    cat, ph, st, *_ = setup
    cat.record_publish("a.jpg", "full", "nga:1", 0, published_at=5.0)
    cat.record_publish(
        "x--halloween-1.jpg", "full", "nga:1", 0, variant=1, published_at=2.0
    )
    cat.record_publish("p.jpg", "full", "portrait:1", 0, published_at=9.0)
    got = live(cat, items(cat, ph, st))
    assert [(x.filename, x.key, x.root) for x in got] == [
        ("x--halloween-1.jpg", "variant:1", "nga:1"),
        ("a.jpg", "nga:1", "nga:1"),
        ("p.jpg", "portrait:1", "portrait:1"),
    ]
    assert got[0].folder == "full" and got[0].published_at == 2.0


def test_matches():
    it = Item("k", "museum", "t", 0.0, frozenset({"artist:a", "kind:painting"}), "k")
    assert matches(it, ["artist:a"]) and matches(it, ["artist:a", "kind:painting"])
    assert not matches(it, ["artist:a", "kind:print"]) and matches(it, [])
    assert not matches(
        Item("k", "version", "t", 0.0, frozenset({"ai-variant:christmas"}), "k"),
        ["subject:christmas"],
    )


def test_display_tag():
    assert display_tag("ai-variant:christmas") == "ai-variant-christmas"
    assert display_tag("artist:monet") == "artist:monet"


def test_remove_original_removes_versions_by_default(setup):
    cat, ph, st, *_ = setup
    cat.set_library(["nga:1"], "in")
    got = remove(cat, items(cat, ph, st), ["nga:1"])
    assert sorted(got) == ["nga:1", "variant:1", "variant:2"]
    lib = cat.library()
    assert all(state(lib, k) == "removed" for k in got)


def test_remove_original_can_keep_versions(setup):
    cat, ph, st, *_ = setup
    cat.set_library(["nga:1"], "in")
    got = remove(cat, items(cat, ph, st), ["nga:1"], with_versions=False)
    assert got == ["nga:1"]
    assert state(cat.library(), "variant:1") == "out"


def test_put_back_original_leaves_versions_removed(setup):
    cat, ph, st, *_ = setup
    cat.set_library(["nga:1"], "in")
    remove(cat, items(cat, ph, st), ["nga:1"])
    put_back(cat, ["nga:1"])
    lib = cat.library()
    assert state(lib, "nga:1") == "in"
    assert state(lib, "variant:1") == "removed"
    put_back(cat, ["variant:1"])
    assert state(cat.library(), "variant:1") == "out"


def test_adding_a_version_puts_it_back(setup):
    cat, *_ = setup
    cat.set_library(["variant:1"], "removed")
    add(cat, ["variant:1", "nga:1"])
    lib = cat.library()
    assert state(lib, "variant:1") == "out" and state(lib, "nga:1") == "in"


def test_source_image_for_photo_and_portrait(setup, tmp_path):
    cat, ph, st, photo, _ = setup
    all_items = items(cat, ph, st)
    im = source_image(all_items["photo:" + photo.id], None, tmp_path, "strict")
    assert im.size == (40, 30)
    portrait = next(i for i in all_items.values() if i.kind == "portrait")
    assert source_image(portrait, None, tmp_path, "strict").size == (320, 180)


def test_crawl_tags_and_nude_titles_are_subjects():
    # So the Library can filter on them by hand: nothing is filtered for you.
    w = dataclasses.replace(monet(), title="Bathers", tags=["nude", "winter"])
    assert {"subject:nude", "subject:winter"} <= work_tags(w)
    w = dataclasses.replace(monet(), title="Venus and Adonis", tags=[])
    assert "subject:nude" in work_tags(w)
    assert "subject:nude" not in work_tags(monet())
