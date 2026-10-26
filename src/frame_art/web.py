"""LAN web UI: the pool, the catalogue, settings, comparison sheets, runs.

Server-rendered with plain HTML forms; no build step. It never talks to the TV
and holds no HA token. The "on the TV now" picture is HA's own public
thumbnail, loaded by the browser.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import time
from collections import Counter
from dataclasses import fields
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import imaging, keys, pipeline
from .classify import current_season
from .publish import find_content, read_sidecar
from .service import JOB_KINDS, TV_ACTIONS, Service
from .settings import (
    CHOOSE_FROM,
    PHOTO_SOURCES,
    SLIDESHOW,
    SOURCE_NAMES,
    SOURCES,
    Settings,
)
from .sources import ART_KINDS, KINDS

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
PAGE = 60
THUMB = (480, 270)
STATUSES = {
    "eligible": "Eligible",
    "live": "In the pool",
    "pinned": "Pinned",
    "banned": "Banned",
    "excluded": "Left out by settings",
    "rejected": "Rejected after download",
}
# The nightly automation uploads full/ only, so the TV-matte style is not offered.
UI_PAD_STYLES = {"blur": "Blurred background", "solid": "Solid colour"}
CROP_LABELS = {
    "native": "Native: only works already 16:9 (cuts at most 2%)",
    "strict": "Strict: cuts at most 13%",
    "medium": "Medium: cuts at most 21%",
    "loose": "Loose: cuts at most 27%",
}


def _version() -> str:
    try:
        return version("frame-art")
    except PackageNotFoundError:
        return "dev"


def searchable(w) -> str:
    """Everything the catalogue's search box matches against, lowercased."""
    parts = [w.title, w.artist, w.style, w.genre, w.place, w.description, *w.subjects]
    return " ".join(p for p in parts if p).lower()


TAG_PARAMS = ("tag", "subject", "style", "genre")  # the last three: older links
SUGGESTIONS = 15


def tag_pairs(w):
    """(kind, tag) for everything a work can be filtered by."""
    for kind in ("artist", "style", "genre", "place"):
        if getattr(w, kind):
            yield kind, getattr(w, kind)
    for x in w.subjects:
        yield "subject", x


def tags_of(w) -> set[str]:
    return {t.lower() for _, t in tag_pairs(w)}


def wanted_tags(q) -> list[str]:
    """The tag filters in a query string, lowercased, in order, deduplicated."""
    tags = [v.strip().lower() for k, v in q.multi_items() if k in TAG_PARAMS]
    return list(dict.fromkeys(t for t in tags if t))


def suggest_tags(works, tags: list[str], text: str) -> list[dict]:
    """Tags of the works matching ``tags``, for the catalogue's autocomplete.

    Word-start matches rank before matches mid-word, then by how many works
    carry the tag; tags already in the filter are left out.
    """
    counts: Counter = Counter()
    names: dict[str, Counter] = {}
    for w in works:
        if tags and not set(tags) <= tags_of(w):
            continue
        for key, (kind, tag) in {t.lower(): (k, t) for k, t in tag_pairs(w)}.items():
            if key in tags or (text and text not in key):
                continue
            counts[key] += 1
            names.setdefault(key, Counter())[(kind, tag)] += 1
    starts = re.compile(rf"\b{re.escape(text)}") if text else None
    ranked = sorted(
        counts,
        key=lambda k: (bool(starts) and not starts.search(k), -counts[k], k),
    )
    out = []
    for key in ranked[:SUGGESTIONS]:
        (kind, tag), _ = names[key].most_common(1)[0]
        out.append({"tag": tag, "kind": kind, "count": counts[key]})
    return out


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def _when(ts: float | None, tz) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%a %b %-d, %-I:%M %p")


def _span(seconds: float) -> str:
    """'2h 5m', '12m', 'under a minute'."""
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h < 24 else f"{h // 24}d {h % 24}h"


