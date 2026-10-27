"""Seasonal variants: AI-made restyles of a work (a winter or Halloween take, say).

Which version of each live work the TV holds is decided by slots: a slot gives
a work a variant type, and works without a slot show their original. The mix
says what share of the live works hold each type.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from PIL import Image

from frame_art import imaging, openrouter, pipeline
from frame_art.catalogue import Catalogue, Slot, Variant, VariantState
from frame_art.model import Work
from frame_art.net import Http
from frame_art.publish import Publisher, find_work
from frame_art.settings import Settings

log = logging.getLogger(__name__)

# Nightly runs give up on a (work, type) after this many failures.
NIGHTLY_FAILURES = 3

HALLOWEEN_PROMPT = (
    "Turn this picture into a clearly Halloween version of itself: Halloween "
    "elements, mood and light throughout, while keeping the composition and the "
    "original style and medium recognisable, as if the same artist had made a "
    "Halloween edition."
)

# Types a new install starts with.
DEFAULT_TYPES = (("halloween", "Halloween", HALLOWEEN_PROMPT),)


def seed_types(cat: Catalogue) -> None:
    """Once: add the default types, unless there are types already."""
    if cat.get("variant_types_seeded"):
        return
    if not cat.variant_types():
        for slug, name, prompt in DEFAULT_TYPES:
            cat.add_variant_type(slug, name, prompt)
            log.info("added the %s variant type", name)
    cat.put("variant_types_seeded", True)


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


def source_path(data: Path, w: Work) -> Path:
    return data / "sources" / f"{Path(pipeline.filename(w)).stem}.jpg"


def variant_path(data: Path, w: Work, type: str, n: int) -> Path:
    return data / "variants" / type / Path(pipeline.filename(w)).stem / f"{n}.jpg"


def _save(im: Image.Image, path: Path) -> None:
    # Via a temp file, so a crash never leaves a half-written cache behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    im.convert("RGB").save(tmp, "JPEG", quality=95)
    tmp.replace(path)


def load_source(http: Http, data: Path, w: Work, prefs: pipeline.Prefs) -> Image.Image:
    """The work's trimmed source image, downloaded once and kept."""
    path = source_path(data, w)
    if path.exists():
        return imaging.load(path.read_bytes())
    plan = pipeline.exclusion(w, prefs)
    if isinstance(plan, str):
        # Left out of new picks now (a setting changed, say), but it's live.
        plan = imaging.plan_for(w.width, w.height, prefs.max_crop)
    im = pipeline.load_work(http, pipeline.Candidate(w, plan, 0.0, prefs))
    _save(im, path)
    return im


def generate(
    cat: Catalogue,
    http: Http,
    data: Path,
    api_key: str,
    pub: Publisher,
    key: str,
    type: str,
    prompt: str | None = None,
) -> Variant:
    """Make a new variant of work ``key``; it becomes the current one.

    Any failure counts against the (work, type) and is raised again.
    """
    try:
        w = find_work(cat, key)
        if w is None:
            raise LookupError(f"{key} is not in the catalogue")
        if prompt is None:
            vt = next((t for t in cat.variant_types() if t.slug == type), None)
            if vt is None:
                raise LookupError(f"no variant type {type!r}")
            prompt = vt.prompt
        src = load_source(http, data, w, pub.prefs_for(w))
        edit = openrouter.edit_image(http, api_key, src, prompt)
        n = cat.next_variant_n(key, type)
        path = variant_path(data, w, type, n)
        _save(edit.image, path)
        v = cat.add_variant(
            key, type, n, prompt, openrouter.MODEL, edit.cost, str(path)
        )
    except Exception:
        cat.note_variant_failure(key, type)
        raise
    log.info("%s: made %s variant %d (cost %s)", key, type, n, edit.cost)
    return v


