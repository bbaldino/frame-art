from __future__ import annotations

import argparse
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import imaging, pipeline, report
from .catalogue import Catalogue
from .classify import current_season
from .net import Http
from .sources import all_sources

log = logging.getLogger("frame_art")


def cmd_crawl(args, cat: Catalogue, http: Http) -> None:
    sources = [
        s
        for s in all_sources(args.data / "cache")
        if not args.source or s.name in args.source
    ]

    def crawl(src):
        works = []
        try:
            works.extend(src.iter_works(http, args.limit))
        except Exception:
            # Keep whatever arrived before the failure.
            log.exception("%s: crawl stopped after %d works", src.name, len(works))
        return src.name, works

    # Sources crawl in parallel; the SQLite connection stays on this thread.
    with ThreadPoolExecutor(len(sources)) as pool:
        for name, works in pool.map(crawl, sources):
            log.info("%s: %d candidates", name, cat.upsert(works))


def cmd_dry_run(args, cat: Catalogue, http: Http) -> None:
    season = args.season or current_season(datetime.date.today().month)
    out = args.out
    (out / "rendered").mkdir(parents=True, exist_ok=True)

    per_source: dict[str, pipeline.FilterReport] = {}
    picks: list[pipeline.Candidate] = []
    all_cands: list[pipeline.Candidate] = []
    for src in sorted({w.source for w in cat.works()}):
        cands, rep = pipeline.filter_works(cat.works(src), season)
        per_source[src] = rep
        all_cands += cands
        cands.sort(key=lambda c: c.score, reverse=True)
        # Show both treatments: the best crops and the best pads.
        crops = [c for c in cands if c.plan.mode == "crop"]
        pads = [c for c in cands if c.plan.mode == "pad"]
        n_pad = args.per_source // 2
        chosen = crops[: args.per_source - n_pad] + pads[:n_pad]
        chosen += [c for c in cands if c not in chosen][: args.per_source - len(chosen)]
        picks += chosen

    _, overall = pipeline.filter_works([c.work for c in all_cands], season)
    log.info(
        "cross-source identity dedupe: %d of %d passed candidates are duplicates",
        overall.rejected["duplicate (same artist+title or QID)"],
        len(all_cands),
    )

    def work(c: pipeline.Candidate):
        try:
            path, ph, plan = pipeline.render(http, c, out / "rendered")
        except Exception as ex:  # noqa: BLE001 - one bad image must not stop the run
            log.warning("%s: render failed: %s", c.work.key, ex)
            return None
        return c, path, ph, plan

    rendered: list[report.Rendered] = []
    hashes: list[tuple[int, str]] = []
    dupes: list[str] = []
    with ThreadPoolExecutor(args.workers) as pool:
        for res in pool.map(work, picks):
            if res is None:
                continue
            c, path, ph, plan = res
            match = next(
                (
                    k
                    for h, k in hashes
                    if imaging.hamming(h, ph) <= pipeline.PHASH_DISTANCE
                ),
                None,
            )
            if match:
                dupes.append(f"{c.work.key} looks like {match}")
                path.unlink(missing_ok=True)
                continue
            hashes.append((ph, c.work.key))
            cat.record_render(c.work.key, ph, plan.mode, plan.scale, path)
            rendered.append(report.Rendered(c.work, path, plan.mode, plan.scale))

    report.contact_sheet(
        rendered,
        out / "contact-sheet.jpg",
        f"Frame art dry run: {len(rendered)} works, top picks per source (season: {season})",
    )
    (out / "report.md").write_text(report.markdown(per_source, rendered, dupes))
    log.info("wrote %s and %s", out / "contact-sheet.jpg", out / "report.md")


def main() -> None:
    p = argparse.ArgumentParser(prog="frame-art")
    p.add_argument(
        "--data", type=Path, default=Path("data"), help="state and cache directory"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="refresh the catalogue from sources")
    c.add_argument("--limit", type=int, default=300, help="max candidates per source")
    c.add_argument("--source", action="append", help="only these sources (repeatable)")

    d = sub.add_parser(
        "dry-run", help="filter, render a sample, write a report and contact sheet"
    )
    d.add_argument("--per-source", type=int, default=6)
    d.add_argument(
        "--workers", type=int, default=2, help="parallel renders (memory-bound)"
    )
    d.add_argument("--season", choices=["winter", "spring", "summer", "autumn"])
    d.add_argument("--out", type=Path, default=Path("out"))

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cat = Catalogue(args.data / "frame-art.db")
    http = Http()
    {"crawl": cmd_crawl, "dry-run": cmd_dry_run}[args.cmd](args, cat, http)
