"""Mix schedules: yearly date ranges, each with a share of the screen per variant type.

Schedules that cover the same day combine "off the top": the narrowest range takes
its total share first, each wider one takes its share of what is left, and
whatever remains shows originals. Ties go to the later start in the year, then to
the schedule created later. A schedule on Feb 29 runs on Feb 28 in other years.
"""

from __future__ import annotations

import calendar
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

YEAR = 365
_MMDD = re.compile(r"^(\d\d)-(\d\d)$")


@dataclass
class Schedule:
    id: str
    name: str
    start: str  # "MM-DD", inclusive
    end: str  # "MM-DD", inclusive; before start means the range wraps the new year
    shares: dict[str, int] = field(default_factory=dict)  # percent per variant type

    def total(self) -> int:
        return sum(self.shares.values())


def _md(mmdd: str) -> tuple[int, int]:
    m = _MMDD.match(mmdd)
    if not m:
        raise ValueError(f"not MM-DD: {mmdd!r}")
    return int(m[1]), int(m[2])


def _on(mmdd: str, year: int) -> date:
    """The date ``mmdd`` falls on in ``year``; Feb 29 is Feb 28 outside leap years."""
    m, d = _md(mmdd)
    if (m, d) == (2, 29) and not calendar.isleap(year):
        d = 28
    return date(year, m, d)


def _doy(mmdd: str) -> int:
    """Day of the year in a non-leap year, from 1."""
    return _on(mmdd, 2025).timetuple().tm_yday


def covers(s: Schedule, day: date) -> bool:
    start, end, today = _on(s.start, day.year), _on(s.end, day.year), day
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end


def length(s: Schedule) -> int:
    n = _doy(s.end) - _doy(s.start) + 1
    return n if n > 0 else n + YEAR


def order(schedules: list[Schedule]) -> list[Schedule]:
    """Narrowest first; ties go to the later start, then to the later in the list."""
    keyed = sorted(
        enumerate(schedules), key=lambda p: (length(p[1]), -_doy(p[1].start), -p[0])
    )
    return [s for _, s in keyed]


def layers(schedules: list[Schedule], day: date) -> list[Schedule]:
    return [s for s in order(schedules) if covers(s, day)]


def known(schedules: list[Schedule], types: set[str]) -> list[Schedule]:
    return [
        replace(s, shares={t: p for t, p in s.shares.items() if t in types})
        for s in schedules
    ]


def allocate(
    layers: list[Schedule], n: int, ready: dict[str, int] | None = None
) -> dict[str, int]:
    """Works per type out of ``n``, cascading through ``layers`` (narrowest first).

    With ``ready``, a type gets no more works than it has versions ready, and the
    rest of that layer's share falls to the next layer down.
    """
    rem, out = n, Counter()
    for s in layers:
        base = rem
        for t, p in sorted(s.shares.items()):
            if p <= 0:
                continue
            want = min(round(p * base / 100), rem)
            if ready is not None:
                want = min(want, max(0, ready.get(t, 0) - out[t]))
            out[t] += want
            rem -= want
    return {t: k for t, k in out.items() if k}


def shares_for(schedules: list[Schedule], day: date) -> dict[str, float]:
    """Today's target percent per type: the cascade on 100, unrounded."""
    rem, out = 100.0, Counter()
    for s in layers(schedules, day):
        base = rem
        for t, p in sorted(s.shares.items()):
            if p > 0:
                out[t] += p * base / 100
                rem -= p * base / 100
    return {t: v for t, v in out.items() if v}


def next_start(s: Schedule, day: date) -> date:
    """The first day of the schedule's next run on or after ``day``."""
    first = _on(s.start, day.year)
    return first if first >= day else _on(s.start, day.year + 1)


def upcoming(
    schedules: list[Schedule], day: date, days: int = 7
) -> list[tuple[date, Schedule]]:
    """Schedules not running on ``day`` that start within the next ``days`` days."""
    out = []
    for s in schedules:
        if covers(s, day):
            continue
        first = next_start(s, day)
        if first - day <= timedelta(days=days):
            out.append((first, s))
    return out


def bars(s: Schedule) -> list[tuple[float, float]]:
    """``(left%, width%)`` pieces on a 365-day year; two when the range wraps."""
    left = _doy(s.start) - 1
    if _doy(s.end) >= _doy(s.start):
        return [(left / YEAR * 100, length(s) / YEAR * 100)]
    return [
        (left / YEAR * 100, (YEAR - left) / YEAR * 100),
        (0, _doy(s.end) / YEAR * 100),
    ]


def _date_error(label: str, mmdd: object) -> str | None:
    try:
        m, d = _md(mmdd)  # type: ignore[arg-type]
        date(2024, m, d)  # a leap year, so Feb 29 is allowed
    except (ValueError, TypeError):
        return f"{label} is not a real date"
    return None


def validate(s: Schedule) -> list[str]:
    errors = []
    if not isinstance(s.name, str) or not s.name.strip():
        errors.append("A schedule needs a name")
    for label, v in (("The start", s.start), ("The end", s.end)):
        if err := _date_error(label, v):
            errors.append(err)
    if not isinstance(s.shares, dict):
        return [*errors, "Shares must be a list of types and percentages"]
    for t, p in s.shares.items():
        if not isinstance(p, int) or isinstance(p, bool) or not 0 <= p <= 100:
            errors.append(f"The share for {t} must be a whole number from 0 to 100")
    if not errors and s.total() > 100:
        errors.append(f"The shares add up to {s.total()}%; at most 100% is allowed")
    return errors


def new_id() -> str:
    return secrets.token_hex(4)


def everyday(mix: dict[str, int]) -> Schedule:
    return Schedule(new_id(), "Everyday", "01-01", "12-31", dict(mix))