def apply_versions(
    cat: Catalogue,
    http: Http,
    data: Path,
    pub: Publisher,
    keys: set[str] | None = None,
) -> dict:
    """Put the version each live work's slot asks for on the TV.

    A work with a slot and a usable variant of that type shows the variant;
    every other work shows its original. A failed swap leaves the work as it is.
    """
    slots = cat.slots()
    usable = cat.usable_variants()
    showing = cat.published_variants()
    swapped: list[str] = []
    failed: list[str] = []
    for p in cat.published(active=True):
        if keys is not None and p.key not in keys:
            continue
        slot = slots.get(p.key)
        want = usable.get((p.key, slot.type)) if slot else None
        if showing.get(p.filename) == (want.id if want else None):
            continue
        try:
            if want is not None:
                im = imaging.load(Path(want.path).read_bytes())
            else:
                w = find_work(cat, p.key)
                if w is None:
                    raise LookupError(f"{p.key} is not in the catalogue")
                im = load_source(http, data, w, pub.prefs_for(w))
            swapped.append(pub.swap(p, im, want))
        except Exception as ex:  # noqa: BLE001 - one bad work must not stop the rest
            log.warning("%s: could not swap versions: %s", p.key, ex)
            failed.append(p.key)
    if swapped:
        pub._write_meta()
    return {"swapped": swapped, "failed": failed}


def _live(cat: Catalogue) -> list[str]:
    """Live work keys, oldest-published first."""
    return list(dict.fromkeys(p.key for p in cat.published(active=True)))


def nightly(
    cat: Catalogue,
    http: Http,
    data: Path,
    pub: Publisher,
    settings: Settings,
    api_key: str | None,
) -> dict:
    """The nightly variant step: reassign slots, make what's missing, swap."""
    known = {t.slug for t in cat.variant_types()}
    mix = {t: share for t, share in settings.mix.items() if t in known}
    live = _live(cat)
    slots = assign_slots(live, mix, cat.slots(), set(cat.usable_variants()))
    cat.set_slots(slots)

    todo = needed(slots, cat.variant_states(), live)[: settings.variant_limit]
    generated: list[str] = []
    gen_failed: list[str] = []
    cost = 0.0
    if todo and api_key is None:
        log.info("variants: no OpenRouter key, so no new variants tonight")
        todo = []
    for key, vtype in todo:
        try:
            v = generate(cat, http, data, api_key, pub, key, vtype)
        except Exception as ex:  # noqa: BLE001 - one bad work must not stop the rest
            log.warning(
                "%s: could not make a %s variant: %s: %s",
                key, vtype, type(ex).__name__, ex,
            )  # fmt: skip
            gen_failed.append(key)
            continue
        generated.append(key)
        cost += v.cost or 0.0

    res = apply_versions(cat, http, data, pub)
    log.info(
        "variants: %d made, %d failed, %d swapped",
        len(generated), len(gen_failed), len(res["swapped"]),
    )  # fmt: skip
    return {
        "slots": dict(Counter(s.type for s in slots.values())),
        "generated": generated,
        "gen_failed": gen_failed,
        "swapped": res["swapped"],
        "cost": round(cost, 4),
    }


def progress(cat: Catalogue, settings: Settings) -> list[dict]:
    """How far each variant type is from its share of the TV."""
    live = _live(cat)
    slots = cat.slots()
    states = cat.variant_states()
    usable = cat.usable_variants()
    todo = needed(slots, states, live)
    all_variants = cat.variants()
    type_of = {v.id: v.type for v in all_variants}
    showing = Counter(type_of.get(vid) for vid in cat.published_variants().values())
    live_files = len(cat.published(active=True))
    out = []
    for vt in cat.variant_types():
        slotted = [k for k, s in slots.items() if s.type == vt.slug]
        stuck = [
            k
            for k in slotted
            if (k, vt.slug) not in usable
            and (st := states.get((k, vt.slug))) is not None
            and st.failures >= NIGHTLY_FAILURES
        ]
        out.append(
            {
                "slug": vt.slug,
                "name": vt.name,
                "prompt": vt.prompt,
                "share": settings.mix.get(vt.slug, 0),
                "live_share": round(100 * showing[vt.slug] / live_files)
                if live_files
                else 0,
                "slotted": len(slotted),
                "ready": sum(1 for k in slotted if (k, vt.slug) in usable),
                "needed": sum(1 for _, t in todo if t == vt.slug),
                "stuck": stuck,
                "cost": round(
                    sum(v.cost or 0.0 for v in all_variants if v.type == vt.slug), 4
                ),
            }
        )
    return out
