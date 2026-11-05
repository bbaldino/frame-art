import dataclasses
from datetime import date

import pytest

from frame_art.schedules import (
    Schedule,
    Slice,
    allocate,
    bars,
    covers,
    everyday,
    flatten,
    from_dict,
    layers,
    length,
    next_start,
    order,
    shares_for,
    upcoming,
    validate,
)


def S(start, end, idx=0, **shares):
    return Schedule(
        f"s{idx}",
        f"sched {idx}",
        start,
        end,
        [Slice([f"ai-variant:{t}"], p) for t, p in sorted(shares.items())],
    )


def sh(schedules, day):
    """shares_for as {variant: percent}."""
    out = {}
    for sl, v in shares_for(schedules, day):
        k = sl.tags[0].split(":", 1)[1]
        out[k] = out.get(k, 0) + v
    return out


def alloc(ls, n, available=None):
    """allocate as {variant: count}, one slice per variant in these tests."""
    flat = flatten(ls)
    got = allocate(ls, n, available)
    out = {}
    for sl, c in zip(flat, got):
        k = sl.tags[0].split(":", 1)[1]
        out[k] = out.get(k, 0) + c
    return {k: v for k, v in out.items() if v}


FALL = S("09-01", "12-31", fall=30)
HW = S("10-01", "10-31", halloween=50)
BDAY = S("10-28", "10-28", birthday=30)


def test_cascade_worked_examples():
    assert sh([FALL, HW], date(2026, 10, 15)) == {"halloween": 50, "fall": 15}
    got = sh([FALL, HW, BDAY], date(2026, 10, 28))
    assert got == pytest.approx({"birthday": 30, "halloween": 35, "fall": 10.5})
    assert alloc(layers([FALL, HW, BDAY], date(2026, 10, 28)), 200) == {
        "birthday": 60,
        "halloween": 70,
        "fall": 21,
    }


def test_no_schedule_is_all_originals():
    assert sh([HW], date(2026, 3, 1)) == {}
    assert allocate([], 150) == []


def test_full_share_replaces_everything_below():
    full = S("10-28", "10-28", birthday=100)
    assert alloc(layers([FALL, HW, full], date(2026, 10, 28)), 150) == {"birthday": 150}


def test_shortfall_falls_to_the_next_layer():
    # Birthday wants 60 of 200 but only 10 are ready: Halloween gets 50% of 190.
    ls = layers([FALL, HW, BDAY], date(2026, 10, 28))
    assert alloc(ls, 200, [10, 999, 999]) == {
        "birthday": 10,
        "halloween": 95,
        "fall": 28,
    }


def test_same_type_in_two_layers_adds_up():
    a = S("09-01", "12-31", fall=20)
    b = S("10-01", "10-31", idx=1, fall=50)
    assert sh([a, b], date(2026, 10, 5)) == {"fall": 60}


def test_wrap_around_new_year():
    xmas = S("12-20", "01-05", christmas=40)
    assert covers(xmas, date(2026, 12, 31)) and covers(xmas, date(2027, 1, 1))
    assert not covers(xmas, date(2027, 1, 6))
    assert not covers(xmas, date(2026, 12, 19))
    assert length(xmas) == 17
    assert bars(xmas) == [
        pytest.approx((353 / 365 * 100, 12 / 365 * 100)),
        pytest.approx((0, 5 / 365 * 100)),
    ]
    assert bars(HW) == [pytest.approx((273 / 365 * 100, 31 / 365 * 100))]


def test_ties_later_start_then_later_created():
    a = S("10-01", "10-10", idx=0, x=50)
    b = S("10-05", "10-14", idx=1, y=50)
    assert [s.id for s in layers([a, b], date(2026, 10, 7))] == ["s1", "s0"]
    c = S("10-01", "10-10", idx=2, z=50)
    assert [s.id for s in layers([a, c], date(2026, 10, 7))] == ["s2", "s0"]


def test_order_is_narrowest_first_without_a_date():
    assert [
        s.id
        for s in order([FALL, S("10-28", "10-28", idx=1), S("10-01", "10-31", idx=2)])
    ] == [
        "s1",
        "s2",
        "s0",
    ]


def test_leap_day_runs_on_feb_28_in_other_years():
    leap = S("02-29", "02-29", birthday=30)
    assert covers(leap, date(2027, 2, 28)) and covers(leap, date(2028, 2, 29))
    assert not covers(leap, date(2028, 2, 28))
    assert length(leap) == 1


