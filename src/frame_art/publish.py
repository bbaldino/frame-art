"""Keep a rotating pool of rendered works in the folder Home Assistant uploads from.

Layout under the destination folder:

    full/      3840x2160 renders; HA uploads these with no matte
    matted/    native-shape works (pad style "matte"); HA uploads with a matte
    .staging/  renders in progress, renamed into place when complete
    _meta/index.json   filename -> title/artist/date/source for what's live

What HA has to do next (uploads, and TV content ids to delete) goes to HA over
MQTT rather than into this folder, which HA serves publicly at /local/.

HA's art_upload_batch only reads images directly inside the folder it is given,
and records filename -> content_id in a ``.samsungtv_upload.json`` sidecar there.
We read that sidecar (never write it) to learn which TV id to delete when a work
is retired, and report it to HA (see ``pending``). Published files are never
rewritten in place: the integration skips by filename + mtime, so a rewrite
would upload a duplicate.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import imaging, pipeline
from .catalogue import Catalogue
from .net import Http

log = logging.getLogger(__name__)

SIDECAR = ".samsungtv_upload.json"
FOLDERS = ("full", "matted")
# Keep listing retired ids this long, so a night HA misses doesn't leak art onto the TV.
RETIRE_WINDOW = 14 * 24 * 3600
# A retired work may come back after this long.
COOLDOWN = 365 * 24 * 3600


@dataclass
class PublishResult:
    added: list[str]
    retired: list[str]
    failed: list[str]


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def read_sidecar(folder: Path) -> dict[str, str]:
    """filename -> TV content_id, from the integration's upload sidecar."""
    try:
        data = json.loads((folder / SIDECAR).read_text())
    except (OSError, ValueError):
        return {}
    return {name: e["content_id"] for name, e in data.items() if e.get("content_id")}


def diversify(cands: list[pipeline.Candidate], seed: int) -> list[pipeline.Candidate]:
    """Order by score with some jitter, then interleave sources round-robin."""
    rng = random.Random(seed)
    by_source: dict[str, list[tuple[float, pipeline.Candidate]]] = defaultdict(list)
    for c in cands:
        by_source[c.work.source].append((c.score + rng.uniform(0, 2), c))
    queues = [sorted(v, key=lambda t: t[0], reverse=True) for v in by_source.values()]
    rng.shuffle(queues)
    out: list[pipeline.Candidate] = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0)[1])
    return out


