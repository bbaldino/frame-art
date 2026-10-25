from __future__ import annotations

import argparse
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
from .publish import Publisher, diversify
from .sources import KINDS, all_sources

log = logging.getLogger("frame_art")


def _fit(args) -> pipeline.Fit:
    return pipeline.Fit(imaging.CROP_LEVELS[args.crop], args.full_screen_only)


def _season(args) -> str:
    return args.season or current_season(datetime.date.today().month)


def cmd_crawl(args, cat: Catalogue, http: Http) -> None:
    cache = args.data / "cache"
    sources = [
        s for s in all_sources(cache, args.kinds) if not args.source or s.name in args.source
    ]

    def crawl(src):
        works = []
        try:
            works.extend(src.iter_works(http, args.limit))
        except Exception:
            # Keep whatever arrived before the failure.
            log.exception("%s/%s: crawl stopped after %d works", src.name, src.kind, len(works))
        return src, works

    # Sources crawl in parallel; the SQLite connection stays on this thread.
    with ThreadPoolExecutor(len(sources)) as pool:
        for src, works in pool.map(crawl, sources):
            log.info("%s/%s: %d candidates", src.name, src.kind, cat.upsert(works))

    works = cat.works()
    notability.annotate(http, works, cache)
    cat.upsert(works)
    log.info("notability: %d of %d works have Wikipedia articles",
             sum(1 for w in works if w.sitelinks), len(works))  # fmt: skip


def cmd_dry_run(args, cat: Catalogue, http: Http) -> None:
    season = _season(args)
    out = args.out

    per_source: dict[str, pipeline.FilterReport] = {}
    picks: list[pipeline.Candidate] = []
    works = [w for w in cat.works() if w.kind in args.kinds]
    for src in sorted({w.source for w in works}):
        cands, rep = pipeline.filter_works(
            [w for w in works if w.source == src], season, _fit(args)
        )
        per_source[src] = rep
        cands.sort(key=lambda c: c.score, reverse=True)
        # Show both treatments: the best crops and the best pads.
        crops = [c for c in cands if c.plan.mode == "crop"]
        pads = [c for c in cands if c.plan.mode == "pad"]
        n_pad = args.per_source // 2
        chosen = crops[: args.per_source - n_pad] + pads[:n_pad]
        chosen += [c for c in cands if c not in chosen][: args.per_source - len(chosen)]
        picks += chosen

    def work(c: pipeline.Candidate):
        try:
            return pipeline.render(http, c, args.pad_style)
        except Exception as ex:  # noqa: BLE001 - one bad image must not stop the run
            log.warning("%s: render failed: %s", c.work.key, ex)
            return None

    rendered: list[report.Rendered] = []
    hashes: list[tuple[int, str]] = []
    dupes: list[str] = []
    with ThreadPoolExecutor(args.workers) as pool:
        for res in pool.map(work, picks):
            if res is None:
                continue
            match = next(
                (k for h, k in hashes if imaging.hamming(h, res.phash) <= pipeline.PHASH_DISTANCE),
                None,
            )
            if match:
                dupes.append(f"{res.work.key} looks like {match}")
                continue
            hashes.append((res.phash, res.work.key))
            folder = pipeline.folder_for(res.plan, args.pad_style)
            path = out / "rendered" / folder / pipeline.filename(res.work)
            path.parent.mkdir(parents=True, exist_ok=True)
            pipeline.write_atomic(res.image, path, out / ".staging")
            cat.record_render(res.work.key, res.phash, res.plan.mode, res.plan.scale, path)
            rendered.append(report.Rendered(res.work, path, res.plan.mode, res.plan.scale))

    report.contact_sheet(
        rendered,
        out / "contact-sheet.jpg",
        f"Frame art dry run: {len(rendered)} works, top picks per source "
        f"(season: {season}, pad: {args.pad_style}, crop: {args.crop})",
    )
    (out / "report.md").write_text(report.markdown(per_source, rendered, dupes))
    log.info("wrote %s and %s", out / "contact-sheet.jpg", out / "report.md")


def cmd_compare(args, cat: Catalogue, http: Http) -> None:
    """Sheets of works each looser crop level would add: original, padded, cropped."""
    works = [w for w in cat.works() if w.kind in args.kinds]
    cands, _ = pipeline.filter_works(works, None, pipeline.Fit(imaging.CROP_LEVELS["loose"]))
    order = diversify(cands, seed=0)

    def band_rows(lo: float, hi: float) -> list[report.Comparison]:
        rows = []
        for c in order:
            loss = imaging.crop_loss(c.work.width, c.work.height)
            if len(rows) >= args.per_band or not lo < loss <= hi or c.plan.mode != "crop":
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
            rows.append(
                report.Comparison(
                    c.work,
                    f"{c.work.source} · aspect {im.width / im.height:.2f} · "
                    f"crop cuts {100 * imaging.crop_loss(*im.size):.0f}%",
                    [
                        ("original", im),
                        (f"padded ({args.pad_style})", imaging.render(im, pad, args.pad_style)),
                        ("cropped to full screen", imaging.render(im, crop)),
                    ],
                )
            )
            log.info("compare: %s", c.work.key)
        return rows

    args.out.mkdir(parents=True, exist_ok=True)
    for (_, lo), (name, hi) in itertools.pairwise(imaging.CROP_LEVELS.items()):
        dest = args.out / f"compare-{name}.jpg"
        report.comparison_sheet(
            band_rows(lo, hi),
            dest,
            f"Crop level {name}: works it adds to full screen (crops cutting "
            f"{100 * lo:.0f}-{100 * hi:.0f}%)",
        )
        log.info("wrote %s", dest)


