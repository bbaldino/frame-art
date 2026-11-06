"""LAN web UI: the TV, the Library, the schedule, Studio, settings and runs.

Server-rendered with plain HTML forms; no build step. It never talks to the TV
and holds no HA token. The "on the TV now" picture is HA's own public
thumbnail, loaded by the browser.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import io
import logging
import math
import re
import secrets
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import (
    google_photos,
    imaging,
    keys,
    library,
    mix,
    photos,
    pipeline,
    schedules,
    studio,
    variants,
)
from .net import Http
from .publish import content_index, find_content, read_sidecar
from .schedules import Schedule, Slice
from .service import ARG_JOBS, JOB_KINDS, TV_ACTIONS, Service
from .settings import (
    PHOTO_SOURCES,
    SLIDESHOW,
    SOURCE_NAMES,
    SOURCES,
)
from .sources import ART_KINDS, KINDS, info

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
PAGE = 60
THUMB = (480, 270)
# Largest photo an upload may carry, per file.
MAX_UPLOAD = 50 * 1024 * 1024
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
    """Everything a museum work's search matches against, lowercased."""
    parts = [w.title, w.artist, w.style, w.genre, w.place, w.description, *w.subjects]
    return " ".join(p for p in parts if p).lower()


# -- Library ---------------------------------------------------------------

VIEWS = {
    "in": "Library",
    "out": "Not in Library",
    "removed": "Removed",
    "favorites": "★ Favorites",
}
SORTS = {"added": "Newest first", "title": "Title"}
# Group of a tag (its prefix) -> how the tag picker heads it, in picker order.
TAG_GROUPS = {
    "artist": "Subject and style", "style": "Subject and style",
    "genre": "Subject and style", "place": "Subject and style",
    "subject": "Subject and style", "kind": "Kind", "source": "Source",
    "ai-variant": "AI variant", "yours": "Yours",
}  # fmt: skip
OWN_SOURCES = {"photos": "Your photos", "studio": "Studio"}
# Query parameters that narrow a view; one must be set to act on all of "All
# works", or to remove or put back all of anything. A view alone doesn't count:
# "Not in Library" is nearly every crawled work, and "Library" is the whole TV.
NARROWING = ("q", "source", "kind", "tag", "added_since")
LIBRARY_TAGS = 20  # suggestions per tags.json answer


def norm_tag(tag: str) -> str:
    """A filter tag as stored: lowercased, "ai-variant-x" read as "ai-variant:x"."""
    tag = tag.strip().lower()
    if ":" not in tag and tag.startswith("ai-variant-"):
        return "ai-variant:" + tag.removeprefix("ai-variant-")
    return tag


def has_tag(item: library.Item, tag: str) -> bool:
    """``tag`` is group-qualified; a bare word (typed without JavaScript) matches
    that value in any group but AI variant, so it never means a version."""
    if ":" in tag:
        return tag in item.tags
    return any(
        t.partition(":")[2] == tag for t in item.tags if not t.startswith("ai-variant:")
    )


def tag_label(tag: str) -> str:
    group, _, value = tag.partition(":")
    if group == "source":
        return OWN_SOURCES.get(value) or SOURCE_NAMES.get(value, value)
    if group == "ai-variant":
        return library.display_tag(tag)
    return value


def item_state(lib: dict[str, tuple[str, float]], it: library.Item) -> str:
    """ "in", "removed" or "out"; a version is in while its original is."""
    own = library.state(lib, it.key)
    if it.kind != "version" or own == "removed":
        return own
    return "in" if library.state(lib, it.root) == "in" else "out"


def describe(it: library.Item) -> tuple[str, str]:
    """(who, more) for an item's card: the artist line, then source and kind."""
    kind = next((t.partition(":")[2] for t in it.tags if t.startswith("kind:")), "")
    if it.kind == "museum":
        w = it.work
        who = (w.artist or "Unknown artist") + (f", {w.date}" if w.date else "")
        return who, f"{SOURCE_NAMES.get(w.source, w.source)} · {w.kind}"
    if it.kind == "photo":
        return "Your photo", kind or "not sorted yet"
    if it.kind == "portrait":
        return "Studio portrait", "Studio"
    vtype = next((t for t in it.tags if t.startswith("ai-variant:")), "")
    return "AI version", library.display_tag(vtype)


def item_text(it: library.Item) -> str:
    """What the Library's search box matches against, lowercased."""
    words = [it.title, *(t.partition(":")[2] for t in it.tags)]
    if it.work is not None:
        words.append(searchable(it.work))
    return " ".join(words).lower()


@dataclasses.dataclass
class TvContext:
    """What the TV pages read once per page, not once per card."""

    items: dict[str, library.Item]
    lib: dict[str, tuple[str, float]]
    starred: dict[str, float]
    names: dict[str, str]  # version type slug -> name
    files: dict[str, int]  # published version filename -> variant id

    def live_key(self, filename: str, key: str) -> str:
        """The item a published file shows: a version file is ``variant:<id>``."""
        if filename in self.files:
            return f"{library.VARIANT}{self.files[filename]}"
        return key


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


