import pytest

from frame_art.catalogue import Catalogue, Slot
from frame_art.model import Work


@pytest.fixture
def cat(tmp_path):
    c = Catalogue(tmp_path / "db.sqlite")
    c.upsert(
        Work(
            source="nga",
            source_id=i,
            title="T",
            artist="A",
            date=None,
            kind="painting",
            license="CC0",
            image_url="http://x/1.jpg",
        )  # fmt: skip
        for i in ("1", "2")
    )
    return c


def test_variant_lifecycle(cat):
    cat.add_variant_type("halloween", "Halloween", "Make it spooky")
    with pytest.raises(ValueError):
        cat.add_variant_type("halloween", "Again", "x")
    assert cat.next_variant_n("nga:1", "halloween") == 1
    v1 = cat.add_variant("nga:1", "halloween", 1, "p1", "m", 0.12, "/d/1.jpg")
    v2 = cat.add_variant("nga:1", "halloween", 2, "p2", "m", None, "/d/2.jpg")
    assert cat.next_variant_n("nga:1", "halloween") == 3
    assert cat.usable_variants()[("nga:1", "halloween")].id == v2.id
    cat.set_dont_use("nga:1", "halloween", True)
    assert ("nga:1", "halloween") not in cat.usable_variants()
    cat.set_current_variant("nga:1", "halloween", v1.id)  # clears don't use
    assert cat.usable_variants()[("nga:1", "halloween")].id == v1.id
    assert cat.note_variant_failure("nga:1", "halloween") == 1
    cat.add_variant("nga:1", "halloween", 3, "p3", "m", 0.1, "/d/3.jpg")
    assert cat.variant_states()[("nga:1", "halloween")].failures == 0


def test_variant_types_and_lookups(cat):
    cat.add_variant_type("halloween", "Halloween", "spooky")
    cat.add_variant_type("xmas", "Christmas", "festive")
    cat.update_variant_type("halloween", "Hallowe'en", "creepy")
    assert [(t.slug, t.name, t.prompt) for t in cat.variant_types()] == [
        ("halloween", "Hallowe'en", "creepy"),
        ("xmas", "Christmas", "festive"),
    ]
    v = cat.add_variant("nga:1", "xmas", 1, "p", "m", 0.5, "/d/1.jpg")
    cat.add_variant("nga:2", "xmas", 1, "p", "m", None, "/d/2.jpg")
    assert cat.variant(v.id) == v
    assert cat.variant(999) is None
    assert [x.key for x in cat.variants()] == ["nga:1", "nga:2"]
    assert cat.variants("nga:2")[0].cost is None


def test_unused_filename_never_reuses_a_published_name(cat):
    cat.record_publish("a.jpg", "full", "nga:1", 0)
    cat.record_retire("a.jpg", "MY_F0001")
    assert cat.unused_filename("a.jpg") == "a-2.jpg"
    assert cat.unused_filename("b.jpg") == "b.jpg"
    cat.record_publish("a-2.jpg", "full", "nga:1", 0)
    assert cat.unused_filename("a.jpg") == "a-3.jpg"


def test_published_variant_recorded_beside_published(cat):
    cat.record_publish("a--halloween-1.jpg", "full", "nga:1", 0, variant=7)
    assert cat.published_variants() == {"a--halloween-1.jpg": 7}
    assert len(cat.published()) == 1  # older readers still see 7 columns


def test_slots_replace_and_set(cat):
    cat.set_slots({"nga:1": Slot("halloween", False)})
    cat.set_slot("nga:2", "halloween")
    assert cat.slots() == {
        "nga:1": Slot("halloween", False),
        "nga:2": Slot("halloween", True),
    }
    cat.set_slots({})
    assert cat.slots() == {}