def cmd_publish(args, cat: Catalogue, http: Http) -> None:
    seed = args.seed if args.seed is not None else datetime.date.today().toordinal()
    result = Publisher(cat, http, args.dest, args.pad_style, args.kinds, _fit(args)).run(
        args.count, args.pool, _season(args), seed
    )
    live = len(cat.published(active=True))
    log.info(
        "publish: %d added, %d retired, %d failed; %d live in %s",
        len(result.added), len(result.retired), len(result.failed), live, args.dest,
    )  # fmt: skip


def cmd_nightly(args, cat: Catalogue, http: Http) -> None:
    newest = max((t for (t,) in cat.db.execute("select crawled_at from works")), default=0)
    if time.time() - newest > args.recrawl_days * 86400:
        cmd_crawl(args, cat, http)
    cmd_publish(args, cat, http)


def main() -> None:
    p = argparse.ArgumentParser(prog="frame-art")
    p.add_argument("--data", type=Path, default=Path("data"), help="state and cache directory")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def kinds(value: str) -> tuple[str, ...]:
        out = tuple(k.strip() for k in value.split(",") if k.strip())
        bad = [k for k in out if k not in KINDS]
        if bad:
            raise argparse.ArgumentTypeError(f"unknown kind(s) {bad}; choose from {KINDS}")
        return out

    def add_crawl_args(sp):
        sp.add_argument("--limit", type=int, default=300, help="max candidates per source and kind")
        sp.add_argument("--source", action="append", help="only these sources (repeatable)")
        sp.add_argument("--kinds", type=kinds, default=("painting",),
                        help="comma-separated: painting,drawing,print (default painting)")  # fmt: skip

    def add_render_args(sp, with_kinds: bool = True):
        sp.add_argument("--pad-style", choices=imaging.PAD_STYLES, default="blur",
                        help="how non-16:9 works fill the screen")  # fmt: skip
        sp.add_argument("--crop", choices=list(imaging.CROP_LEVELS), default="strict",
                        help="how much a crop to 16:9 may cut off: native 2%%, strict 13%%, "
                             "medium 21%%, loose 27%% (default strict)")  # fmt: skip
        sp.add_argument("--full-screen-only", action="store_true",
                        help="skip works that would need a background pad")  # fmt: skip
        sp.add_argument("--season", choices=["winter", "spring", "summer", "autumn"])
        if with_kinds:
            sp.add_argument("--kinds", type=kinds, default=("painting",),
                            help="comma-separated kinds to show (default painting)")  # fmt: skip

    def add_publish_args(sp):
        sp.add_argument("--dest", type=Path, required=True, help="folder HA uploads from")
        sp.add_argument("--count", type=int, default=7, help="new works per run")
        sp.add_argument("--pool", type=int, default=150, help="works kept live on the TV")
        sp.add_argument("--seed", type=int, help="selection seed (default: today's date)")

    c = sub.add_parser("crawl", help="refresh the catalogue from sources")
    add_crawl_args(c)

    d = sub.add_parser("dry-run", help="filter, render a sample, write a report and contact sheet")
    d.add_argument("--per-source", type=int, default=6)
    d.add_argument("--workers", type=int, default=2, help="parallel renders (memory-bound)")
    d.add_argument("--out", type=Path, default=Path("out"))
    add_render_args(d)

    cmp = sub.add_parser("compare", help="side-by-side sheets of what looser crop levels add")
    cmp.add_argument("--per-band", type=int, default=8)
    cmp.add_argument("--out", type=Path, default=Path("out"))
    cmp.add_argument("--pad-style", choices=imaging.PAD_STYLES, default="blur")
    cmp.add_argument("--kinds", type=kinds, default=("painting",))

    pub = sub.add_parser("publish", help="add new works to the pool and retire the oldest")
    add_publish_args(pub)
    add_render_args(pub)

    n = sub.add_parser("nightly", help="recrawl when stale, then publish")
    add_crawl_args(n)  # its --kinds also limits what gets published
    add_publish_args(n)
    add_render_args(n, with_kinds=False)
    n.add_argument("--recrawl-days", type=float, default=7)

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cat = Catalogue(args.data / "frame-art.db")
    http = Http()
    commands = {
        "crawl": cmd_crawl,
        "dry-run": cmd_dry_run,
        "compare": cmd_compare,
        "publish": cmd_publish,
        "nightly": cmd_nightly,
    }
    commands[args.cmd](args, cat, http)
