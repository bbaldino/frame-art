"""The work itself: crawl, publish, compare. Shared by the CLI and the service."""

from __future__ import annotations

import datetime
import itertools
import logging
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (
    imaging,
    keys,
    library,
    notability,
    pipeline,
    report,
    schedules,
    variants,
)
from .catalogue import Catalogue
from .net import Http
from .photos import PhotoStore
from .publish import Publisher, PublishResult
from .settings import Settings
from .sources import all_sources
from .studio import StudioStore

log = logging.getLogger("frame_art")


def crawl(
    cat: Catalogue,
    http: Http,
    cache: Path,
    kinds: tuple[str, ...],
    limit: int,
    sources: list[str] | None = None,
) -> dict:
    adapters = [
        s for s in all_sources(cache, kinds) if not sources or s.name in sources
    ]

    def one(src):
        works = []
        try:
            works.extend(src.iter_works(http, limit))
        except Exception:
            # Keep whatever arrived before the failure.
            log.exception(
                "%s/%s: crawl stopped after %d works", src.name, src.kind, len(works)
            )
        return src, works

    counts = {}
    # Sources crawl in parallel; the SQLite connection stays on this thread.
    with ThreadPoolExecutor(len(adapters)) as pool:
        for src, works in pool.map(one, adapters):
            counts[f"{src.name}/{src.kind}"] = cat.upsert(works)
            cat.record_crawl(src.name)
            log.info("%s/%s: %d candidates", src.name, src.kind, len(works))

    works = cat.works()
    notability.annotate(http, works, cache)
    cat.upsert(works)
    notable = sum(1 for w in works if w.sitelinks)
    log.info("notability: %d of %d works have Wikipedia articles", notable, len(works))
    return {"crawled": counts, "catalogue": len(works)}


def last_crawl(cat: Catalogue) -> float:
    if crawls := cat.source_crawls():
        return max(crawls.values())
    return max(
        (t for (t,) in cat.db.execute("select crawled_at from works")), default=0
    )


def publisher(
    cat: Catalogue, http: Http | None, data: Path, dest: Path, s: Settings
) -> Publisher:
    db = data / "frame-art.db"
    return Publisher(
        cat,
        http,
        dest,
        s.pad_style,
        s.crop,
        PhotoStore(db, data / "photos"),
        StudioStore(db, data / "studio"),
        data,
    )


def publish(
    cat: Catalogue,
    http: Http,
    data: Path,
    dest: Path,
    settings: Settings,
    seed: int | None = None,
    today: datetime.date | None = None,
) -> PublishResult:
    today = today or datetime.date.today()
    seed = seed if seed is not None else today.toordinal()
    result = publisher(cat, http, data, dest, settings).run(
        schedules.layers(settings.schedules, today),
        settings.pool,
        settings.count,
        seed,
    )
    live = len(cat.published(active=True))
    log.info(
        "publish: %d added, %d retired, %d failed; %d live in %s",
        len(result.added), len(result.retired), len(result.failed), live, dest,
    )  # fmt: skip
    return result


def nightly(
    cat: Catalogue,
    http: Http,
    data: Path,
    dest: Path,
    settings: Settings,
    today: datetime.date,
) -> dict:
    summary = {}
    if time.time() - last_crawl(cat) > settings.recrawl_days * 86400:
        summary.update(
            crawl(
                cat,
                http,
                data / "cache",
                settings.crawl_kinds(),
                settings.crawl_limit,
                settings.sources,
            )
        )
    res = publish(cat, http, data, dest, settings, today=today)
    summary.update(
        added=res.added,
        retired=res.retired,
        failed=res.failed,
        live=len(cat.published(active=True)),
    )
    studio = StudioStore(data / "frame-art.db", data / "studio")
    summary["tries_pruned"] = studio.prune(time.time())
    # replaced in Task 8: portraits are published with everything else now.
    portraits = sum(
        1 for p in cat.published(active=True) if p.key.startswith(library.PORTRAIT)
    )
    try:
        # Edits take a minute or more each, so their client waits longer.
        v = variants.nightly(
            cat,
            Http(timeout=300),
            data,
            publisher(cat, http, data, dest, settings),
            settings,
            keys.read_key("openrouter"),
            today,
            portraits=portraits,
        )
    except Exception as ex:  # noqa: BLE001 - keep what publishing did
        log.exception("variants: the seasonal step failed")
        summary["variants"] = {"error": type(ex).__name__}
        summary["failed"] = [*summary["failed"], "variants"]
        return summary
    summary["variants"] = v
    # A failed edit or swap makes the run partial, like a failed publish.
    summary["failed"] = [*summary["failed"], *v["gen_failed"], *v["swap_failed"]]
    return summary