def create_app(svc: Service) -> Starlette:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        ago=_ago,
        card_id=lambda key: "w-" + re.sub(r"[^A-Za-z0-9_-]", "-", key),
        when=lambda ts: _when(ts, svc.tz),
        version=svc.version,
        source_names=SOURCE_NAMES,
        thumb_url=pipeline.thumb_url,
        preview_url=pipeline.preview_url,
    )
    thumbs = svc.data / "thumbs"

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    def back(request: Request, default: str = "/") -> RedirectResponse:
        # Only same-site paths, never an absolute URL from the form.
        nxt = request.query_params.get("next") or default
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = default
        return RedirectResponse(nxt, status_code=303)

    async def overview(request: Request):
        cat = svc.catalogue()
        s = svc.settings()
        state = svc.publish_state(cat)
        return render(
            request,
            "overview.html",
            page="overview",
            settings=s,
            state=state,
            next_run=svc.next_run(),
            current=svc.current,
            queued=svc.queued(),
            runs=cat.runs(8),
            job_kinds=JOB_KINDS,
            mqtt=svc.link.connected if svc.link else None,
            last_upload=cat.get("last_upload"),
            tv_actions=TV_ACTIONS,
            tv_command=cat.get("tv_command"),
            slideshow=SLIDESHOW[s.slideshow],
            now=svc.now_showing(cat),
            catalogue_size=len(cat.works()),
        )

    async def pool(request: Request):
        cat = svc.catalogue()
        works = {w.key: w for w in cat.works()}
        overrides, collection = cat.overrides(), cat.collection()
        sidecars = {f: read_sidecar(svc.dest / f) for f in ("full", "matted")}
        items = [
            {
                "p": p,
                "w": works.get(p.key),
                "on_tv": p.filename in sidecars.get(p.folder, {}),
                "state": overrides.get(p.key),
                "collected": p.key in collection,
            }
            for p in reversed(cat.published(active=True))
        ]
        return render(request, "pool.html", page="pool", items=items)

    async def catalogue(request: Request):
        cat = svc.catalogue()
        q = request.query_params
        s = svc.settings()
        prefs, photo_prefs = s.prefs(), s.photo_prefs()
        shown_kinds = s.shown_kinds()
        season = current_season(datetime.date.today().month)
        overrides = cat.overrides()
        rejected = cat.rejected()
        live = {p.key for p in cat.published(active=True)}
        text = (q.get("q") or "").strip().lower()
        source, kind, status = q.get("source"), q.get("kind"), q.get("status")
        tags = wanted_tags(q)
        collection = cat.collection()
        # /collection is the catalogue limited to Brian's picks, newest first.
        only_collection = request.url.path == "/collection"

        rows = []
        counts = dict.fromkeys(STATUSES, 0)
        works = cat.works()
        for w in works:
            if only_collection and w.key not in collection:
                continue
            if source and w.source != source:
                continue
            if kind and w.kind != kind:
                continue
            if tags and not set(tags) <= tags_of(w):
                continue
            if text and text not in searchable(w):
                continue
            plan = pipeline.exclusion(
                w, photo_prefs if w.kind == "photograph" else prefs
            )
            reason = None
            if overrides.get(w.key) == "banned":
                st = "banned"
            elif w.key in live:
                st = "live"
            elif overrides.get(w.key) == "pinned":
                st = "pinned"
            elif isinstance(plan, str):
                st, reason = "excluded", plan
            elif w.key in rejected:
                st, reason = "rejected", rejected[w.key]
            elif w.kind == "photograph" and not s.photo_share:
                st, reason = "excluded", "photography share is 0%"
            elif w.kind not in shown_kinds or w.source not in s.sources:
                st, reason = "excluded", "kind or source turned off"
            else:
                st = "eligible"
            counts[st] += 1
            if status and st != status:
                continue
            sc = pipeline.score(w, plan, season) if not isinstance(plan, str) else -99
            mode = None if isinstance(plan, str) else plan.mode
            rows.append(
                {
                    "w": w,
                    "status": st,
                    "reason": reason,
                    "score": sc,
                    "mode": mode,
                    "override": overrides.get(w.key),
                    "collected": w.key in collection,
                }
            )
        if only_collection:
            rows.sort(key=lambda r: collection[r["w"].key], reverse=True)
        else:
            rows.sort(key=lambda r: r["score"], reverse=True)
        pages = max(1, math.ceil(len(rows) / PAGE))
        page = min(max(1, int(q.get("page") or 1)), pages)
        base = [
            (k, v) for k, v in q.multi_items() if k not in ("page", *TAG_PARAMS) and v
        ]

        def url(tags: list[str], page: int = 1) -> str:
            return (
                request.url.path
                + "?"
                + urlencode(base + [("tag", t) for t in tags] + [("page", page)])
            )

        return render(
            request,
            "catalogue.html",
            page="collection" if only_collection else "catalogue",
            in_collection=len(collection),
            rows=rows[(page - 1) * PAGE : page * PAGE],
            total=len(rows),
            counts=counts,
            statuses=STATUSES,
            sources=SOURCES,
            kinds=KINDS,
            q=q,
            pageno=page,
            pages=pages,
            tags=tags,
            link=lambda n: url(tags, n),
            add_tag=lambda t: url(list(dict.fromkeys([*tags, t.lower()]))),
            drop_tag=lambda t: url([x for x in tags if x != t]),
            clear_tags=url([]),
            here=str(request.url.path)
            + ("?" + request.url.query if request.url.query else ""),
        )

    async def history_page(request: Request):
        cat = svc.catalogue()
        works = {w.key: w for w in cat.works()}
        overrides, collection = cat.overrides(), cat.collection()
        published = cat.published()
        latest = {p.key: p for p in published}  # oldest first, so the last wins
        live = {p.key for p in published if p.retired_at is None}
        todo = request.query_params.get("todo") == "1"
        resolved: dict[str, str | None] = {}
        items, day = [], None
        now = time.time()
        for r in cat.shown():
            key = r["key"]
            if key is None:
                # Uploaded after it was first seen: the sidecar knows it now.
                if r["content_id"] not in resolved:
                    p = find_content(cat, svc.dest, r["content_id"])
                    resolved[r["content_id"]] = p.key if p else None
                key = resolved[r["content_id"]]
            w = works.get(key) if key else None
            state = overrides.get(key) if key else None
            collected = key in collection
            if todo and (w is None or collected or state == "banned"):
                continue
            started = datetime.datetime.fromtimestamp(r["started_at"], svc.tz)
            label = started.strftime("%A %B %-d")
            if label != day:
                items.append({"day": label})
                day = label
            p = latest.get(key)
            items.append(
                {
                    "row": r,
                    "w": w,
                    "key": key,
                    "state": state,
                    "collected": collected,
                    # None: never published (frame-art didn't put it there).
                    "live": (key in live) if key in latest else None,
                    "image": f"/thumb/{p.folder}/{p.filename}"
                    if p and p.retired_at is None
                    else (pipeline.thumb_url(w) if w else None),
                    "full": f"/render/{p.folder}/{p.filename}"
                    if p and p.retired_at is None
                    else (pipeline.preview_url(w) if w else None),
                    "started": started.strftime("%-I:%M %p"),
                    "span": _span((r["ended_at"] or now) - r["started_at"]),
                    "current": r["ended_at"] is None,
                }
            )
        return render(request, "history.html", page="history", items=items, todo=todo)

    async def tags_json(request: Request):
        q = request.query_params
        source, kind = q.get("source"), q.get("kind")
        cat = svc.catalogue()
        collection = cat.collection() if q.get("collection") == "1" else None
        works = [
            w
            for w in cat.works()
            if (not source or w.source == source)
            and (not kind or w.kind == kind)
            and (collection is None or w.key in collection)
        ]
        text = (q.get("q") or "").strip().lower()
        return JSONResponse(suggest_tags(works, wanted_tags(q), text))

    async def settings_page(request: Request):
        errors: list[str] = []
        saved = False
        s = svc.settings()
        if request.method == "POST":
            form = await request.form()
            new = Settings()
            try:
                new.crop = form.get("crop", s.crop)
                new.pad_style = form.get("pad_style", s.pad_style)
                new.full_screen_only = form.get("full_screen_only") == "on"
                new.allow_nudes = form.get("allow_nudes") == "on"
                new.kinds = form.getlist("kinds")
                new.sources = form.getlist("sources")
                new.count = int(form.get("count", s.count))
                new.pool = int(form.get("pool", s.pool))
                new.run_at = form.get("run_at", s.run_at)
                new.paused = form.get("paused") == "on"
                new.recrawl_days = float(form.get("recrawl_days", s.recrawl_days))
                new.crawl_limit = int(form.get("crawl_limit", s.crawl_limit))
                new.photo_share = int(form.get("photo_share", s.photo_share))
                new.photo_crop = form.get("photo_crop", s.photo_crop)
                new.choose_from = form.get("choose_from", s.choose_from)
                new.repeat_days = int(form.get("repeat_days", s.repeat_days))
                new.slideshow = form.get("slideshow", s.slideshow)
            except ValueError as ex:
                errors.append(f"not a number: {ex}")
            if new.pad_style not in UI_PAD_STYLES:
                errors.append("choose a background style")
            errors += new.validate()
            if not errors:
                svc.save_settings(new)
                s, saved = new, True
            else:
                s = new
        return render(
            request,
            "settings.html",
            page="settings",
            s=s,
            errors=errors,
            saved=saved,
            crop_labels=CROP_LABELS,
            choose_from=CHOOSE_FROM,
            slideshow=SLIDESHOW,
            pad_styles=UI_PAD_STYLES,
            art_sources=[x for x in SOURCES if x not in PHOTO_SOURCES],
            photo_sources=PHOTO_SOURCES,
            kinds=ART_KINDS,
            keyed=[
                (k, keys.has_key(k.slug), keys.key_path(k.slug)) for k in keys.SERVICES
            ],
            fields=[f.name for f in fields(Settings)],
        )

    async def compare_page(request: Request):
        sheets = sorted(svc.compare_dir.glob("compare-*.jpg"))
        order = list(imaging.CROP_LEVELS)
        sheets.sort(
            key=lambda p: order.index(p.stem[8:]) if p.stem[8:] in order else 99
        )
        return render(
            request,
            "compare.html",
            page="compare",
            sheets=[(p.name, p.stem[8:], p.stat().st_mtime) for p in sheets],
            crop_labels=CROP_LABELS,
            busy=any(j.kind == "compare" for j in svc.queued())
            or (svc.current and svc.current.kind == "compare"),
        )

    async def compare_image(request: Request):
        name = request.path_params["name"]
        path = svc.compare_dir / name
        if not re.fullmatch(r"compare-[a-z]+\.jpg", name) or not path.exists():
            return PlainTextResponse("not found", 404)
        return FileResponse(path)

    async def runs_page(request: Request):
        cat = svc.catalogue()
        run_id = request.query_params.get("id")
        runs = cat.runs(50)
        selected = next((r for r in runs if str(r["id"]) == run_id), None)
        return render(request, "runs.html", page="runs", runs=runs, selected=selected)

    async def submit_job(request: Request):
        kind = request.path_params["kind"]
        if kind not in JOB_KINDS:
            return PlainTextResponse("unknown job", 404)
        svc.submit(kind)
        return back(request)

    async def tv_command(request: Request):
        action = request.path_params["action"]
        if action not in TV_ACTIONS:
            return PlainTextResponse("unknown action", 404)
        svc.tv_command(action)
        return back(request)

    async def set_override(request: Request):
        key = request.path_params["key"]
        form = await request.form()
        state = form.get("state")
        if state not in ("banned", "pinned", "clear", "collect", "uncollect"):
            return PlainTextResponse("bad state", 400)
        cat = svc.catalogue()
        if not any(w.key == key for w in cat.works()):
            return PlainTextResponse("unknown work", 404)
        if state in ("collect", "uncollect"):
            cat.collect(key, state == "collect")
        else:
            cat.set_override(key, None if state == "clear" else state)
        if state == "banned" and key in {p.key for p in cat.published(active=True)}:
            svc.submit("sync")  # take it off the TV now, not at the next run
        return back(request)

    async def thumb(request: Request):
        folder, name = request.path_params["folder"], request.path_params["name"]
        if folder not in ("full", "matted") or not re.fullmatch(r"[\w.-]+\.jpg", name):
            return PlainTextResponse("not found", 404)
        src = svc.dest / folder / name
        if not src.exists():
            return PlainTextResponse("not found", 404)
        out = thumbs / folder / name
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im.draft("RGB", THUMB)
                im = im.convert("RGB")
                im.thumbnail(THUMB, Image.LANCZOS)
                tmp = out.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=82)
                tmp.replace(out)
        return FileResponse(out, headers={"Cache-Control": "max-age=86400"})

    async def now_page(request: Request):
        return render(request, "now.html", page="now", now=svc.now_showing())

    async def now_json(request: Request):
        return JSONResponse(svc.now_showing())

    async def rendered(request: Request):
        """A published render at full size, for the Now showing page."""
        folder, name = request.path_params["folder"], request.path_params["name"]
        if folder not in ("full", "matted") or not re.fullmatch(r"[\w.-]+\.jpg", name):
            return PlainTextResponse("not found", 404)
        src = svc.dest / folder / name
        if not src.exists():
            return PlainTextResponse("not found", 404)
        return FileResponse(src, headers={"Cache-Control": "max-age=86400"})

    async def healthz(request: Request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", overview),
            Route("/now", now_page),
            Route("/now.json", now_json),
            Route("/render/{folder}/{name}", rendered),
            Route("/pool", pool),
            Route("/catalogue", catalogue),
            Route("/collection", catalogue),
            Route("/history", history_page),
            Route("/tags.json", tags_json),
            Route("/settings", settings_page, methods=["GET", "POST"]),
            Route("/compare", compare_page),
            Route("/compare/{name}", compare_image),
            Route("/runs", runs_page),
            Route("/jobs/{kind}", submit_job, methods=["POST"]),
            Route("/tv/{action}", tv_command, methods=["POST"]),
            Route("/works/{key:path}/override", set_override, methods=["POST"]),
            Route("/thumb/{folder}/{name}", thumb),
            Route("/healthz", healthz),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ],
    )


def serve(
    data: Path,
    dest: Path,
    host: str,
    port: int,
    ha_folder: str,
    mqtt_host: str | None,
    mqtt_port: int,
) -> None:
    svc = Service(data, dest, ha_folder, mqtt_host, mqtt_port, _version())
    svc.start()
    try:
        uvicorn.run(create_app(svc), host=host, port=port, log_level="warning")
    finally:
        svc.stop()
