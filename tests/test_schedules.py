import dataclasses
from datetime import date

import pytest

from frame_art.schedules import (
    Schedule,
    allocate,
    bars,
    covers,
    everyday,
    known,
    layers,
    length,
    next_start,
    order,
    shares_for,
    upcoming,
    validate,
)


def S(start, end, idx=0, **shares):
    return Schedule(f"s{idx}", f"sched {idx}", start, end, shares)


FALL = S("09-01", "12-31", fall=30)
HW = S("10-01", "10-31", halloween=50)
BDAY = S("10-28", "10-28", birthday=30)


def test_cascade_worked_examples():
    assert shares_for([FALL, HW], date(2026, 10, 15)) == {"halloween": 50, "fall": 15}
    got = shares_for([FALL, HW, BDAY], date(2026, 10, 28))
    assert got == pytest.approx({"birthday": 30, "halloween": 35, "fall": 10.5})
    assert allocate(layers([FALL, HW, BDAY], date(2026, 10, 28)), 200) == {
        "birthday": 60,
        "halloween": 70,
        "fall": 21,
    }


def test_no_schedule_is_all_originals():
    assert shares_for([HW], date(2026, 3, 1)) == {}
    assert allocate([], 150) == {}


def test_full_share_replaces_everything_below():
    full = S("10-28", "10-28", birthday=100)
    assert allocate(layers([FALL, HW, full], date(2026, 10, 28)), 150) == {
        "birthday": 150
    }


def test_shortfall_falls_to_the_next_layer():
    # Birthday wants 60 of 200 but only 10 are ready: Halloween gets 50% of 190.
    ls = layers([FALL, HW, BDAY], date(2026, 10, 28))
    assert allocate(ls, 200, {"birthday": 10, "halloween": 999, "fall": 999}) == {
        "birthday": 10,
        "halloween": 95,
        "fall": 28,
    }


def test_same_type_in_two_layers_adds_up():
    a = S("09-01", "12-31", fall=20)
    b = S("10-01", "10-31", idx=1, fall=50)
    assert shares_for([a, b], date(2026, 10, 5)) == {"fall": 60}


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


def test_unknown_types_are_ignored():
    s = S("01-01", "12-31", halloween=30, gone=40)
    assert shares_for(known([s], {"halloween"}), date(2026, 5, 1)) == {"halloween": 30}
    assert s.shares == {"halloween": 30, "gone": 40}  # the original is untouched


def test_everyday():
    e = everyday({"halloween": 75})
    assert (e.name, e.start, e.end, e.shares) == (
        "Everyday",
        "01-01",
        "12-31",
        {"halloween": 75},
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
    assert validate(dataclasses.replace(S("10-01", "10-31"), name=" "))  # needs a name
