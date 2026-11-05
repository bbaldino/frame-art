"""Seasonal versions: AI-made restyles of a Library work (a winter or Halloween take, say).

A schedule slice tagged ``ai-variant:<type>`` asks for versions of that type.
They are made ahead of time, for any eligible Library work (museum works,
photos, portraits); the publisher puts them on the TV only through their slice.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from frame_art import imaging, library, pipeline, schedules
from frame_art.catalogue import Catalogue, Variant
from frame_art.library import Item, save_image
from frame_art.net import Http
from frame_art.providers import ImageProvider, OpenRouterProvider
from frame_art.schedules import Schedule
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


def variant_path(data: Path, original_key: str, type: str, n: int) -> Path:
    return data / "variants" / type / pipeline.slug(original_key) / f"{n}.jpg"


def generate(
    cat: Catalogue,
    http: Http,
    data: Path,
    provider: ImageProvider,
    original: Item,
    type: str,
    crop: str,
    prompt: str | None = None,
) -> Variant:
    """Make a new version of ``original`` (any non-version item).

    Any failure counts against the (work, type) and is raised again.
    """
    key = original.key
    try:
        if prompt is None:
            vt = next((t for t in cat.variant_types() if t.slug == type), None)
            if vt is None:
                raise LookupError(f"no variant type {type!r}")
            prompt = vt.prompt
        src = library.source_image(original, http, data, crop)
        # A work that fills the screen gets a screen-shaped version: the
        # nearest edit shape may need more crop than the crop level allows.
        fills = imaging.plan_for(*src.size, imaging.CROP_LEVELS[crop]).mode == "crop"
        edit = provider.paint(src, prompt, aspect="16:9" if fills else None)
        n = cat.next_variant_n(key, type)
        path = variant_path(data, key, type, n)
        save_image(edit.image, path)
        v = cat.add_variant(key, type, n, prompt, provider.model, edit.cost, str(path))
    except Exception:
        cat.note_variant_failure(key, type)
        raise
    log.info("%s: made %s version %d (cost %s)", key, type, n, edit.cost)
    return v


def regenerate(
    cat: Catalogue,
    http: Http,
    data: Path,
    provider: ImageProvider,
    all_items: dict[str, Item],
    variant_id: int,
    crop: str,
) -> Variant:
    """Make another version of the same work and type, then remove the old one."""
    old = cat.variant(variant_id)
    if old is None:
        raise LookupError(f"no version {variant_id}")
    original = all_items.get(old.key)
    if original is None or original.kind == "version":
        raise LookupError(f"{old.key} is not in the Library")
    new = generate(cat, http, data, provider, original, old.type, crop)
    cat.set_library([f"{library.VARIANT}{old.id}"], "removed")
    return new


def _types_of(sl: schedules.Slice) -> list[str]:
    return [
        t.removeprefix("ai-variant:") for t in sl.tags if t.startswith("ai-variant:")
    ]


def needed(
    cat: Catalogue,
    all_items: dict[str, Item],
    schedules_: list[Schedule],
    pool: int,
    days: list[date],
    type: str | None = None,
) -> list[tuple[str, str]]:
    """The (original key, type) pairs to make, narrowest schedule first.

    Days are taken in order, and within a day slices in layer order. Each
    ``ai-variant`` slice wants as many works with a version as the cascade gives
    it; candidates are eligible works without a version of the type, live ones
    first, then the newest Library additions.
    """
    elig = library.eligible(cat, all_items)
    roots = {it.root for it in elig}
    p = min(pool, len(roots))
    originals = [it for it in elig if it.kind != "version"]
    lib = cat.library()
    live = {pub.key for pub in cat.published(active=True)}
    states = cat.variant_states()
    all_versions = [it for it in all_items.values() if it.kind == "version"]
    out: dict[tuple[str, str], None] = {}
    for day in days:
        layers = schedules.layers(schedules_, day)
        wants = schedules.allocate(layers, p)
        for sl, want in zip(schedules.flatten(layers), wants, strict=True):
            for t in _types_of(sl):
                if type is not None and t != type:
                    continue
                tags = set(sl.tags)
                have = {
                    v.root
                    for v in elig
                    if v.kind == "version" and v.vtype == t and tags <= v.tags
                }
                # A removed version stops retries too; Regenerate makes a new one.
                have_any = {v.root for v in all_versions if v.vtype == t}
                # Pairs already chosen for an earlier day serve this one too.
                have |= {
                    all_items[k].root
                    for k, t2 in out
                    if t2 == t and tags <= all_items[k].tags | {f"ai-variant:{t}"}
                }
                missing = want - len(have)
                if missing <= 0:
                    continue
                cands = [
                    it
                    for it in originals
                    if it.key not in have_any
                    and tags <= it.tags | {f"ai-variant:{t}"}
                    and (states.get((it.key, t)) is None
                         or states[(it.key, t)].failures < NIGHTLY_FAILURES)
                    and (it.key, t) not in out
                ]  # fmt: skip
                cands.sort(
                    key=lambda it: (
                        it.key not in live,
                        -lib.get(it.key, ("in", 0.0))[1],
                        it.key,
                    )
                )
                for it in cands[:missing]:
                    out[(it.key, t)] = None
    return list(out)


def nightly(
    cat: Catalogue,
    http: Http,
    data: Path,
    all_items: dict[str, Item],
    settings: Settings,
    api_key: str | None,
    today: date,
) -> dict:
    """The nightly step: make the versions the schedules will need.

    For today's schedules and those of the next week, narrowest first, up to
    the nightly limit.
    """
    days = [today + timedelta(days=i) for i in range(8)]
    todo = needed(cat, all_items, settings.schedules, settings.pool, days)
    todo = todo[: settings.variant_limit]
    generated: list[str] = []
    gen_failed: list[str] = []
    cost = 0.0
    if todo and api_key is None:
        log.info("variants: no OpenRouter key, so no new versions tonight")
        todo = []
    provider = OpenRouterProvider(http, api_key) if api_key else None
    for key, vtype in todo:
        try:
            v = generate(
                cat, http, data, provider, all_items[key], vtype, settings.crop
            )
        except Exception as ex:  # noqa: BLE001 - one bad work must not stop the rest
            log.warning(
                "%s: could not make a %s version: %s: %s",
                key, vtype, type(ex).__name__, ex,
            )  # fmt: skip
            gen_failed.append(key)
            continue
        generated.append(key)
        cost += v.cost or 0.0
    log.info("variants: %d made, %d failed", len(generated), len(gen_failed))
    return {"generated": generated, "gen_failed": gen_failed, "cost": round(cost, 4)}


def make_now_count(
    cat: Catalogue,
    all_items: dict[str, Item],
    settings: Settings,
    type: str,
    today: date,
) -> int:
    """How many versions of ``type`` the next run of a schedule that uses it needs."""
    tag = f"ai-variant:{type}"
    for i in range(367):
        day = today + timedelta(days=i)
        layers = schedules.layers(settings.schedules, day)
        if any(tag in sl.tags for sl in schedules.flatten(layers)):
            return len(
                needed(cat, all_items, settings.schedules, settings.pool, [day], type)
            )
    return 0
