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
from dataclasses import fields
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import imaging, keys, pipeline
from .classify import current_season
from .publish import read_sidecar
from .service import JOB_KINDS, Service
from .settings import PHOTO_SOURCES, SOURCE_NAMES, SOURCES, Settings
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


def create_app(svc: Service, tv_image_url: str | None) -> Starlette:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        ago=_ago,
        when=lambda ts: _when(ts, svc.tz),
        version=svc.version,
        source_names=SOURCE_NAMES,
        thumb_url=pipeline.thumb_url,
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
            tv_image_url=tv_image_url,
            catalogue_size=len(cat.works()),
        )

    async def pool(request: Request):
        cat = svc.catalogue()
        works = {w.key: w for w in cat.works()}
        overrides = cat.overrides()
        sidecars = {f: read_sidecar(svc.dest / f) for f in ("full", "matted")}
        items = [
            {
                "p": p,
                "w": works.get(p.key),
                "on_tv": p.filename in sidecars.get(p.folder, {}),
                "state": overrides.get(p.key),
            }
            for p in reversed(cat.published(active=True))
        ]
        return render(request, "pool.html", page="pool", items=items)

    async def catalogue(request: Request):
        cat = svc.catalogue()
        q = request.query_params
        s = svc.settings()
        prefs, photo_prefs = s.prefs(), s.photo_prefs()
        shown_kinds = s.crawl_kinds()
        season = current_season(datetime.date.today().month)
        overrides = cat.overrides()
        rejected = cat.rejected()
        live = {p.key for p in cat.published(active=True)}
        text = (q.get("q") or "").strip().lower()
        source, kind, status = q.get("source"), q.get("kind"), q.get("status")

        rows = []
        counts = dict.fromkeys(STATUSES, 0)
        for w in cat.works():
            if source and w.source != source:
                continue
            if kind and w.kind != kind:
                continue
            if text and text not in f"{w.title} {w.artist or ''}".lower():
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
                }
            )
        rows.sort(key=lambda r: r["score"], reverse=True)
        pages = max(1, math.ceil(len(rows) / PAGE))
        page = min(max(1, int(q.get("page") or 1)), pages)
        args = {k: v for k, v in q.items() if k != "page" and v}
        return render(
            request,
            "catalogue.html",
            page="catalogue",
            rows=rows[(page - 1) * PAGE : page * PAGE],
            total=len(rows),
            counts=counts,
            statuses=STATUSES,
            sources=SOURCES,
            kinds=KINDS,
            q=q,
            pageno=page,
            pages=pages,
            link=lambda n: "/catalogue?" + urlencode({**args, "page": n}),
            here=str(request.url.path)
            + ("?" + request.url.query if request.url.query else ""),
        )

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

    async def set_override(request: Request):
        key = request.path_params["key"]
        form = await request.form()
        state = form.get("state")
        if state not in ("banned", "pinned", "clear"):
            return PlainTextResponse("bad state", 400)
        cat = svc.catalogue()
        if not any(w.key == key for w in cat.works()):
            return PlainTextResponse("unknown work", 404)
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

    async def healthz(request: Request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", overview),
            Route("/pool", pool),
            Route("/catalogue", catalogue),
            Route("/settings", settings_page, methods=["GET", "POST"]),
            Route("/compare", compare_page),
            Route("/compare/{name}", compare_image),
            Route("/runs", runs_page),
            Route("/jobs/{kind}", submit_job, methods=["POST"]),
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
    tv_image_url: str | None,
    ha_folder: str,
    mqtt_host: str | None,
    mqtt_port: int,
) -> None:
    svc = Service(data, dest, ha_folder, mqtt_host, mqtt_port, _version())
    svc.start()
    try:
        uvicorn.run(
            create_app(svc, tv_image_url), host=host, port=port, log_level="warning"
        )
    finally:
        svc.stop()
