"""The work itself: crawl, publish, compare. Shared by the CLI and the service."""

from __future__ import annotations

import datetime
import itertools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import imaging, notability, pipeline, report
from .catalogue import Catalogue
from .classify import current_season
from .net import Http
from .publish import Publisher, PublishResult, diversify
from .settings import Settings
from .sources import all_sources

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
            log.info("%s/%s: %d candidates", src.name, src.kind, len(works))

    works = cat.works()
    notability.annotate(http, works, cache)
    cat.upsert(works)
    notable = sum(1 for w in works if w.sitelinks)
    log.info("notability: %d of %d works have Wikipedia articles", notable, len(works))
    return {"crawled": counts, "catalogue": len(works)}


def last_crawl(cat: Catalogue) -> float:
    return max(
        (t for (t,) in cat.db.execute("select crawled_at from works")), default=0
    )


def publisher(cat: Catalogue, http: Http, dest: Path, s: Settings) -> Publisher:
    return Publisher(
        cat,
        http,
        dest,
        s.pad_style,
        tuple(s.kinds),
        s.prefs(),
        s.photo_share,
        s.photo_prefs(),
    )


def publish(
    cat: Catalogue,
    http: Http,
    dest: Path,
    settings: Settings,
    season: str | None = None,
    seed: int | None = None,
) -> PublishResult:
    season = season or current_season(datetime.date.today().month)
    seed = seed if seed is not None else datetime.date.today().toordinal()
    result = publisher(cat, http, dest, settings).run(
        settings.count, settings.pool, season, seed
    )
    live = len(cat.published(active=True))
    log.info(
        "publish: %d added, %d retired, %d failed; %d live in %s",
        len(result.added), len(result.retired), len(result.failed), live, dest,
    )  # fmt: skip
    return result


def nightly(
    cat: Catalogue, http: Http, data: Path, dest: Path, settings: Settings
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
    res = publish(cat, http, dest, settings)
    summary.update(
        added=res.added,
        retired=res.retired,
        failed=res.failed,
        live=len(cat.published(active=True)),
    )
    return summary


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
    order = diversify(cands, seed=0)

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
