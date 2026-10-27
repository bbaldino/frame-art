"""Seasonal variants: AI-made restyles of a work (a winter or Halloween take, say).

Which version of each live work the TV holds is decided by slots: a slot gives
a work a variant type, and works without a slot show their original. The mix
says what share of the live works hold each type.
"""

from frame_art.catalogue import Slot, VariantState

# Nightly runs give up on a (work, type) after this many failures.
NIGHTLY_FAILURES = 3


def assign_slots(
    live: list[str],
    mix: dict[str, int],
    current: dict[str, Slot],
    usable: set[tuple[str, str]],
) -> dict[str, Slot]:
    """Slots for the live works, keeping current ones where the mix allows.

    ``live`` is oldest-published first. Manual slots survive a shrinking share,
    but not their type leaving the mix.
    """
    types = [t for t, share in mix.items() if share > 0]
    target = {t: round(mix[t] * len(live) / 100) for t in types}
    age = {key: i for i, key in enumerate(live)}
    slots = {
        key: Slot(s.type, s.manual)
        for key, s in current.items()
        if key in age and s.type in target
    }

    for t in types:
        members = [k for k, s in slots.items() if s.type == t]
        # Variantless works go first, then the oldest.
        droppable = sorted(
            (k for k in members if not slots[k].manual),
            key=lambda k: ((k, t) in usable, age[k]),
        )
        for key in droppable[: max(0, len(members) - target[t])]:
            del slots[key]

    for t in types:
        missing = target[t] - sum(1 for s in slots.values() if s.type == t)
        # Works that already have a variant first, then the newest.
        free = sorted(
            (k for k in live if k not in slots),
            key=lambda k: ((k, t) not in usable, -age[k]),
        )
        for key in free[: max(0, missing)]:
            slots[key] = Slot(t, False)

    return slots


def needed(
    slots: dict[str, Slot],
    states: dict[tuple[str, str], VariantState],
    live: list[str],
) -> list[tuple[str, str]]:
    """The (key, type) pairs still to generate, newest work first."""
    out = []
    for key in reversed(live):
        slot = slots.get(key)
        if slot is None:
            continue
        state = states.get((key, slot.type))
        if state is None:
            out.append((key, slot.type))
        elif (
            state.current is None
            and not state.dont_use
            and state.failures < NIGHTLY_FAILURES
        ):
            out.append((key, slot.type))
    return out
