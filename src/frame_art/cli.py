from __future__ import annotations

import argparse
import datetime
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import imaging, jobs, pipeline, report
from .catalogue import Catalogue
from .classify import current_season
from .net import Http
from .settings import Settings
from .sources import KINDS

log = logging.getLogger("frame_art")


def _settings(args) -> Settings:
    """Settings from command-line flags, for one-off runs outside the service."""
    s = Settings()
    for name in (
        "crop",
        "full_screen_only",
        "allow_nudes",
        "pad_style",
        "count",
        "pool",
        "photo_share",
        "photo_crop",
    ):
        if hasattr(args, name):
            setattr(s, name, getattr(args, name))
    if getattr(args, "kinds", None):
        # Photographs come from the style mix (--photo-share), not the kinds list.
        s.kinds = [k for k in args.kinds if k != "photograph"] or ["painting"]
    if getattr(args, "source", None):
        s.sources = args.source
    if hasattr(args, "limit"):
        s.crawl_limit = args.limit
    if hasattr(args, "recrawl_days"):
        s.recrawl_days = args.recrawl_days
    return s


def cmd_crawl(args, cat: Catalogue, http: Http) -> None:
    jobs.crawl(cat, http, args.data / "cache", args.kinds, args.limit, args.source)


def cmd_dry_run(args, cat: Catalogue, http: Http) -> None:
    season = args.season or current_season(datetime.date.today().month)
    out = args.out

    per_source: dict[str, pipeline.FilterReport] = {}
    picks: list[pipeline.Candidate] = []
    works = [w for w in cat.works() if w.kind in args.kinds]
    for src in sorted({w.source for w in works}):
        cands, rep = pipeline.filter_works(
            [w for w in works if w.source == src], season, _settings(args).prefs()
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
                (
                    k
                    for h, k in hashes
                    if imaging.hamming(h, res.phash) <= pipeline.PHASH_DISTANCE
                ),
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
            cat.record_render(
                res.work.key, res.phash, res.plan.mode, res.plan.scale, path
            )
            rendered.append(
                report.Rendered(res.work, path, res.plan.mode, res.plan.scale)
            )

    report.contact_sheet(
        rendered,
        out / "contact-sheet.jpg",
        f"Frame art dry run: {len(rendered)} works, top picks per source "
        f"(season: {season}, pad: {args.pad_style}, crop: {args.crop})",
    )
    (out / "report.md").write_text(report.markdown(per_source, rendered, dupes))
    log.info("wrote %s and %s", out / "contact-sheet.jpg", out / "report.md")


def cmd_compare(args, cat: Catalogue, http: Http) -> None:
    jobs.compare(cat, http, args.out, _settings(args), args.per_band)


def cmd_publish(args, cat: Catalogue, http: Http) -> None:
    jobs.publish(cat, http, args.dest, _settings(args), args.season, args.seed)


def cmd_nightly(args, cat: Catalogue, http: Http) -> None:
    jobs.nightly(cat, http, args.data, args.dest, _settings(args))


def cmd_serve(args, cat: Catalogue, http: Http) -> None:
    from . import web

    web.serve(
        args.data,
        args.dest,
        args.host,
        args.port,
        args.tv_image_url,
        args.ha_folder,
        args.mqtt_host,
        args.mqtt_port,
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="frame-art")
    p.add_argument("--data", type=Path, default=Path(os.environ.get("FRAME_ART_DATA", "data")),
                   help="state and cache directory (env FRAME_ART_DATA)")  # fmt: skip
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def kinds(value: str) -> tuple[str, ...]:
        out = tuple(k.strip() for k in value.split(",") if k.strip())
        bad = [k for k in out if k not in KINDS]
        if bad:
            raise argparse.ArgumentTypeError(
                f"unknown kind(s) {bad}; choose from {KINDS}"
            )
        return out

    def add_crawl_args(sp):
        sp.add_argument(
            "--limit", type=int, default=300, help="max candidates per source and kind"
        )
        sp.add_argument(
            "--source", action="append", help="only these sources (repeatable)"
        )
        sp.add_argument("--kinds", type=kinds, default=("painting",),
                        help="comma-separated: painting,drawing,print,photograph (default painting)")  # fmt: skip

    def add_render_args(sp, with_kinds: bool = True):
        sp.add_argument("--pad-style", choices=imaging.PAD_STYLES, default="blur",
                        help="how non-16:9 works fill the screen")  # fmt: skip
        sp.add_argument("--crop", choices=list(imaging.CROP_LEVELS), default="strict",
                        help="how much a crop to 16:9 may cut off: native 2%%, strict 13%%, "
                             "medium 21%%, loose 27%% (default strict)")  # fmt: skip
        sp.add_argument("--full-screen-only", action="store_true",
                        help="skip works that would need a background pad")  # fmt: skip
        sp.add_argument("--allow-nudes", action="store_true",
                        help="include nudes (left out by default)")  # fmt: skip
        sp.add_argument("--season", choices=["winter", "spring", "summer", "autumn"])
        sp.add_argument("--photo-share", type=int, default=0,
                        help="percent of new works that are photographs (default 0)")  # fmt: skip
        sp.add_argument("--photo-crop", choices=list(imaging.CROP_LEVELS), default="medium",
                        help="crop level for photographs (default medium)")  # fmt: skip
        if with_kinds:
            sp.add_argument("--kinds", type=kinds, default=("painting",),
                            help="comma-separated kinds to show (default painting)")  # fmt: skip

    def add_publish_args(sp):
        sp.add_argument(
            "--dest", type=Path, required=True, help="folder HA uploads from"
        )
        sp.add_argument("--count", type=int, default=7, help="new works per run")
        sp.add_argument(
            "--pool", type=int, default=150, help="works kept live on the TV"
        )
        sp.add_argument(
            "--seed", type=int, help="selection seed (default: today's date)"
        )

    c = sub.add_parser("crawl", help="refresh the catalogue from sources")
    add_crawl_args(c)

    d = sub.add_parser(
        "dry-run", help="filter, render a sample, write a report and contact sheet"
    )
    d.add_argument("--per-source", type=int, default=6)
    d.add_argument(
        "--workers", type=int, default=2, help="parallel renders (memory-bound)"
    )
    d.add_argument("--out", type=Path, default=Path("out"))
    add_render_args(d)

    cmp = sub.add_parser(
        "compare", help="side-by-side sheets of what looser crop levels add"
    )
    cmp.add_argument("--per-band", type=int, default=8)
    cmp.add_argument("--out", type=Path, default=Path("out"))
    cmp.add_argument("--pad-style", choices=imaging.PAD_STYLES, default="blur")
    cmp.add_argument("--kinds", type=kinds, default=("painting",))
    cmp.add_argument("--allow-nudes", action="store_true")

    pub = sub.add_parser(
        "publish", help="add new works to the pool and retire the oldest"
    )
    add_publish_args(pub)
    add_render_args(pub)

    n = sub.add_parser("nightly", help="recrawl when stale, then publish")
    add_crawl_args(n)  # its --kinds also limits what gets published
    add_publish_args(n)
    add_render_args(n, with_kinds=False)
    n.add_argument("--recrawl-days", type=float, default=7)

    sv = sub.add_parser(
        "serve",
        help="web UI plus the nightly job on its own schedule (settings in --data)",
    )
    sv.add_argument("--dest", type=Path, default=os.environ.get("FRAME_ART_DEST"),
                    required="FRAME_ART_DEST" not in os.environ,
                    help="folder HA uploads from (env FRAME_ART_DEST)")  # fmt: skip
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument(
        "--port", type=int, default=int(os.environ.get("FRAME_ART_PORT", 8740))
    )
    env = os.environ.get
    sv.add_argument("--ha-folder", default=env("FRAME_ART_HA_FOLDER", "/config/www/frame_upload/frame-art"),
                    help="where HA sees --dest (env FRAME_ART_HA_FOLDER)")  # fmt: skip
    sv.add_argument("--mqtt-host", default=env("FRAME_ART_MQTT_HOST"),
                    help="broker HA listens on (env FRAME_ART_MQTT_HOST)")  # fmt: skip
    sv.add_argument(
        "--mqtt-port", type=int, default=int(env("FRAME_ART_MQTT_PORT", 1883))
    )
    sv.add_argument("--tv-image-url", default=env("FRAME_ART_TV_IMAGE_URL"),
                    help="HA's public current-art thumbnail, shown in the UI "
                         "(env FRAME_ART_TV_IMAGE_URL)")  # fmt: skip

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
        "serve": cmd_serve,
        "publish": cmd_publish,
        "nightly": cmd_nightly,
    }
    commands[args.cmd](args, cat, http)