def test_upcoming_within_a_week():
    xmas = S("12-01", "12-26", christmas=60)
    assert upcoming([xmas, HW], date(2026, 11, 24)) == [(date(2026, 12, 1), xmas)]
    assert upcoming([xmas], date(2026, 11, 23)) == []
    assert upcoming([xmas], date(2026, 12, 2)) == []  # running, not upcoming


def test_upcoming_across_the_new_year():
    ny = S("01-02", "01-03", x=10)
    assert upcoming([ny], date(2026, 12, 30)) == [(date(2027, 1, 2), ny)]


def test_next_start():
    assert next_start(HW, date(2026, 10, 15)) == date(2027, 10, 1)
    assert next_start(HW, date(2026, 10, 1)) == date(2026, 10, 1)
    assert next_start(HW, date(2026, 3, 1)) == date(2026, 10, 1)
    leap = S("02-29", "02-29", birthday=30)
    assert next_start(leap, date(2027, 1, 1)) == date(2027, 2, 28)
    assert next_start(leap, date(2028, 1, 1)) == date(2028, 2, 29)


def test_everyday():
    e = everyday([Slice(["ai-variant:halloween"], 75)])
    assert (e.name, e.start, e.end, e.slices) == (
        "Everyday",
        "01-01",
        "12-31",
        [Slice(["ai-variant:halloween"], 75)],
    )
    assert e.total() == 75
    assert length(e) == 365


def test_validate():
    assert validate(S("10-01", "10-31", x=50)) == []
    assert validate(S("13-01", "10-31", x=50))  # no month 13
    assert validate(S("02-30", "03-01", x=50))  # no Feb 30
    assert validate(S("10-01", "1031", x=50))  # not MM-DD
    assert validate(S("02-29", "02-29", x=50)) == []
    assert validate(S("10-01", "10-31", x=60, y=50))  # over 100
    assert validate(S("10-01", "10-31", x=-1))
    assert validate(S("10-01", "10-31", x=True))  # bool is not a share
    assert validate(dataclasses.replace(S("10-01", "10-31"), name=" "))  # needs a name


def test_old_shares_become_slices():
    s = from_dict(
        {
            "id": "x",
            "name": "H",
            "start": "10-01",
            "end": "10-31",
            "shares": {"halloween": 60, "portraits": 10},
        }
    )
    assert [(sl.tags, sl.share) for sl in s.slices] == [
        (["ai-variant:halloween"], 60),
        (["source:studio"], 10),
    ]


def test_from_dict_new_slices_round_trip():
    d = {
        "id": "x",
        "name": "H",
        "start": "10-01",
        "end": "10-31",
        "slices": [{"tags": ["kind:photograph"], "share": 20}],
    }
    assert from_dict(d).slices == [Slice(["kind:photograph"], 20)]
    assert from_dict(dataclasses.asdict(from_dict(d))) == from_dict(d)


def test_allocate_caps_by_available_and_falls_through():
    narrow = Schedule(
        "a", "Xmas", "12-01", "12-31", [Slice(["ai-variant:christmas"], 40)]
    )
    wide = everyday([Slice(["kind:photograph"], 50)])
    assert allocate([narrow, wide], 100, available=[10, 100]) == [10, 45]
    assert allocate([narrow, wide], 100) == [40, 30]


def test_flatten_is_layer_then_slice_order():
    a = Schedule(
        "a", "A", "01-01", "01-31", [Slice(["kind:x"], 1), Slice(["kind:y"], 2)]
    )
    b = Schedule("b", "B", "01-01", "12-31", [Slice(["kind:z"], 3)])
    assert [sl.tags for sl in flatten([a, b])] == [["kind:x"], ["kind:y"], ["kind:z"]]


def test_slice_tags_must_be_grouped():
    assert validate(Schedule("a", "A", "01-01", "01-31", [Slice(["christmas"], 10)]))
    assert validate(Schedule("a", "A", "01-01", "01-31", [Slice([], 10)]))
    assert validate(Schedule("a", "A", "01-01", "01-31", [Slice(["Kind:x"], 10)]))
    assert not validate(Schedule("a", "A", "01-01", "01-31", [Slice(["kind:x y"], 10)]))
