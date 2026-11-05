"""Keep the TV's share of the Library rendered in the folder Home Assistant uploads from.

One publisher handles every kind of item: museum works, Brian's photos, saved
Studio portraits and AI versions of any of them. ``mix.plan`` decides what
retires and what fills each slice; the publisher carries that out, keeping an
original and its versions off the TV together.

Layout under the destination folder:

    full/      3840x2160 renders; HA uploads these with no matte
    matted/    native-shape works (pad style "matte"); HA uploads with a matte
    .staging/  renders in progress, renamed into place when complete
    _meta/index.json   filename -> title/artist/date/source for live museum works

What HA has to do next (uploads, and TV content ids to delete) goes to HA over
MQTT rather than into this folder. The folder (``/config/frame_art_pool`` on HA)
is outside ``/config/www``, so HA never serves it over HTTP: Brian's photos and
portraits rendered here stay on the LAN.

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
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import imaging, library, mix, pipeline
from .catalogue import NON_MUSEUM, Catalogue, Published
from .library import Item, Live
from .model import Work
from .net import Http
from .photos import PhotoStore
from .schedules import Schedule
from .studio import StudioStore

log = logging.getLogger(__name__)

SIDECAR = ".samsungtv_upload.json"
FOLDERS = ("full", "matted")
# Keep listing retired ids this long, so a night HA misses doesn't leak art onto the TV.
RETIRE_WINDOW = 14 * 24 * 3600


@dataclass
class PublishResult:
    added: list[str]
    retired: list[str]
    failed: list[str]


class Skipped(Exception):
    """Not published this run, and not rejected: tried again next time."""


class Duplicate(Skipped):
    """The picture looks like one already on the TV; skipped, not rejected."""


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


def last_shown(cat: Catalogue) -> dict[str, float]:
    """Item key -> when it last left the TV (or went up, if still live).

    Version files are published under their original's key, so they are read
    back as ``variant:<id>``.
    """
    files = cat.published_variants()
    out: dict[str, float] = {}
    for p in cat.published():
        key = f"{library.VARIANT}{files[p.filename]}" if p.filename in files else p.key
        out[key] = max(out.get(key, 0.0), p.retired_at or p.published_at)
    return out


def stand_in(item: Item) -> Work:
    """A Work for a photo or portrait (or a version of one), for rendering."""
    source, source_id = item.root.split(":", 1)
    kind = "photograph" if item.root.startswith(library.PHOTO) else "painting"
    return Work(source=source, source_id=source_id, title=item.title, artist=None,
                date=None, kind=kind, license="own")  # fmt: skip


class Publisher:
    def __init__(
        self,
        cat: Catalogue,
        http: Http | None,
        dest: Path,
        pad_style: str,
        crop: str,
        photos: PhotoStore,
        studio: StudioStore,
        data: Path,
    ):
        if pad_style not in imaging.PAD_STYLES:
            raise ValueError(f"pad style must be one of {imaging.PAD_STYLES}")
        self.cat = cat
        self.http = http
        self.dest = dest
        self.pad_style = pad_style
        self.crop = crop
        self.photos = photos
        self.studio = studio
        self.data = data
        # What's in the Library is Brian's choice, so nothing is left out for
        # its shape or its subject: only the crop level applies.
        self.prefs = pipeline.Prefs(
            imaging.CROP_LEVELS[crop], full_screen_only=False, allow_nudes=True
        )

    def run(
        self,
        layers: list[Schedule],
        pool: int,
        count: int,
        seed: int,
        now: float | None = None,
    ) -> PublishResult:
        """Retire and fill per today's slices (``layers``, narrowest first).

        Works rotating out retire only once their replacements are up, so a
        night of failed renders leaves the TV as it was.
        """
        now = time.time() if now is None else now
        all_items = library.items(self.cat, self.photos, self.studio)
        ok = library.eligible(self.cat, all_items)
        live = library.live(self.cat, all_items)
        pinned = self._pins(live)
        favorites = set(self.cat.collection())
        plan = mix.plan(layers, ok, live, pool, count, pinned, favorites,
                        last_shown(self.cat), now, seed)  # fmt: skip
        rows = {p.filename: p for p in self.cat.published(active=True)}
        retired = self._retire_files([rows[f] for f in plan.retire if f in rows])
        gone = set(plan.retire)
        live = [f for f in live if f.filename not in gone]
        added, failed = self._fill(plan, ok, live, pinned, rows, retired)
        self._write_meta()
        return PublishResult(added, retired, failed)

    def _pins(self, live: list[Live]) -> set[str]:
        """Pinned keys; a pinned work live as one of its versions pins that version."""
        pins = {k for k, s in self.cat.overrides().items() if s == "pinned"}
        return pins | {f.key for f in live if f.root in pins}

    def _fill(
        self,
        plan: mix.Plan,
        ok: list[Item],
        live: list[Live],
        pinned: set[str],
        rows: dict[str, Published],
        retired: list[str],
    ) -> tuple[list[str], list[str]]:
        """Publish from each slice's waiting items until it reaches its target.

        A root (an original and its versions) is live at most once. A version
        whose original is live (not pinned, not put up in this run) replaces
        it; the original's slice is refilled when its turn comes. Each slice's
        rotation (``plan.rotate``) then retires, oldest first, only as many as
        the slice is now over its target. Retirements are appended to
        ``retired``.
        """
        by_key = {it.key: it for it in ok}
        named = plan.slices[:-1]

        def slice_of(key: str) -> int | None:
            it = by_key.get(key)
            return None if it is None else mix.member(it, named)

        # Rotating files stay live until replaced, but count as already gone.
        held = [list(r) for r in plan.rotate]
        rotating = {name for r in held for name in r}
        here = [0] * len(plan.slices)
        for f in live:
            if (i := slice_of(f.key)) is not None and f.filename not in rotating:
                here[i] += 1
        showing = {f.root: f for f in live}
        fresh: set[str] = set()  # roots put up in this run
        tried: set[str] = set()
        added: list[str] = []
        failed: list[str] = []
        # A replaced original can leave an earlier slice short, so sweep again
        # until a pass adds nothing.
        progress = True
        while progress:
            progress = False
            for i, keys in enumerate(plan.fill):
                for key in keys:
                    if here[i] >= plan.targets[i]:
                        break
                    if key in tried:
                        continue
                    item = by_key[key]
                    cur = showing.get(item.root)
                    if cur is not None and not (
                        item.kind == "version"
                        and cur.key == item.root
                        and cur.key not in pinned
                        and item.root not in fresh
                    ):
                        continue
                    tried.add(key)
                    try:
                        out = self.publish_item(item)
                    except (pipeline.Rejected, Skipped) as ex:
                        log.info("%s: skipped: %s", key, ex)
                        continue
                    except Exception as ex:  # noqa: BLE001 - one bad image must not stop the run
                        log.warning("%s: render failed: %s: %s",
                                    key, type(ex).__name__, ex)  # fmt: skip
                        failed.append(key)
                        continue
                    added.append(out)
                    here[i] += 1
                    progress = True
                    if cur is not None:
                        retired += self._retire_files([rows[cur.filename]])
                        j = slice_of(cur.key)
                        if j is not None and cur.filename in held[j]:
                            held[j].remove(cur.filename)
                        elif j is not None:
                            here[j] -= 1
                    folder, filename = out.split("/", 1)
                    showing[item.root] = Live(filename, folder, key, item.root, 0.0)
                    fresh.add(item.root)
        for i, names in enumerate(held):
            over = here[i] + len(names) - plan.targets[i]
            retired += self._retire_files([rows[f] for f in names[: max(0, over)]])
        return added, failed

    def sync(self) -> list[str]:
        """Retire live files that are no longer eligible, now; adds nothing."""
        all_items = library.items(self.cat, self.photos, self.studio)
        ok = {it.key for it in library.eligible(self.cat, all_items)}
        doomed = {
            f.filename for f in library.live(self.cat, all_items) if f.key not in ok
        }
        retired = self._retire_files(
            [p for p in self.cat.published(active=True) if p.filename in doomed]
        )
        self._write_meta()
        return retired

    def publish_item(self, item: Item) -> str:
        """Render ``item`` into the upload folders; returns ``"<folder>/<filename>"``.

        Raises ``pipeline.Rejected`` (a museum work unusable once fetched:
        recorded, so it isn't tried again), ``Skipped`` (left out this run
        only), or whatever downloading or rendering raised. Brian's own
        pictures, portraits and versions are never recorded as rejected.
        """
        if item.kind == "museum":
            return self._publish_museum(item)
        try:
            im = library.source_image(item, self.http, self.data, self.crop)  # type: ignore[arg-type]
            work = item.work or stand_in(item)
            museum = item.work is not None
            # HA uploads only full/, so Brian's own pictures are padded to the
            # panel even when the pad style is "matte" (R5).
            style = self.pad_style if museum or self.pad_style != "matte" else "blur"
            out = pipeline.render_image(work, im, self.prefs, style)
        except pipeline.Rejected as ex:
            raise Skipped(str(ex)) from ex
        folder = pipeline.folder_for(out.plan, self.pad_style) if museum else "full"
        stem = (
            Path(pipeline.filename(work)).stem
            if museum
            else item.root.replace(":", "-", 1)
        )
        if item.kind != "version":
            path = self._write(out.image, folder, f"{stem}.jpg")
            self.cat.record_publish(path.name, folder, item.key, out.phash)
        else:
            v = self.cat.variant(item.variant_id)  # type: ignore[arg-type]
            n = v.n if v else item.variant_id
            path = self._write(out.image, folder, f"{stem}--{item.vtype}-{n}.jpg")
            # Published under its original's key, with the original's picture
            # hash where known, so the duplicate check still recognises it.
            self.cat.record_publish(path.name, folder, item.root,
                                    self._root_hash(item.root, out.phash),
                                    variant=item.variant_id)  # fmt: skip
        log.info("published %s as %s/%s", item.key, folder, path.name)
        return f"{folder}/{path.name}"

    def _publish_museum(self, item: Item) -> str:
        w = item.work
        assert w is not None
        # Exclusions depend on settings (the crop level), so they skip only.
        plan = pipeline.exclusion(w, self.prefs)
        if isinstance(plan, str):
            raise Skipped(plan)
        try:
            cand = pipeline.Candidate(w, plan, 0.0, self.prefs)
            out = pipeline.render(self.http, cand, self.pad_style)
        except pipeline.Rejected as ex:
            self.cat.record_reject(item.key, str(ex))
            raise
        live = [
            p.phash
            for p in self.cat.published(active=True)
            if not p.key.startswith(NON_MUSEUM)
        ]
        if any(imaging.hamming(out.phash, h) <= pipeline.PHASH_DISTANCE for h in live):
            raise Duplicate("looks like a work already on the TV")
        folder = pipeline.folder_for(out.plan, self.pad_style)
        path = self._write(out.image, folder, pipeline.filename(w))
        self.cat.record_publish(path.name, folder, item.key, out.phash)
        if out.colors:
            self.cat.set_palette(item.key, out.colors)
        log.info("published %s/%s", folder, path.name)
        return f"{folder}/{path.name}"

    def _root_hash(self, root: str, default: int) -> int:
        """The picture hash the original was last published with, else ``default``."""
        versions = self.cat.published_variants()
        prior = [
            p.phash
            for p in self.cat.published()
            if p.key == root and p.filename not in versions
        ]
        return prior[-1] if prior else default

    def _write(self, im: Image.Image, folder: str, name: str) -> Path:
        """Save under a filename never published before, so HA uploads it as new."""
        path = self.dest / folder / self.cat.unused_filename(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_atomic(im, path, self.dest / ".staging")
        return path

    def _retire_files(self, doomed: list[Published]) -> list[str]:
        """Delete these live files and note the TV ids HA must delete."""
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
        """The public index lists museum works only: photos never leave the LAN."""
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


def find_work(cat: Catalogue, key: str) -> Work | None:
    source = key.split(":", 1)[0]
    return next((w for w in cat.works(source) if w.key == key), None)


def content_index(cat: Catalogue, dest: Path) -> dict[str, Published]:
    """TV content id -> the published file it is, live or retired.

    Where two files claim an id, the first published wins.
    """
    sidecars = {f: read_sidecar(dest / f) for f in FOLDERS}
    index: dict[str, Published] = {}
    for p in cat.published():
        for cid in (p.content_id, sidecars.get(p.folder, {}).get(p.filename)):
            if cid:
                index.setdefault(cid, p)
    return index


def find_content(cat: Catalogue, dest: Path, content_id: str) -> Published | None:
    """The published file the TV knows as ``content_id``, live or retired."""
    return content_index(cat, dest).get(content_id)
