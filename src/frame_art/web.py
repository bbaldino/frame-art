"""LAN web UI: the pool, the catalogue, settings, comparison sheets, runs.

Server-rendered with plain HTML forms; no build step. It never talks to the TV
and holds no HA token. The "on the TV now" picture is HA's own public
thumbnail, loaded by the browser.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import fields
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlencode

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
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import (
    google_photos,
    imaging,
    keys,
    library,
    photos,
    pipeline,
    schedules,
    studio,
    variants,
)
from .catalogue import Variant
from .classify import current_season
from .net import Http
from .publish import content_index, read_sidecar
from .schedules import Schedule
from .service import ARG_JOBS, JOB_KINDS, TV_ACTIONS, Service
from .settings import (
    PHOTO_SOURCES,
    SLIDESHOW,
    SOURCE_NAMES,
    SOURCES,
    Settings,
)
from .sources import ART_KINDS, KINDS, info

CHOOSE_FROM: dict[str, str] = {}  # replaced in Task 11

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
PAGE = 60
THUMB = (480, 270)
# Largest photo an upload may carry, per file.
MAX_UPLOAD = 50 * 1024 * 1024
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


def tag_pairs(w, seasonal=()):
    """(kind, tag) for everything a work can be filtered by.

    ``seasonal`` is the work's seasonal-version tags (``seasonal_tags``), which
    live in frame-art's own tables rather than on the work.
    """
    for kind in ("artist", "style", "genre", "place"):
        if getattr(w, kind):
            yield kind, getattr(w, kind)
    for x in w.subjects:
        yield "subject", x
    for x in seasonal:
        yield "seasonal", x


def tags_of(w, seasonal=()) -> set[str]:
    return {t.lower() for _, t in tag_pairs(w, seasonal)}


def seasonal_tags(cat) -> dict[str, dict[str, Variant]]:
    """Per work, "<Type> version" -> its current variant, for each type it has one of.

    Named "... version" so the tag never collides with a subject such as
    "Halloween".
    """
    names = {t.slug: t.name for t in cat.variant_types()}
    current = {v.id: v for v in cat.variants()}
    out: dict[str, dict[str, Variant]] = {}
    for (key, type), st in cat.variant_states().items():
        if st.current in current and type in names:
            out.setdefault(key, {})[f"{names[type]} version"] = current[st.current]
    return out


def wanted_tags(q) -> list[str]:
    """The tag filters in a query string, lowercased, in order, deduplicated."""
    tags = [v.strip().lower() for k, v in q.multi_items() if k in TAG_PARAMS]
    return list(dict.fromkeys(t for t in tags if t))


def suggest_tags(works, tags: list[str], text: str, seasonal=None) -> list[dict]:
    """Tags of the works matching ``tags``, for the catalogue's autocomplete.

    Word-start matches rank before matches mid-word, then by how many works
    carry the tag; tags already in the filter are left out.
    """
    counts: Counter = Counter()
    names: dict[str, Counter] = {}
    seasonal = seasonal or {}
    for w in works:
        extra = seasonal.get(w.key, ())
        if tags and not set(tags) <= tags_of(w, extra):
            continue
        pairs = tag_pairs(w, extra)
        for key, (kind, tag) in {t.lower(): (k, t) for k, t in pairs}.items():
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
# Query parameters that narrow "All works"; one must be set to act on all of it.
NARROWING = ("q", "source", "kind", "tag", "added_since", "view")
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
        seasonal = seasonal_tags(cat)
        for w in works:
            if only_collection and w.key not in collection:
                continue
            if source and w.source != source:
                continue
            if kind and w.kind != kind:
                continue
            versions = seasonal.get(w.key, {})
            if tags and not set(tags) <= tags_of(w, versions):
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
                    "versions": versions,
                    # Filtering by a seasonal tag shows that version's picture.
                    "shown_version": next(
                        (v for t, v in versions.items() if t.lower() in tags), None
                    ),
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
        # Oldest first, so the last wins; live rows go last, as a swapped-out
        # version shares its replacement's publish time.
        latest = {
            p.key: p for p in sorted(published, key=lambda p: p.retired_at is None)
        }
        live = {p.key for p in published if p.retired_at is None}
        todo = request.query_params.get("todo") == "1"
        by_content = content_index(cat, svc.dest)
        labels = svc.variant_labels(cat)
        items, day = [], None
        now = time.time()
        for r in cat.shown():
            key = r["key"]
            shown_file = by_content.get(r["content_id"])
            if key is None:
                # Uploaded after it was first seen: the sidecar knows it now.
                key = shown_file.key if shown_file else None
            variant = labels.get(shown_file.filename) if shown_file else None
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
                    "variant": variant["type"] if variant else None,
                    "portrait": bool(key) and key.startswith(library.PORTRAIT),
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
        seasonal = {k: list(v) for k, v in seasonal_tags(cat).items()}
        return JSONResponse(suggest_tags(works, wanted_tags(q), text, seasonal))

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
            "hide_nudes": q.get("nudes") == "hide",
            "full_screen": q.get("full_screen") == "1",
        }  # fmt: skip

    def narrowed(q) -> bool:
        """Whether an "all" action is a choice: "All works" needs a filter."""
        return q.get("scope") != "all" or any(q.get(k) for k in NARROWING)

    def lib_rows(cat, items: dict[str, library.Item], f: dict) -> list[dict]:
        """The items ``f`` matches, sorted, each version after its original."""
        s = svc.settings()
        lib, starred, rejected = cat.library(), cat.collection(), cat.rejected()
        live = {x.key for x in library.live(cat, items)}
        max_crop = imaging.CROP_LEVELS[s.crop]
        strict = pipeline.Prefs(max_crop, f["full_screen"], not f["hide_nudes"])
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
            # Today's Settings filters, as the publisher used to apply them.
            if it.work is not None and (f["hide_nudes"] or f["full_screen"]):
                why = pipeline.exclusion(it.work, strict)
                if why == "nude" or (f["full_screen"] and why in (
                    "not full screen at this crop level", "too tall or too wide"
                )):  # fmt: skip
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
        t = pipeline.thumb_url(it.work)
        return t, pipeline.preview_url(it.work) or t

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

    def chosen(request: Request, form, cat, items) -> list[str] | PlainTextResponse:
        """The keys a Library action is for: the ticked ones, or the whole filter."""
        if form.get("all") == "1":
            q = request.query_params
            if not narrowed(q):
                return PlainTextResponse(
                    "Choose a filter first: that would act on every work.", 400
                )
            return [r["it"].key for r in lib_rows(cat, items, lib_filter(q))]
        keys = [k for k in form.getlist("key") if k in items]
        if not keys:
            return PlainTextResponse("no works chosen", 400)
        return keys

    def library_action(fn):
        """Wrap ``fn(cat, items, keys, form)`` as a Library POST: pick the keys,
        act, sync when something live dropped out, then go back."""

        async def handler(request: Request):
            cat, form, items = svc.catalogue(), await request.form(), all_items()
            keys = chosen(request, form, cat, items)
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
            new = Settings()
            # Not on this form; edited elsewhere, so carry them over.
            new.schedules, new.variant_limit = s.schedules, s.variant_limit
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

    MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
    # Days per month, for the timeline's month columns.
    MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    # A bar narrower than this (percent of the year) puts its label beside it.
    NARROW_BAR = 9

    def schedule_editor(s, sch, types, today, live, ready, is_new):
        """The editor's fields and rows for ``s`` (a saved schedule or a draft)."""
        start_m, _, start_d = s.start.partition("-")
        end_m, _, end_d = s.end.partition("-")
        known = next((k for k in sch if k.id == s.id), None)
        day = today
        if known and not schedules.covers(known, today):
            day = schedules.next_start(known, today)
        # This schedule's own portion: the cascade up to it, less the layers above.
        ls = schedules.layers(sch, day)
        mine = ls.index(known) + 1 if known in ls else 0
        above = schedules.allocate(ls[: mine - 1], len(live)) if mine else {}
        upto = schedules.allocate(ls[:mine], len(live))
        rows = []
        for slug, value in s.shares.items():
            if slug not in types:
                continue
            if slug == schedules.PORTRAITS:
                # Saved paintings need no versions made, so no "ready" meter.
                rows.append({"slug": slug, "name": "Portraits (saved Studio paintings)",
                             "value": value})  # fmt: skip
                continue
            m = upto.get(slug, 0) - above.get(slug, 0)
            n = min(m, max(0, ready.get(slug, 0) - above.get(slug, 0)))
            rows.append(
                {"slug": slug, "name": types[slug], "value": value, "n": n, "m": m,
                 "pct": round(100 * n / m) if m else 0}
            )  # fmt: skip
        return {
            "id": s.id,
            "new": is_new,
            "name": s.name,
            "start_month": start_m.lstrip("0"),
            "start_day": start_d.lstrip("0"),
            "end_month": end_m.lstrip("0"),
            "end_day": end_d.lstrip("0"),
            "rows": rows,
            "others": [(k, v) for k, v in types.items() if k not in s.shares],
            "total": sum(v for v in s.shares.values() if isinstance(v, int)),
        }

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

    DONE = {"saved": "Saved to Portraits.", "deleted": "Try deleted."}

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
            st.save(t.id)
        except ValueError as e:
            return PlainTextResponse(str(e), 400)
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

    # -- Portraits --------------------------------------------------------------

    def portraits_view(request: Request, notice=""):
        ps = svc.photos()
        return render(request, "paintings.html", page="portraits",
                      paintings=svc.studio().paintings(), notice=notice,
                      photo=lambda pid: ps.get(pid) if pid else None)  # fmt: skip

    async def portraits_page(request: Request):
        return portraits_view(request)

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

    async def remove_painting(request: Request):
        pid = request.path_params["id"]
        if not svc.studio().remove_painting(pid):
            return PlainTextResponse("unknown portrait", 404)
        svc.submit("portraits")  # takes it off the TV
        (thumbs / "paintings" / f"{pid}.jpg").unlink(missing_ok=True)
        return portraits_view(request, notice="Removed from Portraits.")

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
        store.remove(pid)
        svc.studio().forget_photo(pid)
        (thumbs / "photos" / f"{pid}.jpg").unlink(missing_ok=True)
        return photos_view(request, notice=f"Removed {p.filename}.")

    def variants_view(request: Request, errors=(), notice="", status=200, draft=None):
        cat = svc.catalogue()
        s = svc.settings()
        today = svc.today()
        vtypes = cat.variant_types()
        types = variants.share_types(cat)
        color = {t.slug: i % 6 for i, t in enumerate(vtypes)}
        sch = schedules.known(s.schedules, set(types))
        usable = set(cat.usable_variants())
        works = {w.key: w for w in cat.works()}
        live: dict[str, str] = {}  # work key -> a live file to show
        for p in cat.published(active=True):
            live.setdefault(p.key, f"/thumb/{p.folder}/{p.filename}")
        progress = variants.progress(cat, s, today)
        candidates = {
            row["slug"]: [
                (k, works[k].title if k in works else k, thumb)
                for k, thumb in live.items()
                if (k, row["slug"]) not in usable
            ]
            for row in progress
        }

        def day(mmdd):
            m, _, d = mmdd.partition("-")
            return f"{MONTHS[int(m) - 1]} {int(d)}"

        # The timeline: widest at the top, so the schedule that wins is lowest.
        timeline = []
        for k in reversed(schedules.order(sch)):
            pieces = schedules.bars(k)
            widest = max(range(len(pieces)), key=lambda i: pieces[i][1])
            left, width = pieces[widest]
            main = max(k.shares, key=k.shares.get) if k.total() else None
            timeline.append(
                {"id": k.id, "name": k.name, "label": f"{k.name} · {k.total()}%", "pieces": pieces,
                 "label_at": widest if width >= NARROW_BAR else None,
                 "side_left": left + width if left + width < 70 else None,
                 "side_right": 100 - left if left + width >= 70 else None,
                 "color": color.get(main, "x"), "dates": f"{day(k.start)} – {day(k.end)}",
                 "shares": ", ".join(f"{types[t]} {p}%" for t, p in k.shares.items())
                 or "Originals only"}
            )  # fmt: skip
        start_of_year = datetime.date(today.year, 1, 1)
        today_pct = min((today - start_of_year).days, 364) / 365 * 100

        # Today's screen: what's showing, with today's target alongside.
        target = s.today_shares(today, set(types))
        strip = [
            {"name": r["name"], "pct": r["live_share"],
             "target": round(target.get(r["slug"], 0), 1), "color": color[r["slug"]]}
            for r in progress
            if r["live_share"] or target.get(r["slug"])
        ]  # fmt: skip
        strip.append(
            {"name": "Originals", "pct": max(0, 100 - sum(x["pct"] for x in strip)),
             "target": round(100 - sum(target.values()), 1), "color": "x"}
        )  # fmt: skip

        editing = draft.id if draft else request.query_params.get("edit")
        ed = None
        ready = Counter(t for k, t in usable if k in live)
        if draft is not None:
            ed = schedule_editor(draft, sch, types, today, live, ready, not draft.id)
        elif editing == "new":
            mmdd = today.strftime("%m-%d")
            blank = Schedule("", "", mmdd, mmdd, {})
            ed = schedule_editor(blank, sch, types, today, live, ready, True)
        elif saved := next((k for k in s.schedules if k.id == editing), None):
            ed = schedule_editor(saved, sch, types, today, live, ready, False)

        resp = render(
            request,
            "variants.html",
            page="variants",
            rows=progress,
            s=s,
            total_cost=sum(r["cost"] for r in progress),
            has_key=keys.has_key("openrouter"),
            estimate=VARIANT_ESTIMATE,
            candidates=candidates,
            errors=list(errors),
            notice=notice,
            months=MONTHS,
            month_days=MONTH_DAYS,
            timeline=timeline,
            today_pct=today_pct,
            strip=strip,
            ed=ed,
            editing=editing,
        )
        resp.status_code = status
        return resp

    async def variants_page(request: Request):
        return variants_view(request)

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
            return variants_view(request, errors, status=400)
        return variants_view(request, notice=f"Added {name}.")

    async def edit_variant_type(request: Request):
        slug = request.path_params["slug"]
        cat = svc.catalogue()
        if not any(t.slug == slug for t in cat.variant_types()):
            return PlainTextResponse("unknown type", 404)
        form = await request.form()
        name = (form.get("name") or "").strip()
        prompt = (form.get("prompt") or "").strip()
        if not name or not prompt:
            return variants_view(
                request, ["a type needs a name and a prompt"], status=400
            )
        cat.update_variant_type(slug, name, prompt)
        return variants_view(request, notice=f"Saved {name}.")

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
            return variants_view(request, errors, status=400)
        svc.save_settings(new)
        return variants_view(request, notice="Saved. Changes apply from tonight's run.")

    def schedule_from_form(form, sid: str) -> tuple[Schedule, list[str]]:
        """The schedule a form describes, with what was typed kept for a re-render."""
        types = variants.share_types(svc.catalogue())
        errors = []

        def mmdd(month: str, day: str) -> str:
            m = (form.get(month) or "").strip()
            d = (form.get(day) or "").strip()
            if m.isdecimal() and d.isdecimal():
                return f"{int(m):02d}-{int(d):02d}"
            return f"{m}-{d}"  # fails validation, and shows as typed

        shares = {}
        for key, raw in form.multi_items():
            slug = key.removeprefix("share-")
            if slug == key or slug not in types:
                continue
            raw = raw.strip()
            try:
                shares[slug] = int(raw)
            except ValueError:
                shares[slug] = raw
                errors.append(f"{types[slug]}: {raw!r} is not a whole number")
        s = Schedule(
            sid,
            (form.get("name") or "").strip(),
            mmdd("start_month", "start_day"),
            mmdd("end_month", "end_day"),
            shares,
        )
        return s, errors or schedules.validate(s)

    async def add_schedule(request: Request):
        form = await request.form()
        s, errors = schedule_from_form(form, "")
        if errors:
            return variants_view(request, errors, status=400, draft=s)
        s = dataclasses.replace(s, id=schedules.new_id())
        new = dataclasses.replace(
            svc.settings(), schedules=[*svc.settings().schedules, s]
        )
        if errors := new.validate():
            return variants_view(request, errors, status=400, draft=s)
        svc.save_settings(new)
        return variants_view(
            request, notice=f"Saved {s.name}. Changes apply from tonight's run."
        )

    async def save_schedule(request: Request):
        sid = request.path_params["id"]
        current = svc.settings()
        if not any(k.id == sid for k in current.schedules):
            return PlainTextResponse("unknown schedule", 404)
        form = await request.form()
        s, errors = schedule_from_form(form, sid)
        if errors:
            return variants_view(request, errors, status=400, draft=s)
        new = dataclasses.replace(
            current,
            schedules=[s if k.id == sid else k for k in current.schedules],
        )
        if errors := new.validate():
            return variants_view(request, errors, status=400, draft=s)
        svc.save_settings(new)
        return variants_view(
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
        return variants_view(
            request, notice=f"Deleted {gone.name}. Changes apply from tonight's run."
        )

    async def generate_variants(request: Request):
        form = await request.form()
        vtype = form.get("type")
        chosen = form.getlist("keys")
        if not any(t.slug == vtype for t in svc.catalogue().variant_types()):
            return PlainTextResponse("unknown type", 404)
        if not chosen:
            return variants_view(request, ["choose at least one work"], status=400)
        svc.submit("generate", {"type": vtype, "keys": chosen, "prompt": None})
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
        out = cached_thumb(src, thumbs / folder / name)
        return FileResponse(out, headers={"Cache-Control": "max-age=86400"})

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
            Route("/library", library_page),
            Route("/library/tags.json", library_tags_json),
            Route("/library/add", library_action(do_add), methods=["POST"]),
            Route("/library/remove", library_action(do_remove), methods=["POST"]),
            Route("/library/put-back", library_action(do_put_back), methods=["POST"]),
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
            Route("/portraits", portraits_page),
            Route("/portraits/{id:int}.jpg", painting_image),
            Route("/portraits/{id:int}/thumb", painting_thumb),
            Route("/portraits/{id:int}/remove", remove_painting, methods=["POST"]),
            Route("/studio/painters", painters_page),
            Route("/studio/painters", add_painter, methods=["POST"]),
            Route("/studio/painters/{slug}", edit_painter, methods=["POST"]),
            Route("/studio/painters/{slug}/delete", delete_painter, methods=["POST"]),
            Route("/studio/{photo_id}", studio_page),
            Route("/studio/{photo_id}/paint", studio_paint, methods=["POST"]),
            Route("/variants", variants_page),
            Route("/variants/types", add_variant_type, methods=["POST"]),
            Route("/variants/types/{slug}", edit_variant_type, methods=["POST"]),
            Route("/variants/limit", save_variant_limit, methods=["POST"]),
            Route("/variants/schedules", add_schedule, methods=["POST"]),
            Route("/variants/schedules/{id}", save_schedule, methods=["POST"]),
            Route("/variants/schedules/{id}/delete", delete_schedule, methods=["POST"]),
            Route("/variants/generate", generate_variants, methods=["POST"]),
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
