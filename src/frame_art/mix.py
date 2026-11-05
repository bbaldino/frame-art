"""Today's mix: which live files retire and which Library items fill each slice.

A pure planner over today's schedule layers, the eligible Library items and
what's live on the TV. Slices are flattened narrowest schedule first, with Rest
of the Library last; each item counts toward the first slice it matches. The
publisher executes the plan.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass

from .library import Item, Live
from .schedules import Schedule, allocate, flatten

REST: frozenset[str] = frozenset()


def member(item: Item, slices: list[frozenset[str]]) -> int | None:
    """The first slice whose tags are all on ``item``; ``len(slices)`` is Rest.

    A version only matches a slice naming its ``ai-variant:`` tag and never
    falls to Rest.
    """
    own = f"ai-variant:{item.vtype}" if item.kind == "version" else None
    for i, tags in enumerate(slices):
        if tags <= item.tags and (own is None or own in tags):
            return i
    return None if own is not None else len(slices)


@dataclass
class Plan:
    slices: list[frozenset[str]]  # flattened, Rest last
    targets: list[int]
    retire: list[str]  # live filenames to retire now, in order
    # Per slice: live filenames rotating out, oldest first. The publisher
    # retires only as many as the slice's new items replace.
    rotate: list[list[str]]
    fill: list[list[str]]  # per slice: waiting item keys, best first


def spread(count: int, targets: list[int]) -> list[int]:
    """``count`` split in proportion to ``targets`` by largest remainder; ties go to the lower index."""
    total = sum(targets)
    if total <= 0:
        return [0] * len(targets)
    split = [divmod(count * t, total) for t in targets]
    out = [q for q, _ in split]
    by_remainder = sorted(range(len(targets)), key=lambda i: (-split[i][1], i))
    for i in by_remainder[: count - sum(out)]:
        out[i] += 1
    return out


def _group(item: Item) -> str:
    """What never-shown items interleave by: the artist, else the source."""
    for prefix in ("artist:", "source:"):
        if tags := [t for t in item.tags if t.startswith(prefix)]:
            return min(tags)
    return ""


def _round_robin(waiting: list[Item], seed: int) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in sorted(waiting, key=lambda x: x.key):
        groups[_group(item)].append(item.key)
    queues = [groups[g] for g in sorted(groups)]
    random.Random(seed).shuffle(queues)
    out: list[str] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def plan(
    layers: list[Schedule],
    items: list[Item],
    live: list[Live],
    pool: int,
    count: int,
    pinned: set[str],
    favorites: set[str],
    last_shown: dict[str, float],
    now: float,
    seed: int,
) -> Plan:
    """Plan the night's retirements and fills; ``items`` are the eligible items."""
    named = [frozenset(sl.tags) for sl in flatten(layers)]
    slices = [*named, REST]
    n = len(slices)
    where = {it.key: member(it, named) for it in items}
    by_key = {it.key: it for it in items}
    retire: list[str] = []

    # 1. Live files outside the Library, or in no slice today, go first.
    on: list[list[Live]] = [[] for _ in range(n)]
    for f in sorted(live, key=lambda f: f.published_at):
        i = where.get(f.key)
        if i is None:
            retire.append(f.filename)
        else:
            on[i].append(f)

    # 2-3. Targets: the cascade over what each slice can supply, Rest last.
    pool_size = min(pool, len({it.root for it in items}))
    roots: list[set[str]] = [set() for _ in range(n)]
    members: list[list[Item]] = [[] for _ in range(n)]
    for it in items:
        if (i := where[it.key]) is not None:
            roots[i].add(it.root)
            members[i].append(it)
    available = [len(r) for r in roots]
    targets = allocate(layers, pool_size, available[:-1])
    targets.append(min(pool_size - sum(targets), available[-1]))
    # What Rest can't take goes back to slices with works to spare, narrowest first.
    for i in range(n - 1):
        extra = min(pool_size - sum(targets), available[i] - targets[i])
        targets[i] += max(0, extra)

    # 4-5. Per slice: what leaves (the excess, then the oldest in rotation)
    # and the waiting, ranked.
    live_keys = {f.key for f in live}
    # Originals a version can replace: live as themselves, not pinned.
    swappable = {f.root: f for f in live if f.key == f.root and f.key not in pinned}
    rotate_n = spread(count, targets)
    unpinned: list[list[Live]] = []
    excess: list[int] = []
    leaving: list[int] = []  # excess + rotation, before any version swaps
    fill: list[list[str]] = []
    for i in range(n):
        here = {f.root for f in on[i]}
        waiting = [
            it for it in members[i] if it.key not in live_keys and it.root not in here
        ]
        unpinned.append([f for f in on[i] if f.key not in pinned])
        excess.append(max(0, len(on[i]) - targets[i]))
        # Rotate only as many as the waiting roots can replace beyond today's gaps.
        gone = min(excess[i], len(unpinned[i]))
        gap = max(0, targets[i] - (len(on[i]) - gone))
        r = min(rotate_n[i], max(0, len({it.root for it in waiting}) - gap))
        leaving.append(min(len(unpinned[i]), excess[i] + r))
        fill.append(_rank(waiting, pinned, favorites, last_shown, now, seed,
                          set(swappable) - here))  # fmt: skip

    # A version going up replaces its live original, so that original stands
    # in for one of its own slice's departures instead of something older.
    swapping: set[str] = set()
    for i in range(n):
        slots = targets[i] - (len(on[i]) - leaving[i])
        taken: set[str] = set()
        for key in fill[i]:
            if len(taken) >= slots:
                break
            it = by_key[key]
            if it.root in taken:
                continue
            taken.add(it.root)
            if it.kind == "version" and (f := swappable.get(it.root)) is not None:
                swapping.add(f.filename)

    # Excess retires now; rotation waits until its replacements are up.
    rotate: list[list[str]] = []
    for i in range(n):
        swaps = sum(f.filename in swapping for f in on[i])
        rest = [f.filename for f in unpinned[i] if f.filename not in swapping]
        now_n = max(0, excess[i] - swaps)
        later_n = max(0, leaving[i] - excess[i] - max(0, swaps - excess[i]))
        retire += rest[:now_n]
        rotate.append(rest[now_n : now_n + later_n])
    return Plan(slices, targets, retire, rotate, fill)


def _rank(
    waiting: list[Item],
    pinned: set[str],
    favorites: set[str],
    last_shown: dict[str, float],
    now: float,
    seed: int,
    live_roots: set[str],
) -> list[str]:
    """Pinned first, then never shown (interleaved by group), then least recently shown.

    After the pins, versions of originals live in another slice (``live_roots``)
    come first, in that same order, so a schedule starting swaps versions in
    for their originals rather than replacing other works too.
    """
    pins = sorted(it.key for it in waiting if it.key in pinned)
    rest = [it for it in waiting if it.key not in pinned]
    never = _round_robin([it for it in rest if it.key not in last_shown], seed)

    def age(key: str) -> float:
        return (now - last_shown[key]) * (2 if key in favorites else 1)

    shown = sorted(
        (it.key for it in rest if it.key in last_shown), key=lambda k: (-age(k), k)
    )
    swaps = {it.key for it in rest if it.kind == "version" and it.root in live_roots}
    ranked = never + shown
    return (
        pins + [k for k in ranked if k in swaps] + [k for k in ranked if k not in swaps]
    )
