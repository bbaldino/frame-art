"""One read model over everything the TV can show.

Museum works, Brian's own photos, saved Studio portraits and AI versions all
become an ``Item`` with a key, a title and group-qualified tags, so the mix
planner, the publisher and the web pages never care where an item came from.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from . import imaging, pipeline
from .catalogue import Catalogue
from .model import Work
from .net import Http
from .photos import PhotoStore
from .studio import StudioStore

PHOTO, PORTRAIT, VARIANT = "photo:", "portrait:", "variant:"


@dataclass(frozen=True)
class Item:
    key: str
    kind: str  # "museum" | "photo" | "portrait" | "version"
    title: str
    added_at: float
    tags: frozenset[str]  # group-qualified, lowercased
    root: str  # own key; a version's original's key
    vtype: str | None = None  # a version's type slug
    variant_id: int | None = None
    work: Work | None = None  # museum works, and versions of them (the original's)
    path: str | None = None  # file for photos, portraits and versions


@dataclass(frozen=True)
class Live:
    filename: str
    folder: str
    key: str  # mapped: "variant:<id>" for a version file
    root: str
    published_at: float


def work_tags(w: Work) -> set[str]:
    tags = {f"kind:{w.kind}", f"source:{w.source}"}
    for group, value in (
        ("artist", w.artist),
        ("style", w.style),
        ("genre", w.genre),
        ("place", w.place),
    ):
        if value:
            tags.add(f"{group}:{value}")
    tags.update(f"subject:{s}" for s in w.subjects if s)
    return {t.lower() for t in tags}


def items(cat: Catalogue, photos: PhotoStore, studio: StudioStore) -> dict[str, Item]:
    added = cat.work_added()
    mine = cat.user_tags()
    starred = cat.collection()

    def yours(key: str) -> set[str]:
        tags = {f"yours:{t}".lower() for t in mine.get(key, ())}
        if key in starred:
            tags.add("yours:favorite")
        return tags

    out: dict[str, Item] = {}
    for w in cat.works():
        out[w.key] = Item(
            w.key, "museum", w.title, added.get(w.key, 0.0),
            frozenset(work_tags(w) | yours(w.key)), w.key, work=w,
        )  # fmt: skip
    for p in photos.all():
        key = PHOTO + p.id
        tags = {"source:photos"}
        if p.kind:
            tags.add(f"kind:{p.kind}".lower())
        out[key] = Item(
            key, "photo", p.filename, p.added, frozenset(tags | yours(key)), key, path=p.path
        )  # fmt: skip
    for pt in studio.paintings():
        key = PORTRAIT + str(pt.id)
        painter = studio.painter(pt.painter)
        name = painter.name if painter else pt.painter
        tags = {"source:studio", "kind:painting", f"artist:{name}".lower()}
        out[key] = Item(
            key, "portrait", f"Portrait by {name}", pt.saved_at,
            frozenset(tags | yours(key)), key, path=pt.path,
        )  # fmt: skip
    names = {t.slug: t.name for t in cat.variant_types()}
    for v in cat.variants():
        orig = out.get(v.key)
        if orig is None or orig.kind == "version":
            continue
        key = f"{VARIANT}{v.id}"
        tags = {t for t in orig.tags if not t.startswith("yours:")}
        tags.add(f"ai-variant:{v.type}".lower())
        out[key] = Item(
            key, "version", f"{orig.title} ({names.get(v.type, v.type)})", v.created_at,
            frozenset(tags | yours(key)), orig.key, vtype=v.type, variant_id=v.id,
            work=orig.work, path=v.path,
        )  # fmt: skip
    return out


def state(lib: dict[str, tuple[str, float]], key: str) -> str:
    """ "in", "removed", or "out" (never added)."""
    return lib[key][0] if key in lib else "out"


def eligible(cat: Catalogue, all_items: dict[str, Item]) -> list[Item]:
    """What the TV may show: in the Library, not rejected, not a removed version."""
    lib = cat.library()
    rejected = cat.rejected_keys()
    out = []
    for it in all_items.values():
        if it.key in rejected:
            continue
        if it.kind == "version":
            ok = state(lib, it.key) != "removed" and state(lib, it.root) == "in"
        else:
            ok = state(lib, it.key) == "in"
        if ok:
            out.append(it)
    return out


def live(cat: Catalogue, all_items: dict[str, Item]) -> list[Live]:
    """Active published files, oldest first, version files mapped to their variant."""
    files = cat.published_variants()
    out = []
    for p in cat.published(active=True):
        key = f"{VARIANT}{files[p.filename]}" if p.filename in files else p.key
        out.append(Live(p.filename, p.folder, key, p.key, p.published_at))
    return out


def matches(item: Item, tags: Iterable[str]) -> bool:
    """True when the item has every one of ``tags``."""
    return set(tags) <= item.tags


def display_tag(tag: str) -> str:
    """ "ai-variant:christmas" reads as "ai-variant-christmas"; others as they are."""
    return tag.replace(":", "-") if tag.startswith("ai-variant:") else tag


def add(cat: Catalogue, keys: Iterable[str]) -> None:
    """Put works in the Library; a version is put back (eligible) instead."""
    keys = list(keys)
    versions = [k for k in keys if k.startswith(VARIANT)]
    cat.set_library(versions, None)
    cat.set_library([k for k in keys if not k.startswith(VARIANT)], "in")


def remove(
    cat: Catalogue,
    all_items: dict[str, Item],
    keys: Iterable[str],
    with_versions: bool = True,
) -> list[str]:
    """Mark works Removed; returns every key removed (an original's versions too)."""
    out = list(dict.fromkeys(keys))
    if with_versions:
        roots = set(out)
        out += [
            k
            for k, it in all_items.items()
            if it.kind == "version" and it.root in roots and k not in out
        ]
    cat.set_library(out, "removed")
    return out


def put_back(cat: Catalogue, keys: Iterable[str]) -> None:
    """Removed works go back in; a version's row is deleted. Versions stay as they are."""
    add(cat, keys)


def source_path(data: Path, w: Work) -> Path:
    return data / "sources" / f"{Path(pipeline.filename(w)).stem}.jpg"


def save_image(im: Image.Image, path: Path) -> None:
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
    save_image(im, path)
    return im


def source_image(item: Item, http: Http, data: Path, crop: str) -> Image.Image:
    """The full-size source for any item, ready to be fitted to the screen."""
    if item.kind == "museum":
        assert item.work is not None
        prefs = pipeline.Prefs(
            imaging.CROP_LEVELS[crop], full_screen_only=False, allow_nudes=True
        )
        return load_source(http, data, item.work, prefs)
    assert item.path is not None
    if item.kind == "photo":
        return ImageOps.exif_transpose(Image.open(item.path))  # as photos.open_image
    return imaging.load(Path(item.path).read_bytes())