class Publisher:
    def __init__(
        self,
        cat: Catalogue,
        http: Http,
        dest: Path,
        pad_style: str,
        kinds: tuple[str, ...] = ("painting",),
        prefs: pipeline.Prefs = pipeline.DEFAULT_PREFS,
    ):
        if pad_style not in imaging.PAD_STYLES:
            raise ValueError(f"pad style must be one of {imaging.PAD_STYLES}")
        self.cat = cat
        self.http = http
        self.dest = dest
        self.pad_style = pad_style
        self.kinds = kinds
        self.prefs = prefs

    def run(
        self, count: int, pool: int, season: str | None, seed: int
    ) -> PublishResult:
        added, failed = self._add(count, season, seed)
        retired = self._retire(pool)
        self._write_meta()
        return PublishResult(added, retired, failed)

    def sync(self) -> list[str]:
        """Apply bans to the live pool now, without adding anything."""
        retired = self._retire(pool=None)
        self._write_meta()
        return retired

    def _add(
        self, count: int, season: str | None, seed: int
    ) -> tuple[list[str], list[str]]:
        now = time.time()
        history = self.cat.published()
        blocked = {
            p.key
            for p in history
            if p.retired_at is None or now - p.retired_at < COOLDOWN
        }
        blocked |= self.cat.rejected_keys()
        overrides = self.cat.overrides()
        blocked |= {k for k, s in overrides.items() if s == "banned"}
        # A pin overrides the cooldown, but not a work already live.
        live_keys = {p.key for p in history if p.retired_at is None}
        blocked -= {k for k, s in overrides.items() if s == "pinned"} - live_keys
        live_hashes = [p.phash for p in history if p.retired_at is None]
        works = [w for w in self.cat.works() if w.kind in self.kinds]
        cands, _ = pipeline.filter_works(works, season, self.prefs)
        cands = diversify([c for c in cands if c.work.key not in blocked], seed)
        # Pinned works go first, keeping their diversified order.
        cands.sort(key=lambda c: overrides.get(c.work.key) != "pinned")

        added: list[str] = []
        failed: list[str] = []
        for cand in cands:
            if len(added) >= count:
                break
            try:
                out = pipeline.render(self.http, cand, self.pad_style)
            except pipeline.Rejected as ex:
                log.info("%s: skipped for good: %s", cand.work.key, ex)
                self.cat.record_reject(cand.work.key, str(ex))
                continue
            except pipeline.NotFullScreen as ex:
                log.info("%s: skipped: %s", cand.work.key, ex)
                continue
            except Exception as ex:  # noqa: BLE001 - one bad image must not stop the run
                log.warning("%s: render failed: %s", cand.work.key, ex)
                failed.append(cand.work.key)
                continue
            if any(
                imaging.hamming(out.phash, h) <= pipeline.PHASH_DISTANCE
                for h in live_hashes
            ):
                log.info(
                    "%s: looks like a work already in the pool; skipping", cand.work.key
                )
                continue
            folder = pipeline.folder_for(out.plan, self.pad_style)
            path = self.dest / folder / pipeline.filename(cand.work)
            path.parent.mkdir(parents=True, exist_ok=True)
            pipeline.write_atomic(out.image, path, self.dest / ".staging")
            self.cat.record_publish(path.name, folder, cand.work.key, out.phash)
            live_hashes.append(out.phash)
            added.append(f"{folder}/{path.name}")
            log.info("published %s/%s", folder, path.name)
        return added, failed

    def _retire(self, pool: int | None) -> list[str]:
        """Retire banned works, then the oldest unpinned ones beyond ``pool``."""
        overrides = self.cat.overrides()
        live = self.cat.published(active=True)
        banned = [p for p in live if overrides.get(p.key) == "banned"]
        rest = [p for p in live if p not in banned and overrides.get(p.key) != "pinned"]
        excess = 0 if pool is None else len(live) - len(banned) - pool
        doomed = banned + rest[: max(0, excess)]  # oldest first
        if not doomed:
            return []
        sidecars = {f: read_sidecar(self.dest / f) for f in FOLDERS}
        retired = []
        for p in doomed:
            content_id = sidecars.get(p.folder, {}).get(p.filename)
            (self.dest / p.folder / p.filename).unlink(missing_ok=True)
            self.cat.record_retire(p.filename, content_id)
            retired.append(f"{p.folder}/{p.filename}")
            log.info(
                "retired %s/%s (TV id %s)",
                p.folder,
                p.filename,
                content_id or "never uploaded",
            )
        return retired

    def _write_meta(self) -> None:
        works = {w.key: w for w in self.cat.works()}
        index = {}
        for p in self.cat.published(active=True):
            w = works.get(p.key)
            if w is None:
                continue
            index[f"{p.folder}/{p.filename}"] = {
                "title": w.title,
                "artist": w.artist,
                "date": w.date,
                "source": w.source,
                "license": w.license,
                "url": w.page_url,
                "published_at": int(p.published_at),
            }
        _write_json(self.dest / "_meta" / "index.json", index)


def pending(cat: Catalogue, dest: Path) -> dict:
    """What HA still has to do: live files not yet uploaded, TV ids to delete."""
    sidecars = {f: read_sidecar(dest / f) for f in FOLDERS}
    live = cat.published(active=True)
    to_upload = [
        f"{p.folder}/{p.filename}"
        for p in live
        if p.filename not in sidecars.get(p.folder, {})
    ]
    cutoff = time.time() - RETIRE_WINDOW
    deleted = cat.deleted_ids()
    retire = []
    for p in cat.published(active=False):
        if not p.retired_at or p.retired_at < cutoff:
            continue
        content_id = p.content_id or sidecars.get(p.folder, {}).get(p.filename)
        if content_id and not p.content_id:
            # Uploaded while we were retiring it; we only learn the id now.
            cat.set_content_id(p.filename, content_id)
        if content_id and content_id not in deleted:
            retire.append(content_id)
    return {"to_upload": to_upload, "retire": retire, "live": len(live)}
