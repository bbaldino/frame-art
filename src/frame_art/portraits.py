"""Saved Studio portraits on the TV, at the Portraits share of the schedules.

A painting goes up as ``portrait-<id>.jpg`` in the same folders HA uploads
from, under the key ``portrait:<id>``. The catalogue leaves those keys out of
its published list unless asked (see ``Catalogue.published``), so the museum
paths (the pool size, retirement, seasonal versions, the metadata index) never
see them. Only HA's to-do list, the TV-id lookup and the history read them.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from . import imaging, pipeline
from .catalogue import PORTRAIT_PREFIX, Catalogue
from .publish import Publisher
from .studio import Painting, StudioStore

log = logging.getLogger(__name__)

PREFIX = PORTRAIT_PREFIX


def is_portrait(key: str) -> bool:
    return key.startswith(PREFIX)


def key_for(painting_id: int) -> str:
    return f"{PREFIX}{painting_id}"


def target(share_pct: float, museum_live: int, saved: int) -> int:
    """How many portraits make ``share_pct`` of the screen beside the museum works."""
    if share_pct <= 0 or saved <= 0:
        return 0
    if share_pct >= 100:
        return saved
    f = share_pct / 100
    return min(saved, round(museum_live * f / (1 - f)))


def _publish(cat: Catalogue, pub: Publisher, dest: Path, p: Painting) -> str:
    """Render a painting for the panel and put it where HA uploads from."""
    im = imaging.load(Path(p.path).read_bytes())
    plan = imaging.plan_for(*im.size)
    # A painting in the photo's own shape goes up matted, never blurred out.
    style = pub.pad_style if plan.mode == "crop" else "matte"
    folder = pipeline.folder_for(plan, style)
    path = dest / folder / cat.unused_filename(f"portrait-{p.id}.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.write_atomic(imaging.render(im, plan, style), path, dest / ".staging")
    cat.record_publish(path.name, folder, key_for(p.id), imaging.phash(im))
    log.info("published portrait %s as %s/%s", p.id, folder, path.name)
    return f"{folder}/{path.name}"


def sync(
    cat: Catalogue,
    store: StudioStore,
    pub: Publisher,
    dest: Path,
    want: int,
    seed: int,
) -> dict:
    """Keep ``want`` saved paintings on the TV.

    Files of removed paintings go first, then the longest-live beyond ``want``;
    the paintings shown least recently (never, first) fill the gap.
    """
    paintings = {key_for(p.id): p for p in store.paintings()}
    history = [p for p in cat.published(portraits=True) if is_portrait(p.key)]
    live = [p for p in history if p.retired_at is None]  # oldest first
    gone = [p for p in live if p.key not in paintings]
    kept = [p for p in live if p.key in paintings]
    excess = kept[: max(0, len(kept) - want)]
    retired = pub._retire_files(gone + excess)
    showing = {p.key for p in kept[len(excess) :]}

    last_shown: dict[str, float] = {}
    for p in history:
        at = p.retired_at or p.published_at
        last_shown[p.key] = max(last_shown.get(p.key, 0.0), at)
    waiting = [k for k in paintings if k not in showing]
    random.Random(seed).shuffle(waiting)
    waiting.sort(key=lambda k: last_shown.get(k, 0.0))  # stable: seed breaks ties

    added: list[str] = []
    failed: list[str] = []
    for key in waiting:
        if len(showing) + len(added) >= want:
            break
        try:
            added.append(_publish(cat, pub, dest, paintings[key]))
        except Exception as ex:  # noqa: BLE001 - one bad file must not stop the rest
            log.warning("%s: could not publish: %s: %s", key, type(ex).__name__, ex)
            failed.append(key)
    out = {"live": len(showing) + len(added), "added": added, "retired": retired}
    if failed:
        out["failed"] = failed
    return out
