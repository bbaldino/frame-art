from frame_art.catalogue import Slot, VariantState
from frame_art.variants import assign_slots, needed

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