def _interleave(cands: list[pipeline.Candidate], seed: int) -> list[pipeline.Candidate]:
    """Order by score with some jitter, then interleave sources round-robin.

    Photos all come from a few sites where a handful of photographers supply
    most of the pictures, so they interleave by photographer instead.
    """
    rng = random.Random(seed)
    by_source: dict[str, list[tuple[float, pipeline.Candidate]]] = defaultdict(list)
    for c in cands:
        group = c.work.artist if c.work.kind == "photograph" else c.work.source
        by_source[group or ""].append((c.score + rng.uniform(0, 2), c))
    queues = [sorted(v, key=lambda t: t[0], reverse=True) for v in by_source.values()]
    rng.shuffle(queues)
    out: list[pipeline.Candidate] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0)[1])
    return out


def compare(
    cat: Catalogue,
    http: Http,
    out: Path,
    settings: Settings,
    per_band: int = 8,
) -> list[Path]:
    """Sheets of works each looser crop level would add: original, padded, cropped."""
    works = [w for w in cat.works() if w.kind in settings.kinds]
    loose = pipeline.Prefs(
        imaging.CROP_LEVELS["loose"], allow_nudes=settings.allow_nudes
    )
    cands, _ = pipeline.filter_works(works, None, loose)
    order = _interleave(cands, seed=0)

    def band_rows(lo: float, hi: float) -> list[report.Comparison]:
        rows = []
        for c in order:
            loss = imaging.crop_loss(c.work.width, c.work.height)
            if len(rows) >= per_band or not lo < loss <= hi or c.plan.mode != "crop":
                continue
            try:
                im = pipeline.load_work(http, c)
            except Exception as ex:  # noqa: BLE001 - one bad image must not stop the run
                log.warning("%s: %s", c.work.key, ex)
                continue
            crop = imaging.plan_for(*im.size, max_crop=1.0)
            if crop.scale > pipeline.MAX_UPSCALE:
                continue
            pad = imaging.plan_for(*im.size, max_crop=0.0)
            padded = imaging.render(im, pad, settings.pad_style)
            rows.append(
                report.Comparison(
                    c.work,
                    f"{c.work.source} · aspect {im.width / im.height:.2f} · "
                    f"crop cuts {100 * imaging.crop_loss(*im.size):.0f}%",
                    [
                        ("original", im),
                        (f"padded ({settings.pad_style})", padded),
                        ("cropped to full screen", imaging.render(im, crop)),
                    ],
                )
            )
            log.info("compare: %s", c.work.key)
        return rows

    out.mkdir(parents=True, exist_ok=True)
    written = []
    for (_, lo), (name, hi) in itertools.pairwise(imaging.CROP_LEVELS.items()):
        dest = out / f"compare-{name}.jpg"
        rows = band_rows(lo, hi)
        if not rows:
            log.info("compare: no works in the %s band", name)
            continue
        report.comparison_sheet(
            rows,
            dest,
            f"Crop level {name}: works it adds to full screen (crops cutting "
            f"{100 * lo:.0f}-{100 * hi:.0f}%)",
        )
        log.info("wrote %s", dest)
        written.append(dest)
    return written