def create_app(svc: Service, http: Http | None = None) -> Starlette:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        ago=_ago,
        card_id=lambda key: "w-" + re.sub(r"[^A-Za-z0-9_-]", "-", key),
        when=lambda ts: _when(ts, svc.tz),
        # The local date of a timestamp, as the Library's added_since filter reads it.
        iso_day=lambda ts: (
            datetime.datetime.fromtimestamp(ts, svc.tz).date().isoformat()
        ),
        version=svc.version,
        source_names=SOURCE_NAMES,
        thumb_url=pipeline.thumb_url,
        preview_url=pipeline.preview_url,
    )
    thumbs = svc.data / "thumbs"

    def cached_thumb(src: Path, out: Path) -> Path:
        """``out`` is a thumbnail of ``src``, made now if missing or older."""
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im.draft("RGB", THUMB)
                im = im.convert("RGB")
                im.thumbnail(THUMB, Image.LANCZOS)
                tmp = out.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=82)
                tmp.replace(out)
        return out

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    def back(request: Request, default: str = "/") -> RedirectResponse:
        # Only same-site paths, never an absolute URL from the form.
        nxt = request.query_params.get("next") or default
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = default
        return RedirectResponse(nxt, status_code=303)

    # A TV request HA hasn't answered by now probably has nobody listening.
    NO_ANSWER = 120
    # art_upload_batch takes about this long per file at throttle 3 (9 in 53s).
    PER_FILE = 6

    def tv_status(cat) -> dict:
        """The TV panel: the last button press and any upload in progress."""
        now = time.time()
        cmd = cat.get("tv_command")
        unanswered = bool(
            cmd and cmd["status"] == "sent" and now - cmd["at"] > NO_ANSWER
        )
        sync = cat.get("tv_sync")
        if sync:
            elapsed = now - sync["started"]
            sync = {**sync, "elapsed": elapsed}
            if sync["phase"] == "uploading" and sync.get("uploaded") is None:
                total = sync.get("total") or 0
                # HA reports only when the whole batch is done, so this is a guess.
                sync["estimate"] = min(max(total - 1, 0), int(elapsed // PER_FILE))
            # A run HA hasn't reported on in far longer than it should take.
            limit = 300 + PER_FILE * 5 * (
                (sync.get("total") or 0) + (sync.get("delete_total") or 0)
            )
            sync["stale"] = sync["phase"] != "done" and now - sync["at"] > limit
            sync["running"] = sync["phase"] != "done" and not sync["stale"]
        # An upload request is answered as soon as HA starts the upload
        # automation, a moment before that reports in: keep watching for it.
        upload_starting = bool(
            cmd
            and cmd["action"] == "upload"
            and cmd["status"] == "done"
            and now - cmd["at"] < 30
            and not (sync and sync["started"] >= cmd["at"])
        )
        return {
            "tv_command": cmd,
            "tv_unanswered": unanswered,
            "tv_sync": sync,
            "uploads": cat.get("uploads_enabled"),
            "tv_live": bool(
                (cmd and cmd["status"] == "sent" and not unanswered)
                or (sync and sync["running"])
                or upload_starting
            ),
        }

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
            job_kinds=JOB_KINDS | ARG_JOBS,
            mqtt=svc.link.connected if svc.link else None,
            last_upload=cat.get("last_upload"),
            tv_actions=TV_ACTIONS,
            slideshow=SLIDESHOW[s.slideshow],
            now=svc.now_showing(cat),
            catalogue_size=len(cat.works()),
            **tv_status(cat),
        )

    async def tv_panel(request: Request):
        """Just the TV panel, which the overview polls while something is happening."""
        cat = svc.catalogue()
        return render(
            request,
            "_tv_status.html",
            # Not publish_state(): polling shouldn't republish to HA every 2s.
            state={"pending": bool(cat.get("ha_work"))},
            tv_actions=TV_ACTIONS,
            slideshow=SLIDESHOW[svc.settings().slideshow],
            now=svc.now_showing(cat),
            **tv_status(cat),
        )

    def tv_context(cat) -> TvContext:
        return TvContext(
            all_items(), cat.library(), cat.collection(),
            {t.slug: t.name for t in cat.variant_types()}, cat.published_variants(),
        )  # fmt: skip

    def tv_item(ctx: TvContext, key: str | None) -> dict:
        """What the TV pages say about an item, and what their buttons act on.

        Brian's photos and portraits are only ever "Your photo" and "Portrait"
        here: never a filename, a painter or a path.
        """
        it = ctx.items.get(key) if key else None
        if it is None:
            return {"it": None}
        root = ctx.items.get(it.root, it)
        return {
            "it": it,
            "w": it.work,
            "private": {"photo": "Your photo", "portrait": "Portrait"}.get(root.kind),
            "version": ctx.names.get(it.vtype, it.vtype) if it.vtype else None,
            "state": item_state(ctx.lib, it),
            "starred": it.key in ctx.starred,
        }

    async def pool(request: Request):
        cat = svc.catalogue()
        ctx, overrides = tv_context(cat), cat.overrides()
        sidecars = {f: read_sidecar(svc.dest / f) for f in ("full", "matted")}
        rows = [
            {
                "f": f,
                **tv_item(ctx, f.key),
                "on_tv": f.filename in sidecars.get(f.folder, {}),
                # Pins are kept on the original; a pin holds its live version too.
                "pinned": overrides.get(f.root) == "pinned",
            }
            for f in reversed(library.live(cat, ctx.items))
        ]
        return render(request, "pool.html", page="pool", items=rows)

    async def history_page(request: Request):
        cat = svc.catalogue()
        ctx = tv_context(cat)
        published = cat.published()
        # Oldest first, so the last wins; live rows go last, as a swapped-out
        # version shares its replacement's publish time.
        latest = {
            p.key: p for p in sorted(published, key=lambda p: p.retired_at is None)
        }
        live = {p.key for p in published if p.retired_at is None}
        todo = request.query_params.get("todo") == "1"
        by_content = content_index(cat, svc.dest)
        rows, day = [], None
        now = time.time()
        for r in cat.shown():
            key = r["key"]
            shown_file = by_content.get(r["content_id"])
            if key is None:
                # Uploaded after it was first seen: the sidecar knows it now.
                key = shown_file.key if shown_file else None
            shown = ctx.live_key(shown_file.filename, key) if shown_file else key
            about = tv_item(ctx, shown)
            if todo and (
                about["it"] is None or about["starred"] or about["state"] == "removed"
            ):
                continue
            started = datetime.datetime.fromtimestamp(r["started_at"], svc.tz)
            label = started.strftime("%A %B %-d")
            if label != day:
                rows.append({"day": label})
                day = label
            p = latest.get(key)
            on_tv = p is not None and p.retired_at is None
            pics = pictures(about["it"]) if about["it"] else (None, None)
            rows.append(
                {
                    "row": r,
                    **about,
                    # None: never published (frame-art didn't put it there).
                    "live": (key in live) if key in latest else None,
                    "image": f"/thumb/{p.folder}/{p.filename}" if on_tv else pics[0],
                    "full": f"/render/{p.folder}/{p.filename}" if on_tv else pics[1],
                    "started": started.strftime("%-I:%M %p"),
                    "span": _span((r["ended_at"] or now) - r["started_at"]),
                    "current": r["ended_at"] is None,
                }
            )
        return render(request, "history.html", page="history", items=rows, todo=todo)

    # The catalogue's old filters, as the Library's group-qualified tags.
    OLD_TAG_PARAMS = ("style", "subject", "genre")

    async def old_catalogue(request: Request):
        """/catalogue → All works; ``style=X`` and the like become ``tag=style:x``."""
        pairs = [("scope", "all")]
        for k, v in request.query_params.multi_items():
            if k in OLD_TAG_PARAMS:
                if v.strip():
                    pairs.append(("tag", f"{k}:{v.strip().lower()}"))
            else:
                pairs.append((k, v))  # a bare old tag=x stays as it is
        return RedirectResponse("/library?" + urlencode(pairs), status_code=303)

    def moved(target: str):
        """A 303 to where an old page went, keeping the query string."""

        async def handler(request: Request):
            query = request.url.query
            if not query:
                return RedirectResponse(target, status_code=303)
            joiner = "&" if "?" in target else "?"
            return RedirectResponse(f"{target}{joiner}{query}", status_code=303)

        return handler

    # -- Library ---------------------------------------------------------------

    # What the OpenRouter model charges for one 4K edit, roughly.
    VARIANT_ESTIMATE = 0.12

    def all_items() -> dict[str, library.Item]:
        return library.items(svc.catalogue(), svc.photos(), svc.studio())

    def short_date(ts: float) -> str:
        d = datetime.datetime.fromtimestamp(ts, svc.tz)
        same_year = d.year == datetime.datetime.now(svc.tz).year
        return d.strftime("%-d %b" if same_year else "%-d %b %Y")

    def lib_filter(q) -> dict:
        """The Library page's filter, read from a query string."""
        scope = "all" if q.get("scope") == "all" else "library"
        view = q.get("view") if q.get("view") in VIEWS else None
        try:
            since = datetime.date.fromisoformat(q.get("added_since") or "")
        except ValueError:
            since = None
        return {
            "scope": scope,
            "view": view or ("in" if scope == "library" else None),
            "text": (q.get("q") or "").strip().lower(),
            "source": q.get("source") or None,
            "kind": q.get("kind") or None,
            "tags": list(dict.fromkeys(norm_tag(v) for v in q.getlist("tag") if v.strip())),
            "sort": q.get("sort") if q.get("sort") in SORTS else "added",
            "since": since,
            "versions": q.get("versions") == "1",
            "full_screen": q.get("full_screen") == "1",
        }  # fmt: skip

    def narrowed(q, emptying: bool = False) -> bool:
        """Whether an "all" action is a choice: "All works" needs a filter, and
        so does removing or putting back everything a view shows."""
        filtered = any(q.get(k) for k in NARROWING)
        return filtered or (not emptying and q.get("scope") != "all")

    def lib_rows(cat, items: dict[str, library.Item], f: dict) -> list[dict]:
        """The items ``f`` matches, sorted, each version after its original."""
        s = svc.settings()
        lib, starred, rejected = cat.library(), cat.collection(), cat.rejected()
        live = {x.key for x in library.live(cat, items)}
        max_crop = imaging.CROP_LEVELS[s.crop]
        strict = pipeline.Prefs(max_crop, f["full_screen"])
        versions = f["versions"] or any(t.startswith("ai-variant:") for t in f["tags"])
        counts = Counter(it.root for it in items.values() if it.kind == "version")
        rows = []
        for it in items.values():
            if it.kind == "version" and not versions:
                continue
            st = item_state(lib, it)
            view = f["view"]
            if view in ("in", "out", "removed") and st != view:
                continue
            if view == "favorites" and it.key not in starred:
                continue
            if f["source"] and f"source:{f['source']}" not in it.tags:
                continue
            if f["kind"] and f"kind:{f['kind']}" not in it.tags:
                continue
            if not all(has_tag(it, t) for t in f["tags"]):
                continue
            if f["text"] and f["text"] not in item_text(it):
                continue
            # The old full-screen-only setting, as the publisher used to apply it.
            if it.work is not None and f["full_screen"]:
                why = pipeline.exclusion(it.work, strict)
                if why in (
                    "not full screen at this crop level",
                    "too tall or too wide",
                ):
                    continue
            if f["scope"] == "library" and st == "in" and it.key in lib:
                at, label = lib[it.key][1], "in Library since"
            else:
                at, label = it.added_at, "added"
            if (
                f["since"]
                and datetime.datetime.fromtimestamp(at, svc.tz).date() < f["since"]
            ):
                continue
            warning = None
            if it.kind == "museum":
                why = pipeline.exclusion(it.work, s.prefs())
                if isinstance(why, str):
                    warning = why
            rows.append(
                {"it": it, "state": st, "at": at, "about": describe(it), "date": f"{label} {short_date(at)}",
                 "starred": it.key in starred, "on_tv": it.key in live,
                 "rejected": rejected.get(it.key), "warning": warning,
                 "versions": counts.get(it.key, 0)}
            )  # fmt: skip
        if f["sort"] == "title":
            rows.sort(key=lambda r: (r["it"].title.lower(), r["it"].key))
        else:
            rows.sort(key=lambda r: (-r["at"], r["it"].key))
        # Versions follow their original when it is shown too.
        shown = {r["it"].key for r in rows}
        after: dict[str, list[dict]] = {}
        for r in rows:
            if r["it"].kind == "version" and r["it"].root in shown:
                after.setdefault(r["it"].root, []).append(r)
        out = []
        for r in rows:
            if r["it"].kind == "version" and r["it"].root in shown:
                continue
            out.append(r)
            out += after.get(r["it"].key, [])
        return out

    def pictures(it: library.Item) -> tuple[str | None, str | None]:
        """(thumbnail, larger picture) URLs for an item's card."""
        if it.kind == "photo":
            src = f"/photos/{it.key.removeprefix(library.PHOTO)}/thumb"
            return src, src
        if it.kind == "portrait":
            pid = it.key.removeprefix(library.PORTRAIT)
            return f"/portraits/{pid}/thumb", f"/portraits/{pid}.jpg"
        if it.kind == "version":
            return (
                f"/variant-file/{it.variant_id}?thumb=1",
                f"/variant-file/{it.variant_id}",
            )
        # Works' pictures come through work_image, not straight from the
        # museum: Wikimedia rate-limits a page of hotlinked thumbnails, and the
        # server can pace its requests, cache the result and say what failed.
        if not pipeline.thumb_url(it.work):
            return None, None
        key = quote(it.key, safe=":")
        return f"/work-image/thumb/{key}", f"/work-image/preview/{key}"

    def here_of(request: Request) -> str:
        return request.url.path + ("?" + request.url.query if request.url.query else "")

    async def library_page(request: Request):
        cat, q = svc.catalogue(), request.query_params
        f = lib_filter(q)
        items = all_items()
        rows = lib_rows(cat, items, f)
        pages = max(1, math.ceil(len(rows) / PAGE))
        try:
            page = min(max(1, int(q.get("page") or 1)), pages)
        except ValueError:
            page = 1
        base = [(k, v) for k, v in q.multi_items() if k not in ("page", "tag") and v]

        def url(tags: list[str], page: int = 1, **over) -> str:
            pairs = [(k, v) for k, v in base if k not in over]
            pairs += [(k, v) for k, v in over.items() if v]
            pairs += [("tag", t) for t in tags]
            if page > 1:
                pairs.append(("page", page))
            return "/library" + ("?" + urlencode(pairs) if pairs else "")

        tags = f["tags"]
        # Bulk forms post the filter itself, so "all" means every page of it.
        filter_qs = urlencode([(k, v) for k, v in q.multi_items() if k != "page" and v])
        s = svc.settings()
        in_lib = {it.root for it in library.eligible(cat, items)}
        return render(
            request,
            "library.html",
            page="library",
            f=f,
            q=q,
            views=VIEWS,
            sorts=SORTS,
            rows=rows[(page - 1) * PAGE : page * PAGE],
            total=len(rows),
            pictures=pictures,
            pageno=page,
            pages=pages,
            link=lambda n: url(tags, n),
            view_url=lambda scope, view: (
                "/library?"
                + urlencode([("scope", scope)] + ([("view", view)] if view else []))
            ),
            drop_tag=lambda t: url([x for x in tags if x != t]),
            add_tag=lambda t: url(list(dict.fromkeys([*tags, t]))),
            clear_tags=url([]),
            tag_label=tag_label,
            tag_group=lambda t: TAG_GROUPS.get(t.partition(":")[0], ""),
            here=here_of(request),
            filter_qs=filter_qs,
            bulk_ok=narrowed(q),
            bulk_filtered=narrowed(q, emptying=True),
            sources=[*SOURCES, *OWN_SOURCES],
            source_label=lambda x: OWN_SOURCES.get(x) or SOURCE_NAMES.get(x, x),
            kinds=[*KINDS, *photos.KINDS],
            types=cat.variant_types(),
            library_size=len(in_lib),
            pool=s.pool,
            estimate=VARIANT_ESTIMATE,
        )

    async def library_tags_json(request: Request):
        """Tag suggestions for the picker, grouped, from the works the filter leaves."""
        q = request.query_params
        chosen = [norm_tag(v) for v in q.getlist("tag") if v.strip()]
        source, kind = q.get("source"), q.get("kind")
        text = (q.get("q") or "").strip().lower()
        counts: Counter = Counter()
        for it in all_items().values():
            if source and f"source:{source}" not in it.tags:
                continue
            if kind and f"kind:{kind}" not in it.tags:
                continue
            if not all(has_tag(it, t) for t in chosen):
                continue
            for t in it.tags:
                if t in chosen or t.partition(":")[0] not in TAG_GROUPS:
                    continue
                if (
                    text
                    and text not in t.partition(":")[2]
                    and text not in tag_label(t).lower()
                ):
                    continue
                counts[t] += 1
        starts = re.compile(rf"\b{re.escape(text)}") if text else None
        ranked = sorted(
            counts,
            key=lambda t: (bool(starts) and not starts.search(tag_label(t).lower()),
                           -counts[t], t),
        )  # fmt: skip
        return JSONResponse(
            [
                {
                    "tag": t,
                    "label": tag_label(t),
                    "group": TAG_GROUPS[t.partition(":")[0]],
                    "count": counts[t],
                }
                for t in ranked[:LIBRARY_TAGS]
            ]  # fmt: skip
        )

    def eligible_live(cat, items) -> set[str]:
        """Live keys the TV may keep showing."""
        ok = {it.key for it in library.eligible(cat, items)}
        return {x.key for x in library.live(cat, items)} & ok

    def sync_if_dropped(cat, before: set[str], items=None) -> None:
        """Take works off the TV now, not at the next run, when one stopped being eligible."""
        if before - eligible_live(cat, items if items is not None else all_items()):
            svc.submit("sync")

    def chosen(
        request: Request, form, cat, items, emptying: bool
    ) -> list[str] | PlainTextResponse:
        """The keys a Library action is for: the ticked ones, or the whole filter."""
        if form.get("all") == "1":
            q = request.query_params
            if not narrowed(q, emptying):
                return PlainTextResponse(
                    "Choose a filter first: that would act on every work.", 400
                )
            return [r["it"].key for r in lib_rows(cat, items, lib_filter(q))]
        keys = [k for k in form.getlist("key") if k in items]
        if not keys:
            return PlainTextResponse("no works chosen", 400)
        return keys

    def library_action(fn, emptying: bool = False):
        """Wrap ``fn(cat, items, keys, form)`` as a Library POST: pick the keys,
        act, sync when something live dropped out, then go back."""

        async def handler(request: Request):
            cat, form, items = svc.catalogue(), await request.form(), all_items()
            keys = chosen(request, form, cat, items, emptying)
            if isinstance(keys, PlainTextResponse):
                return keys
            before = eligible_live(cat, items)
            resp = fn(request, cat, items, keys, form)
            if resp is not None:
                return resp
            sync_if_dropped(cat, before, items)
            return back(request, "/library")

        return handler

    def checked(form, name: str, default: bool = True) -> bool:
        """A checkbox sent with a hidden "0" before it; missing means ``default``."""
        values = form.getlist(name)
        return values[-1] == "1" if values else default

    def do_add(request, cat, items, keys, form):
        library.add(cat, keys)

    def do_remove(request, cat, items, keys, form):
        library.remove(cat, items, keys, with_versions=checked(form, "with_versions"))

    def do_put_back(request, cat, items, keys, form):
        library.put_back(cat, keys)

    def do_star(request, cat, items, keys, form):
        for k in keys:
            cat.collect(k, form.get("on", "1") == "1")

    def do_tag(request, cat, items, keys, form):
        tag = (form.get("tag") or "").strip().lower().removeprefix("yours:").strip()
        if not tag:
            return PlainTextResponse("type a tag", 400)
        cat.set_tag(keys, tag, form.get("on", "1") == "1")

    def do_make_versions(request, cat, items, keys, form):
        vt = next((t for t in cat.variant_types() if t.slug == form.get("type")), None)
        if vt is None:
            return PlainTextResponse("unknown type", 404)
        originals = sorted(k for k in keys if items[k].kind != "version")
        if not originals:
            return PlainTextResponse("versions are made of originals only", 400)
        if form.get("confirm") != "1":
            return render(
                request, "library_confirm.html", page="library", type=vt,
                count=len(originals), cost=len(originals) * VARIANT_ESTIMATE,
                action=here_of(request), fields=[
                    (k, v) for k, v in form.multi_items() if k in ("key", "all", "type")
                ],
                estimate=VARIANT_ESTIMATE, cancel=back(request, "/library").headers["location"],
            )  # fmt: skip
        svc.submit("generate", {"keys": originals, "type": vt.slug})

    def version_action(fn):
        async def handler(request: Request):
            cat = svc.catalogue()
            v = cat.variant(request.path_params["id"])
            if v is None:
                return PlainTextResponse("unknown version", 404)
            items = all_items()
            before = eligible_live(cat, items)
            fn(cat, items, f"{library.VARIANT}{v.id}", v)
            sync_if_dropped(cat, before, items)
            return back(request, f"/works/{library.VARIANT}{v.id}")

        return handler

    def version_remove(cat, items, key, v):
        library.remove(cat, items, [key], with_versions=False)

    def version_put_back(cat, items, key, v):
        library.put_back(cat, [key])

    def version_regenerate(cat, items, key, v):
        svc.submit("regenerate", {"variant": v.id})

    async def delete_item(request: Request):
        """Delete a portrait, or a version's file (its row stays, Removed)."""
        key = request.path_params["key"]
        cat, items = svc.catalogue(), all_items()
        it = items.get(key)
        if it is None:
            return PlainTextResponse("unknown work", 404)
        before = eligible_live(cat, items)
        if it.kind == "portrait":
            pid = int(key.removeprefix(library.PORTRAIT))
            svc.studio().remove_painting(pid)
            cat.set_library([key], None)
            (thumbs / "paintings" / f"{pid}.jpg").unlink(missing_ok=True)
            sync_if_dropped(cat, before)
            return back(request, "/library")
        if it.kind == "version":
            Path(it.path).unlink(missing_ok=True)
            (thumbs / "variants" / f"{it.variant_id}.jpg").unlink(missing_ok=True)
            cat.set_library([key], "removed")
            sync_if_dropped(cat, before, items)
            return back(request, f"/works/{key}")
        return PlainTextResponse("only portraits and versions can be deleted", 400)

    async def settings_page(request: Request):
        errors: list[str] = []
        saved = False
        s = svc.settings()
        if request.method == "POST":
            form = await request.form()
            # Schedules and the versions limit aren't on this form (they live
            # on /schedule and /studio/versions), so they carry over untouched.
            new = dataclasses.replace(s)
            try:
                new.crop = form.get("crop", s.crop)
                new.pad_style = form.get("pad_style", s.pad_style)
                new.kinds = form.getlist("kinds")
                new.sources = form.getlist("sources")
                new.count = int(form.get("count", s.count))
                new.pool = int(form.get("pool", s.pool))
                new.run_at = form.get("run_at", s.run_at)
                new.paused = form.get("paused") == "on"
                new.recrawl_days = float(form.get("recrawl_days", s.recrawl_days))
                new.crawl_limit = int(form.get("crawl_limit", s.crawl_limit))
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
            slideshow=SLIDESHOW,
            pad_styles=UI_PAD_STYLES,
            art_sources=[x for x in SOURCES if x not in PHOTO_SOURCES],
            photo_sources=PHOTO_SOURCES,
            kinds=ART_KINDS,
            keyed=[
                (k, keys.has_key(k.slug), keys.key_path(k.slug)) for k in keys.SERVICES
            ],
        )

    MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
    # Days per month, for the timeline's month columns.
    MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    # A bar narrower than this (percent of the year) puts its label beside it.
    NARROW_BAR = 9

    def slice_label(tags: list[str], names: dict[str, str]) -> str:
        """A slice's tags in words: versions by their style's name."""
        parts = []
        for t in tags:
            group, _, value = t.partition(":")
            parts.append(
                f"{names.get(value, value)} versions"
                if group == "ai-variant"
                else tag_label(t)
            )
        return " + ".join(parts)

    def see_them(tags: list[str]) -> str:
        """The Library view that holds exactly what a slice can draw on."""
        pairs = [("tag", t) for t in tags]
        if any(t.startswith("ai-variant:") for t in tags):
            pairs.append(("versions", "1"))
        return "/library?" + urlencode(pairs)

    def day_of(mmdd: str) -> str:
        m, _, d = mmdd.partition("-")
        return f"{MONTHS[int(m) - 1]} {int(d)}"

    def schedule_view(request: Request, errors=(), notice="", status=200, draft=None):
        cat, s, today = svc.catalogue(), svc.settings(), svc.today()
        items = all_items()
        elig = library.eligible(cat, items)
        pool = min(s.pool, len({it.root for it in elig}))
        sch = s.schedules
        names = {t.slug: t.name for t in cat.variant_types()}
        # One colour per slice, by where it sits in the settings.
        color = {
            (k.id, j): n % 6
            for n, (k, j) in enumerate(
                (k, j) for k in sch for j in range(len(k.slices))
            )
        }
        sched_color = {k.id: i % 6 for i, k in enumerate(sch)}

        # Per schedule, how each slice does on the day it runs (or next runs).
        days: dict[datetime.date, tuple] = {}

        def cascade(day: datetime.date):
            if day not in days:
                ls = schedules.layers(sch, day)
                named = [frozenset(sl.tags) for sl in schedules.flatten(ls)]
                roots: list[set[str]] = [set() for _ in named]
                for it in elig:
                    i = mix.member(it, named)
                    if i is not None and i < len(named):
                        roots[i].add(it.root)
                days[day] = (ls, roots, schedules.allocate(ls, pool))
            return days[day]

        def slice_rows(k: Schedule) -> list[dict]:
            day = (
                today if schedules.covers(k, today) else schedules.next_start(k, today)
            )
            ls, roots, wants = cascade(day)
            first = sum(
                len(x.slices)
                for x in ls[: next(i for i, x in enumerate(ls) if x.id == k.id)]
            )
            out = []
            for j, sl in enumerate(k.slices):
                n, m = len(roots[first + j]), wants[first + j]
                ai = any(t.startswith("ai-variant:") for t in sl.tags)
                out.append(
                    {"label": slice_label(sl.tags, names), "tags": ", ".join(sl.tags),
                     "share": sl.share, "n": n, "m": m, "ai": ai,
                     "short": n < m, "to_make": max(0, m - n) * VARIANT_ESTIMATE,
                     "color": color[(k.id, j)], "see": see_them(sl.tags)}
                )  # fmt: skip
            return out

        # The timeline: widest at the top, so the schedule that wins is lowest.
        timeline = []
        for k in reversed(schedules.order(sch)):
            pieces = schedules.bars(k)
            widest = max(range(len(pieces)), key=lambda i: pieces[i][1])
            left, width = pieces[widest]
            timeline.append(
                {"id": k.id, "name": k.name, "label": f"{k.name} · {k.total()}%", "pieces": pieces,
                 "label_at": widest if width >= NARROW_BAR else None,
                 "side_left": left + width if left + width < 70 else None,
                 "side_right": 100 - left if left + width >= 70 else None,
                 "color": sched_color[k.id], "dates": f"{day_of(k.start)} – {day_of(k.end)}",
                 "running": schedules.covers(k, today), "slices": slice_rows(k)}
            )  # fmt: skip
        start_of_year = datetime.date(today.year, 1, 1)
        today_pct = min((today - start_of_year).days, 364) / 365 * 100

        # Today on the TV: live items per slice of today's layers, Rest last.
        ls = schedules.layers(sch, today)
        flat = [(k, j, sl) for k in ls for j, sl in enumerate(k.slices)]
        named = [frozenset(sl.tags) for _, _, sl in flat]
        counts = [0] * (len(named) + 1)
        outside = 0
        for f in library.live(cat, items):
            it = items.get(f.key)
            i = mix.member(it, named) if it else None
            if i is None:
                outside += 1
            else:
                counts[i] += 1
        strip = [
            {
                "name": slice_label(sl.tags, names),
                "n": counts[i],
                "color": color[(k.id, j)],
            }
            for i, (k, j, sl) in enumerate(flat)
        ]
        strip.append({"name": "Rest of the Library", "n": counts[-1], "color": "x"})
        total_live = sum(x["n"] for x in strip)
        for x in strip:
            x["pct"] = round(100 * x["n"] / total_live, 1) if total_live else 0

        ed = None
        want = draft[0].id if draft else request.query_params.get("edit")
        if draft is not None:
            ed = schedule_editor(draft[0], draft[1], not draft[0].id)
        elif want == "new":
            mmdd = today.strftime("%m-%d")
            ed = schedule_editor(Schedule("", "", mmdd, mmdd, []), None, True)
        elif saved := next((k for k in sch if k.id == want), None):
            ed = schedule_editor(saved, None, False)

        layer_names = " + ".join(k.name for k in reversed(ls)) or "nothing scheduled"
        resp = render(
            request,
            "schedule.html",
            page="schedule",
            timeline=timeline,
            today=today,
            today_label=f"{day_of(today.strftime('%m-%d'))} · {layer_names} · pool of {s.pool}",
            today_pct=today_pct,
            strip=strip,
            outside=outside,
            total_live=total_live,
            library_size=len({it.root for it in elig}),
            pool=s.pool,
            months=MONTHS,
            month_days=MONTH_DAYS,
            ed=ed,
            editing=want,
            tag_hints=sorted(
                {
                    t
                    for it in items.values()
                    for t in it.tags
                    if t.partition(":")[0] in HINT_GROUPS
                }
            ),
            errors=list(errors),
            notice=notice,
        )
        resp.status_code = status
        return resp

    HINT_GROUPS = ("kind", "source", "ai-variant", "yours")

    def schedule_editor(s: Schedule, rows: list[dict] | None, is_new: bool) -> dict:
        """The editor's fields: the slices (or what was typed) plus one blank row."""
        start_m, _, start_d = s.start.partition("-")
        end_m, _, end_d = s.end.partition("-")
        if rows is None:
            rows = [{"tags": ", ".join(sl.tags), "share": sl.share} for sl in s.slices]
        return {
            "id": s.id,
            "new": is_new,
            "name": s.name,
            "start_month": start_m.lstrip("0"),
            "start_day": start_d.lstrip("0"),
            "end_month": end_m.lstrip("0"),
            "end_day": end_d.lstrip("0"),
            "rows": [*rows, {"tags": "", "share": ""}],
        }

    def schedule_from_form(form, sid: str) -> tuple[Schedule, list[dict], list[str]]:
        """The schedule a form describes, the rows as typed, and what is wrong."""
        errors = []

        def mmdd(whole: str, month: str, day: str) -> str:
            if one := (form.get(whole) or "").strip():
                return one
            m = (form.get(month) or "").strip()
            d = (form.get(day) or "").strip()
            if m.isdecimal() and d.isdecimal():
                return f"{int(m):02d}-{int(d):02d}"
            return f"{m}-{d}"  # fails validation, and shows as typed

        rows, slices = [], []
        at = {
            int(m[1])
            for k in form
            if (m := re.fullmatch(r"slice_(?:tags|share)_(\d+)", k))
        }
        for i in sorted(at):
            tags_raw = (form.get(f"slice_tags_{i}") or "").strip()
            share_raw = (form.get(f"slice_share_{i}") or "").strip()
            if not tags_raw and not share_raw:
                continue  # the blank row
            rows.append({"tags": tags_raw, "share": share_raw})
            tags = [norm_tag(x) for x in tags_raw.split(",") if x.strip()]
            try:
                slices.append(Slice(tags, int(share_raw)))
            except ValueError:
                errors.append(
                    f"{tags_raw or 'A slice'}: {share_raw!r} is not a whole number"
                )
        s = Schedule(
            sid,
            (form.get("name") or "").strip(),
            mmdd("start", "start_month", "start_day"),
            mmdd("end", "end_month", "end_day"),
            slices,
        )
        return s, rows, errors or schedules.validate(s)

    async def schedule_page(request: Request):
        return schedule_view(request)

    async def add_schedule(request: Request):
        form = await request.form()
        s, rows, errors = schedule_from_form(form, "")
        if not errors:
            current = svc.settings()
            added = dataclasses.replace(s, id=schedules.new_id())
            new = dataclasses.replace(current, schedules=[*current.schedules, added])
            errors = new.validate()
        if errors:
            # The draft keeps no id, so the form still posts as a new schedule.
            return schedule_view(request, errors, status=400, draft=(s, rows))
        svc.save_settings(new)
        return schedule_view(
            request, notice=f"Saved {s.name}. Changes apply from tonight's run."
        )

    async def save_schedule(request: Request):
        sid = request.path_params["id"]
        current = svc.settings()
        if not any(k.id == sid for k in current.schedules):
            return PlainTextResponse("unknown schedule", 404)
        form = await request.form()
        s, rows, errors = schedule_from_form(form, sid)
        if not errors:
            new = dataclasses.replace(
                current, schedules=[s if k.id == sid else k for k in current.schedules]
            )
            errors = new.validate()
        if errors:
            return schedule_view(request, errors, status=400, draft=(s, rows))
        svc.save_settings(new)
        return schedule_view(
            request, notice=f"Saved {s.name}. Changes apply from tonight's run."
        )

    async def delete_schedule(request: Request):
        sid = request.path_params["id"]
        current = svc.settings()
        gone = next((k for k in current.schedules if k.id == sid), None)
        if gone is None:
            return PlainTextResponse("unknown schedule", 404)
        rest = [k for k in current.schedules if k.id != sid]
        svc.save_settings(dataclasses.replace(current, schedules=rest))
        return schedule_view(
            request, notice=f"Deleted {gone.name}. Changes apply from tonight's run."
        )

    # -- Your photos: private, never on a public page --------------------------

    google_token = svc.google_token
    # How long a started Google sign-in may wait for its pasted address.
    SIGNIN_TTL = 15 * 60

    def photos_busy() -> bool:
        """An import or kind suggestion is queued or running (the page reloads)."""
        jobs_ = [*svc.queued(), svc.current]
        return any(j and j.kind in ("import-google", "classify-photos") for j in jobs_)

    def photos_view(
        request: Request, notice="", errors=(), status=200, google_expired=False
    ):
        resp = render(
            request,
            "photos.html",
            page="photos",
            photos=svc.photos().all(),
            counts=svc.studio().counts(),
            label=lambda s: info(s).label,
            notice=notice,
            errors=list(errors),
            google_client=google_photos.has_client(),
            google_signed_in=google_photos.signed_in(google_token),
            google_expired=google_expired,
            pick=svc.catalogue().get("google_pick"),
            adding=photos_busy(),
        )
        resp.status_code = status
        return resp

    # Google calls block, so the handlers that make them are plain functions,
    # which Starlette runs in its thread pool. Two quick reloads must not both
    # queue the import: only one load checks a pending pick at a time.
    pick_check = threading.Lock()

    def photos_page(request: Request):
        # A pending Google pick is checked once per load; the page reloads itself.
        cat, notice, expired = svc.catalogue(), "", False
        if not pick_check.acquire(blocking=False):
            return photos_view(request)
        try:
            pick, client = cat.get("google_pick"), google_photos.load_client()
            notice, expired = check_pick(cat, pick, client)
        finally:
            pick_check.release()
        return photos_view(request, notice, google_expired=expired)

    def check_pick(cat, pick, client) -> tuple[str, bool]:
        """(notice, sign-in ended) after looking at the pending pick once."""
        notice, expired = "", False
        if pick and client is None:
            cat.put("google_pick", None)
        elif pick:
            http = Http(timeout=30)
            try:
                token = google_photos.access_token(http, client, google_token)
                if time.time() > pick["expires"]:
                    google_photos.delete_session(http, token, pick["id"])
                    cat.put("google_pick", None)
                    notice = "The Google Photos pick timed out; nothing was added."
                elif google_photos.session_done(http, token, pick["id"]):
                    svc.submit("import-google", {"session": pick["id"]})
                    cat.put("google_pick", None)
            except google_photos.SignInNeeded:
                cat.put("google_pick", None)
                expired = True
            except ConnectionError as e:
                notice = f"{e}; trying again in a moment."
        return notice, expired

    async def upload_photos(request: Request):
        form = await request.form()
        store, added, already, errors = svc.photos(), 0, 0, []
        for f in form.getlist("files"):
            if not getattr(f, "filename", None):
                continue
            data = await f.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                errors.append(f"{f.filename}: too large")
                continue
            try:
                # Keyed by content: phones reuse names like IMG_0001.jpg.
                sha = hashlib.sha256(data).hexdigest()
                p = store.add("upload", sha, data, f.filename)
            except ValueError:
                errors.append(f"{f.filename}: not a photo")
                continue
            if p is None:
                already += 1
            else:
                added += 1
        if added:
            svc.submit("classify-photos")
        notice = f"Added {added}."
        if already:
            notice += f" {already} already here."
        return photos_view(request, notice, errors)

    async def photo_thumb(request: Request):
        p = svc.photos().get(request.path_params["id"])
        if p is None or not Path(p.path).is_file():
            return PlainTextResponse("not found", 404)
        out = thumbs / "photos" / f"{p.id}.jpg"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            with photos.open_image(p) as im:
                im = im.convert("RGB")
                im.thumbnail((480, 480), Image.LANCZOS)
                tmp = out.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=85)
                tmp.replace(out)
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})  # fmt: skip

    async def set_photo_kind(request: Request):
        store, pid = svc.photos(), request.path_params["id"]
        p = store.get(pid)
        if p is None:
            return PlainTextResponse("unknown photo", 404)
        kind = (await request.form()).get("kind")
        if kind not in photos.KINDS:
            return photos_view(request, errors=["choose portrait or scene"], status=400)
        store.set_kind(pid, kind, None, False)
        return photos_view(request, notice=f"{p.filename}: {kind}.")

    def google_signin_view(request: Request, error="", status=200):
        client = google_photos.load_client()
        if client is None:
            return photos_view(
                request, errors=["Set up Google Photos first."], status=400
            )
        verifier, challenge = google_photos.new_pkce()
        state = secrets.token_urlsafe(16)
        svc.catalogue().put("google_signin",
                            {"verifier": verifier, "state": state, "at": time.time()})  # fmt: skip
        resp = render(request, "google_signin.html", page="photos", error=error,
                      url=google_photos.auth_url(client, state, challenge))  # fmt: skip
        resp.status_code = status
        return resp

    async def google_signin(request: Request):
        if request.method == "GET":
            return google_signin_view(request)
        client = google_photos.load_client()
        if client is None:
            return photos_view(
                request, errors=["Set up Google Photos first."], status=400
            )
        pasted = str((await request.form()).get("pasted") or "")
        cat = svc.catalogue()
        started = cat.get("google_signin")
        cat.put("google_signin", None)  # one try per sign-in link
        if not started or time.time() - started["at"] > SIGNIN_TTL:
            return google_signin_view(
                request,
                "That sign-in took too long. Start again with this new link.",
                400,
            )
        try:
            await run_in_threadpool(google_photos.exchange, Http(timeout=30), client, pasted,
                                   started["verifier"], started["state"], google_token)  # fmt: skip
        except ValueError as e:
            msg = ("That sign-in didn't match. Start again with this new link."
                   if "didn't match" in str(e) else f"{e}. Start again with this new link.")  # fmt: skip
            return google_signin_view(request, msg, 400)
        return photos_view(request, notice="Signed in to Google Photos.")

    def google_pick(request: Request):
        client, cat = google_photos.load_client(), svc.catalogue()
        if client is None:
            return photos_view(
                request, errors=["Set up Google Photos first."], status=400
            )
        if not cat.get("google_pick"):
            http = Http(timeout=30)
            try:
                token = google_photos.access_token(http, client, google_token)
                s = google_photos.create_session(http, token)
            except google_photos.SignInNeeded:
                return photos_view(request, google_expired=True)
            except ConnectionError as e:
                return photos_view(request, errors=[str(e)], status=502)
            cat.put(
                "google_pick", {"id": s.id, "uri": s.picker_uri, "expires": s.expires}
            )
        return RedirectResponse("/photos", status_code=303)

    def google_cancel(request: Request):
        client, cat = google_photos.load_client(), svc.catalogue()
        pick = cat.get("google_pick")
        cat.put("google_pick", None)
        if pick and client:
            http = Http(timeout=30)
            try:
                token = google_photos.access_token(http, client, google_token)
                google_photos.delete_session(http, token, pick["id"])
            except (google_photos.SignInNeeded, ConnectionError):
                pass  # Google drops an abandoned session by itself
        return photos_view(request, notice="Cancelled; nothing was added.")

    async def google_signout(request: Request):
        google_token.unlink(missing_ok=True)
        return photos_view(request, notice="Signed out of Google Photos.")

    # -- Studio ------------------------------------------------------------------

    # A try still "painting" this long after the model was called was lost
    # (a restart mid-paint, say).
    STUCK = 15 * 60

    DONE = {"saved": "Saved to the Library.", "deleted": "Try deleted."}

    def waiting_tries() -> set[int]:
        """Tries whose paint job is still queued or running."""
        return {j.args["try"] for j in [*svc.queued(), svc.current]
                if j and j.kind == "paint"}  # fmt: skip

    def is_stuck(t, waiting: set[int]) -> bool:
        """A try that will never finish: its job is gone, or it hung painting."""
        if t.status == "queued":
            return t.id not in waiting  # the queue is lost on a restart
        started = t.started_at or t.made_at
        return t.status == "painting" and time.time() - started > STUCK

    async def studio_index(request: Request):
        return render(request, "studio_index.html", page="studio",
                      photos=svc.photos().all())  # fmt: skip

    def last_choices(photo, tries, painters) -> dict:
        """The form as the last try left it, or the defaults for this photo."""
        chosen = {"painter": painters[0].slug if painters else "", "painter_name": "",
                  "setting": "reinvent" if photo.kind == "scene" else "keep",
                  "shape": "fill", "nudges": [], "nudge_text": ""}  # fmt: skip
        if tries:
            t = tries[0]
            by_name = {p.name: p.slug for p in painters}
            chosen.update(
                painter=by_name.get(t.painter, ""),
                painter_name="" if t.painter in by_name else t.painter,
                setting=t.setting,
                shape=t.shape,
                nudges=[n for n in t.nudges if n in studio.NUDGES],
                nudge_text="; ".join(n for n in t.nudges if n not in studio.NUDGES),
            )
        return chosen

    def studio_view(
        request: Request, photo, notice="", errors=(), status=200, form=None
    ):
        waiting = waiting_tries()

        def stuck(t) -> bool:
            return is_stuck(t, waiting)

        st = svc.studio()
        tries = st.tries(photo.id)
        painters = studio.suggested(st.painters(), photo.kind)
        chosen = last_choices(photo, tries, painters)
        notice = notice or DONE.get(request.query_params.get("done", ""), "")
        if form is not None:
            name = (form.get("painter_name") or "").strip()
            chosen.update(
                painter="" if name else form.get("painter") or "",
                painter_name=name,
                nudges=[n for n in form.getlist("nudge") if n in studio.NUDGES],
                nudge_text=(form.get("nudge_text") or "").strip(),
            )
            chosen.update({k: form[k] for k in ("setting", "shape") if form.get(k)})
        from_try = None
        raw = request.query_params.get("from")
        if raw and raw.isdecimal():
            from_try = next((t for t in tries if t.id == int(raw) and t.path), None)
        resp = render(
            request,
            "studio.html",
            page="studio",
            photo=photo,
            painters=painters,
            chosen=chosen,
            nudges=studio.NUDGES,
            tries=tries,
            from_try=from_try,
            spent=st.spent(photo.id),
            saved=st.saved_try_ids(),
            paintings=st.paintings(photo.id),
            painting=any(
                t.status in ("queued", "painting") and not stuck(t) for t in tries
            ),  # fmt: skip
            stuck=stuck,
            notice=notice,
            errors=list(errors),
        )
        resp.status_code = status
        return resp

    async def studio_page(request: Request):
        p = svc.photos().get(request.path_params["photo_id"])
        if p is None:
            return PlainTextResponse("unknown photo", 404)
        return studio_view(request, p)

    async def studio_paint(request: Request):
        p = svc.photos().get(request.path_params["photo_id"])
        if p is None:
            return PlainTextResponse("unknown photo", 404)
        st, form = svc.studio(), await request.form()
        nudges = [n for n in form.getlist("nudge") if n in studio.NUDGES]
        if text := (form.get("nudge_text") or "").strip():
            nudges.append(text)
        raw = form.get("from_try") or ""
        from_try = st.get_try(int(raw)) if raw.isdecimal() else None
        if from_try is not None:
            if from_try.photo_id != p.id or from_try.path is None:
                return studio_view(request, p, errors=["That try can't be used."],
                                   status=400, form=form)  # fmt: skip
            painter = studio.Painter("", from_try.painter, "", ())
            setting, shape = from_try.setting, from_try.shape
            if not nudges:
                return studio_view(request, p, errors=["Say what to change."],
                                   status=400, form=form)  # fmt: skip
        else:
            name = (form.get("painter_name") or "").strip()
            painter = (studio.Painter("", name, "", ()) if name
                       else st.painter(form.get("painter") or ""))  # fmt: skip
            setting, shape = form.get("setting"), form.get("shape")
            if (
                painter is None
                or setting not in studio.SETTINGS
                or shape not in studio.SHAPES
            ):
                return studio_view(request, p, errors=["Choose a painter, setting and shape."],
                                   status=400, form=form)  # fmt: skip
        t = st.start_try(p.id, painter, setting, shape, nudges,
                         from_try.id if from_try else None)  # fmt: skip
        svc.submit("paint", {"try": t.id})
        return RedirectResponse(f"/studio/{p.id}", status_code=303)

    async def try_image(request: Request):
        t = svc.studio().get_try(request.path_params["id"])
        if t is None or not t.path or not Path(t.path).is_file():
            return PlainTextResponse("not found", 404)
        return FileResponse(t.path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})  # fmt: skip

    async def try_thumb(request: Request):
        t = svc.studio().get_try(request.path_params["id"])
        if t is None or not t.path or not Path(t.path).is_file():
            return PlainTextResponse("not found", 404)
        return thumb_of(Path(t.path), thumbs / "tries" / f"{t.id}.jpg")

    def thumb_of(src: Path, out: Path) -> FileResponse:
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((960, 960), Image.LANCZOS)
                tmp = out.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=85)
                tmp.replace(out)
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})  # fmt: skip

    async def save_try(request: Request):
        st = svc.studio()
        t = st.get_try(request.path_params["id"])
        if t is None:
            return PlainTextResponse("unknown try", 404)
        try:
            painting = st.save(t.id)
        except ValueError as e:
            return PlainTextResponse(str(e), 400)
        # A portrait Brian saves is one he wants on the TV, but a repeated save
        # (a resubmitted form) never puts back one he has removed since.
        key, cat = f"{library.PORTRAIT}{painting.id}", svc.catalogue()
        if key not in cat.library():
            cat.set_library([key], "in")
        return RedirectResponse(f"/studio/{t.photo_id}?done=saved", status_code=303)

    async def delete_try(request: Request):
        st = svc.studio()
        t = st.get_try(request.path_params["id"])
        if t is None:
            return PlainTextResponse("unknown try", 404)
        if t.status == "painting" and not is_stuck(t, waiting_tries()):
            return PlainTextResponse("that try is still painting", 400)
        try:
            st.delete_try(t.id)
        except ValueError as e:
            return PlainTextResponse(str(e), 400)
        (thumbs / "tries" / f"{t.id}.jpg").unlink(missing_ok=True)
        return RedirectResponse(f"/studio/{t.photo_id}?done=deleted", status_code=303)

    # -- Portraits: browsed in the Library; these serve the pictures -----------

    def painting_file(request: Request):
        p = svc.studio().get_painting(request.path_params["id"])
        return p if p is not None and Path(p.path).is_file() else None

    async def painting_image(request: Request):
        p = painting_file(request)
        if p is None:
            return PlainTextResponse("not found", 404)
        return FileResponse(p.path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})  # fmt: skip

    async def painting_thumb(request: Request):
        p = painting_file(request)
        if p is None:
            return PlainTextResponse("not found", 404)
        return thumb_of(Path(p.path), thumbs / "paintings" / f"{p.id}.jpg")

    # -- Studio: painters -------------------------------------------------------

    def painters_view(request: Request, notice="", errors=(), status=200, draft=None):
        resp = render(request, "painters.html", page="studio",
                      painters=svc.studio().painters(), kinds=photos.KINDS,
                      notice=notice, errors=list(errors), draft=draft)  # fmt: skip
        resp.status_code = status
        return resp

    async def painters_page(request: Request):
        return painters_view(request)

    async def painter_form(request: Request) -> tuple[str, str, tuple[str, ...]]:
        form = await request.form()
        suits = tuple(s for s in form.getlist("suits") if s in photos.KINDS)
        return (form.get("name") or "").strip(), form.get("manner") or "", suits

    async def add_painter(request: Request):
        name, manner, suits = await painter_form(request)
        try:
            svc.studio().add_painter(name, manner, suits)
        except ValueError as e:
            draft = {"name": name, "manner": manner}
            return painters_view(request, errors=[str(e)], status=400, draft=draft)
        return painters_view(request, notice=f"Added {name}.")

    async def edit_painter(request: Request):
        st, slug = svc.studio(), request.path_params["slug"]
        if st.painter(slug) is None:
            return PlainTextResponse("unknown painter", 404)
        name, manner, suits = await painter_form(request)
        try:
            st.update_painter(slug, name, manner, suits)
        except ValueError as e:
            return painters_view(request, errors=[str(e)], status=400)
        return painters_view(request, notice=f"Saved {name}.")

    async def delete_painter(request: Request):
        st, slug = svc.studio(), request.path_params["slug"]
        p = st.painter(slug)
        if p is None:
            return PlainTextResponse("unknown painter", 404)
        st.remove_painter(slug)
        return painters_view(request, notice=f"Removed {p.name}.")

    async def delete_photo(request: Request):
        store, pid = svc.photos(), request.path_params["id"]
        p = store.get(pid)
        if p is None:
            return PlainTextResponse("unknown photo", 404)
        cat = svc.catalogue()
        before = eligible_live(cat, all_items())
        store.remove(pid)
        svc.studio().forget_photo(pid)
        (thumbs / "photos" / f"{pid}.jpg").unlink(missing_ok=True)
        sync_if_dropped(cat, before)  # a live photo leaves the TV now
        return photos_view(request, notice=f"Removed {p.filename}.")

    def versions_view(request: Request, errors=(), notice="", status=200):
        cat, s = svc.catalogue(), svc.settings()
        items, lib, today = all_items(), svc.catalogue().library(), svc.today()
        made = cat.variants()
        rows = []
        for t in cat.variant_types():
            mine = [v for v in made if v.type == t.slug]
            rows.append(
                {"slug": t.slug, "name": t.name, "prompt": t.prompt,
                 "cost": sum(v.cost or 0.0 for v in mine),
                 "versions": sum(
                     1 for v in mine if library.state(lib, f"{library.VARIANT}{v.id}") != "removed"
                 ),
                 "make": variants.make_now_count(cat, items, s, t.slug, today)}
            )  # fmt: skip
        resp = render(
            request,
            "version_styles.html",
            page="studio",
            rows=rows,
            total_cost=sum(r["cost"] for r in rows),
            s=s,
            has_key=keys.has_key("openrouter"),
            estimate=VARIANT_ESTIMATE,
            errors=list(errors),
            notice=notice,
        )
        resp.status_code = status
        return resp

    async def versions_page(request: Request):
        return versions_view(request)

    async def add_variant_type(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        prompt = (form.get("prompt") or "").strip()
        slug = pipeline.slug(name)
        errors = []
        if not slug:
            errors.append("give the type a name")
        if not prompt:
            errors.append("give the type a prompt")
        if not errors:
            try:
                svc.catalogue().add_variant_type(slug, name, prompt)
            except ValueError:
                errors.append(f"there is already a type called {name}")
        if errors:
            return versions_view(request, errors, status=400)
        return versions_view(request, notice=f"Added {name}.")

    async def edit_variant_type(request: Request):
        slug = request.path_params["slug"]
        cat = svc.catalogue()
        if not any(t.slug == slug for t in cat.variant_types()):
            return PlainTextResponse("unknown type", 404)
        form = await request.form()
        name = (form.get("name") or "").strip()
        prompt = (form.get("prompt") or "").strip()
        if not name or not prompt:
            return versions_view(
                request, ["a type needs a name and a prompt"], status=400
            )
        cat.update_variant_type(slug, name, prompt)
        return versions_view(request, notice=f"Saved {name}.")

    async def save_variant_limit(request: Request):
        form = await request.form()
        s = svc.settings()
        try:
            new = dataclasses.replace(s, variant_limit=int(form.get("variant_limit")))
        except (TypeError, ValueError):
            errors = ["variants per night must be a whole number"]
        else:
            errors = new.validate()
        if errors:
            return versions_view(request, errors, status=400)
        svc.save_settings(new)
        return versions_view(request, notice="Saved. Changes apply from tonight's run.")

    async def make_now(request: Request):
        slug = request.path_params["slug"]
        cat = svc.catalogue()
        vt = next((t for t in cat.variant_types() if t.slug == slug), None)
        if vt is None:
            return PlainTextResponse("unknown type", 404)
        form = await request.form()
        if form.get("confirm") != "1":
            count = variants.make_now_count(
                cat, all_items(), svc.settings(), slug, svc.today()
            )
            return render(
                request, "library_confirm.html", page="studio", type=vt, count=count,
                cost=count * VARIANT_ESTIMATE, action=request.url.path, fields=[],
                estimate=VARIANT_ESTIMATE, cancel="/studio/versions",
            )  # fmt: skip
        svc.submit("make-now", {"type": slug})
        return RedirectResponse("/runs", status_code=303)

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
        # Ban and collect became Remove from Library and the star (/library/*).
        if state not in ("pinned", "clear"):
            return PlainTextResponse("bad state", 400)
        cat = svc.catalogue()
        # A pin outlives its item (a deleted portrait, say), so it can still go.
        if key not in all_items() and not (state == "clear" and key in cat.overrides()):
            return PlainTextResponse("unknown work", 404)
        cat.set_override(key, None if state == "clear" else state)
        return back(request)

    async def thumb(request: Request):
        folder, name = request.path_params["folder"], request.path_params["name"]
        if folder not in ("full", "matted") or not re.fullmatch(r"[\w.-]+\.jpg", name):
            return PlainTextResponse("not found", 404)
        src = svc.dest / folder / name
        if not src.exists():
            return PlainTextResponse("not found", 404)
        out = cached_thumb(src, thumbs / folder / name)
        return FileResponse(out, headers={"Cache-Control": "max-age=86400"})

    # One client for every work_image request, so its per-host pacing holds
    # across a page of thumbnails.
    image_http = http or Http(timeout=20)
    # Its own few threads: a page of thumbnails waiting on a museum's pacing
    # must not take the threads that serve pages.
    image_pool = ThreadPoolExecutor(4, thread_name_prefix="work-image")
    # (work key, size) -> when its picture last failed; retried after an hour.
    image_failed: dict[tuple[str, str], float] = {}

    def fetch_work_image(key: str, size: str) -> Path | tuple[bytes, str] | None:
        """The cached thumbnail's path, or a preview's bytes and media type."""
        out = thumbs / "works" / f"{hashlib.sha1(key.encode()).hexdigest()}.jpg"
        if size == "thumb" and out.exists():
            return out
        if time.time() - image_failed.get((key, size), 0) < 3600:
            return None
        w = svc.catalogue().work(key)
        url = w and (
            pipeline.thumb_url(w) if size == "thumb" else pipeline.preview_url(w)
        )
        if not url:
            return None
        try:
            data = image_http.get_bytes(url, retries=1)
            with Image.open(io.BytesIO(data)) as im:
                if size == "preview":
                    im.verify()
                    return data, Image.MIME.get(im.format or "", "image/jpeg")
                im = im.convert("RGB")
                im.thumbnail((400, 400), Image.LANCZOS)
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(f".{threading.get_ident()}.tmp")
                im.save(tmp, "JPEG", quality=85)
                tmp.replace(out)
            return out
        except Exception as ex:  # an HTTP error, a timeout or not an image
            log.info("no picture for %s from %s: %s", key, url, ex)
            # Remember only answers that won't change soon. A 403 or 429 (the
            # Met and Wikimedia under load), a 5xx or a timeout is tried again
            # on the next request.
            status = getattr(getattr(ex, "response", None), "status_code", None)
            if status in (404, 410) or not isinstance(ex, httpx.HTTPError):
                image_failed[(key, size)] = time.time()
            return None

    async def work_image(request: Request):
        """A museum work's thumbnail (cached) or preview, served from here."""
        size, key = request.path_params["size"], request.path_params["key"]
        if size not in ("thumb", "preview"):
            return PlainTextResponse("not found", 404)
        loop = asyncio.get_running_loop()
        got = await loop.run_in_executor(image_pool, fetch_work_image, key, size)
        headers = {"Cache-Control": "max-age=86400"}
        if isinstance(got, Path):
            return FileResponse(got, media_type="image/jpeg", headers=headers)
        if got is not None:
            return Response(got[0], media_type=got[1], headers=headers)
        return PlainTextResponse("not found", 404)

    async def variant_file(request: Request):
        """A variant's JPEG, found by id so the request never names a path."""
        v = svc.catalogue().variant(request.path_params["id"])
        src = Path(v.path) if v else None
        if not src or not src.is_file():
            return PlainTextResponse("not found", 404)
        if not request.query_params.get("thumb"):
            return FileResponse(src, headers={"Cache-Control": "max-age=86400"})
        out = cached_thumb(src, thumbs / "variants" / f"{v.id}.jpg")
        return FileResponse(out, headers={"Cache-Control": "max-age=86400"})

    async def work_page(request: Request):
        """Any item's page: tags, Library state, versions and, for some, Delete."""
        key = request.path_params["key"]
        cat, items = svc.catalogue(), all_items()
        it = items.get(key)
        if it is None:
            return PlainTextResponse("unknown work", 404)
        lib, mine = cat.library(), cat.user_tags().get(key, set())
        groups: dict[str, list[str]] = {}
        for t in sorted(it.tags):
            if t.partition(":")[0] in TAG_GROUPS and t != "yours:favorite":
                groups.setdefault(TAG_GROUPS[t.partition(":")[0]], []).append(t)
        live = {x.key for x in library.live(cat, items)}
        versions = sorted(
            (v for v in items.values() if v.kind == "version" and v.root == key),
            key=lambda v: v.added_at,
            reverse=True,
        )
        thumb, full = pictures(it)
        painting = (
            svc.studio().get_painting(int(key.removeprefix(library.PORTRAIT)))
            if it.kind == "portrait"
            else None
        )
        return render(
            request,
            "work.html",
            page="library",
            it=it,
            w=it.work if it.kind == "museum" else None,
            thumb=thumb,
            full=full,
            state=item_state(lib, it),
            starred=key in cat.collection(),
            on_tv=key in live,
            groups=[
                (g, groups[g])
                for g in dict.fromkeys(TAG_GROUPS.values())
                if g in groups
            ],
            mine={f"yours:{t}" for t in mine},
            tag_label=tag_label,
            rejected=cat.rejected().get(key),
            original=items.get(it.root) if it.kind == "version" else None,
            variant=cat.variant(it.variant_id) if it.variant_id else None,
            versions=[(v, item_state(lib, v), v.key in live) for v in versions],
            has_versions=bool(versions),
            types=cat.variant_types(),
            photo_id=key.removeprefix(library.PHOTO) if it.kind == "photo" else None,
            painting=painting,
            # The photo a portrait was painted from, while it is still here.
            painted_from=svc.photos().get(painting.photo_id)
            if painting and painting.photo_id
            else None,
            file_missing=bool(it.path) and not Path(it.path).is_file(),
        )

    async def generate_for_work(request: Request):
        key = request.path_params["key"]
        cat = svc.catalogue()
        it = all_items().get(key)
        if it is None or it.kind == "version":
            return PlainTextResponse("unknown work", 404)
        form = await request.form()
        vt = next((t for t in cat.variant_types() if t.slug == form.get("type")), None)
        if not vt:
            return PlainTextResponse("unknown type", 404)
        prompt = (form.get("prompt") or "").strip()
        svc.submit(
            "generate",
            {
                "type": vt.slug,
                "keys": [key],
                "prompt": prompt if prompt and prompt != vt.prompt.strip() else None,
            },
        )
        return RedirectResponse("/runs", status_code=303)

    async def now_page(request: Request):
        cat = svc.catalogue()
        now = svc.now_showing(cat)
        p = find_content(cat, svc.dest, now["content_id"]) if now["ours"] else None
        about: dict = {"it": None}
        if p is not None:
            # The buttons need the item's key, which now.json leaves out for photos.
            ctx = tv_context(cat)
            about = tv_item(ctx, ctx.live_key(p.filename, p.key))
        return render(request, "now.html", page="now", now=now, about=about)

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
            Route("/history", history_page),
            # Old pages, and where they went.
            Route("/catalogue", old_catalogue),
            Route("/collection", moved("/library?view=favorites")),
            Route("/portraits", moved("/library?tag=source%3Astudio")),
            Route("/variants", moved("/schedule")),
            Route("/tags.json", moved("/library/tags.json")),
            Route("/library", library_page),
            Route("/library/tags.json", library_tags_json),
            Route("/library/add", library_action(do_add), methods=["POST"]),
            Route(
                "/library/remove",
                library_action(do_remove, emptying=True),
                methods=["POST"],
            ),
            Route(
                "/library/put-back",
                library_action(do_put_back, emptying=True),
                methods=["POST"],
            ),
            Route("/library/star", library_action(do_star), methods=["POST"]),
            Route("/library/tag", library_action(do_tag), methods=["POST"]),
            Route(
                "/library/make-versions",
                library_action(do_make_versions),
                methods=["POST"],
            ),
            Route(
                "/versions/{id:int}/remove",
                version_action(version_remove),
                methods=["POST"],
            ),
            Route(
                "/versions/{id:int}/put-back",
                version_action(version_put_back),
                methods=["POST"],
            ),
            Route(
                "/versions/{id:int}/regenerate",
                version_action(version_regenerate),
                methods=["POST"],
            ),
            Route("/settings", settings_page, methods=["GET", "POST"]),
            Route("/photos", photos_page),
            Route("/photos/upload", upload_photos, methods=["POST"]),
            Route("/photos/google/signin", google_signin, methods=["GET", "POST"]),
            Route("/photos/google/signout", google_signout, methods=["POST"]),
            Route("/photos/google/pick", google_pick, methods=["POST"]),
            Route("/photos/google/cancel", google_cancel, methods=["POST"]),
            Route("/photos/{id}/thumb", photo_thumb),
            Route("/photos/{id}/kind", set_photo_kind, methods=["POST"]),
            Route("/photos/{id}/delete", delete_photo, methods=["POST"]),
            Route("/studio", studio_index),
            Route("/studio/tries/{id:int}.jpg", try_image),
            Route("/studio/tries/{id:int}/thumb", try_thumb),
            Route("/studio/tries/{id:int}/save", save_try, methods=["POST"]),
            Route("/studio/tries/{id:int}/delete", delete_try, methods=["POST"]),
            Route("/portraits/{id:int}.jpg", painting_image),
            Route("/portraits/{id:int}/thumb", painting_thumb),
            Route("/schedule", schedule_page),
            Route("/schedule/schedules", add_schedule, methods=["POST"]),
            Route("/schedule/schedules/{id}", save_schedule, methods=["POST"]),
            Route("/schedule/schedules/{id}/delete", delete_schedule, methods=["POST"]),
            Route("/studio/versions", versions_page),
            Route("/studio/versions/types", add_variant_type, methods=["POST"]),
            Route("/studio/versions/types/{slug}", edit_variant_type, methods=["POST"]),
            Route("/studio/versions/limit", save_variant_limit, methods=["POST"]),
            Route("/studio/versions/{slug}/make-now", make_now, methods=["POST"]),
            Route("/studio/painters", painters_page),
            Route("/studio/painters", add_painter, methods=["POST"]),
            Route("/studio/painters/{slug}", edit_painter, methods=["POST"]),
            Route("/studio/painters/{slug}/delete", delete_painter, methods=["POST"]),
            Route("/studio/{photo_id}", studio_page),
            Route("/studio/{photo_id}/paint", studio_paint, methods=["POST"]),
            Route("/compare", compare_page),
            Route("/compare/{name}", compare_image),
            Route("/runs", runs_page),
            Route("/jobs/{kind}", submit_job, methods=["POST"]),
            Route("/tv/status", tv_panel),
            Route("/tv/{action}", tv_command, methods=["POST"]),
            Route("/works/{key:path}/override", set_override, methods=["POST"]),
            Route(
                "/works/{key:path}/variants/generate",
                generate_for_work,
                methods=["POST"],
            ),
            Route("/works/{key:path}/delete", delete_item, methods=["POST"]),
            Route("/works/{key:path}", work_page),  # last: it matches any key
            Route("/variant-file/{id:int}", variant_file),
            Route("/work-image/{size}/{key:path}", work_image),
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
