"""The long-running service: a job queue, the nightly schedule, and the HA link.

Jobs run one at a time on a single worker thread, so renders stay serial and
memory stays bounded; the web UI only queues them.
"""

from __future__ import annotations

import datetime
import io
import logging
import os
import queue
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from . import imaging, jobs, keys, variants
from .catalogue import Catalogue
from .halink import HaLink
from .net import Http
from .publish import find_content, pending
from .settings import PHOTO_SOURCES, SOURCE_NAMES, Settings

log = logging.getLogger(__name__)

JOB_KINDS = {
    "nightly": "Nightly run (recrawl if stale, then publish)",
    "crawl": "Recrawl all sources",
    "crawl-photos": "Recrawl photo sources only",
    "publish": "Publish new works now",
    "compare": "Make crop comparison sheets",
    "sync": "Apply bans",
}
# Jobs that take arguments, so the web UI queues them from forms, not /jobs/{kind}.
ARG_JOBS = {
    "generate": "Generate seasonal variants",
    "versions": "Update seasonal versions on the TV",
}
# Requests the web UI can send HA for the TV (see halink).
TV_ACTIONS = {
    "upload": "Upload new works now",
    "mine": "Show frame-art's works",
    "samsung": "Back to Samsung art",
}
# TV content ids look like MY_F0123 (uploads) or SAM-S1110748 (Art Store).
CONTENT_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
# A missed nightly run (container down at run time) is caught up within this window.
CATCH_UP = 6 * 3600


@dataclass
class Job:
    kind: str
    args: dict | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None


class _JobLog(logging.Handler):
    """Collects log lines emitted on the worker thread for the current job."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        )
        self.buf: io.StringIO | None = None
        self.thread: int | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if self.buf is not None and record.thread == self.thread:
            self.buf.write(self.format(record) + "\n")


def pins_to_collection(cat: Catalogue) -> None:
    """Once, on upgrading to 0.6.0: pins meant "I want this"; that is the collection now."""
    if cat.get("pins_to_collection"):
        return
    for key, state in cat.overrides().items():
        if state == "pinned":
            cat.collect(key)
            cat.set_override(key, None)
            log.info("moved pinned %s into the collection", key)
    cat.put("pins_to_collection", True)


def _openrouter_key() -> str:
    api_key = keys.read_key("openrouter")
    if api_key is None:
        raise RuntimeError(f"no OpenRouter key in {keys.key_path('openrouter')}")
    return api_key


class Service:
    def __init__(
        self,
        data: Path,
        dest: Path,
        ha_folder: str,
        mqtt_host: str | None,
        mqtt_port: int,
        version: str,
    ):
        self.data = data
        self.dest = dest
        self.ha_folder = ha_folder
        self.version = version
        self.tz = ZoneInfo(os.environ.get("TZ", "UTC"))
        self.settings_path = data / "settings.json"
        self.compare_dir = data / "compare"
        self._queue: queue.Queue[Job] = queue.Queue()
        self._queued: list[Job] = []
        self.current: Job | None = None
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._joblog = _JobLog()
        logging.getLogger("frame_art").addHandler(self._joblog)
        self.link = (
            HaLink(
                mqtt_host,
                mqtt_port,
                self._on_ack,
                version,
                self._on_current,
                self._on_uploads,
            )
            if mqtt_host
            else None
        )
        if self.link is None:
            log.warning("no MQTT host configured; HA will not be told about new work")

    # -- plumbing --------------------------------------------------------------

    def catalogue(self) -> Catalogue:
        """A connection for the calling thread."""
        return Catalogue(self.data / "frame-art.db")

    def settings(self) -> Settings:
        return Settings.load(self.settings_path)

    def today(self) -> datetime.date:
        """The date in the service's timezone, which decides the schedules."""
        return datetime.datetime.now(self.tz).date()

    def save_settings(self, s: Settings) -> None:
        s.save(self.settings_path)
        self._wake.set()  # the run time may have changed

    def start(self) -> None:
        threading.Thread(target=self._worker, name="jobs", daemon=True).start()
        threading.Thread(target=self._scheduler, name="schedule", daemon=True).start()
        cat = self.catalogue()
        pins_to_collection(cat)
        variants.seed_types(cat)
        self.publish_state(cat)
        self.publish_now(cat)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.link:
            self.link.close()

    # -- jobs ------------------------------------------------------------------

    def submit(self, kind: str, args: dict | None = None) -> bool:
        """Queue a job unless the same one is already waiting."""
        if kind not in JOB_KINDS and kind not in ARG_JOBS:
            raise ValueError(kind)
        if any(j.kind == kind and j.args == args for j in self._queued):
            return False
        job = Job(kind, args)
        self._queued.append(job)
        self._queue.put(job)
        return True

    def queued(self) -> list[Job]:
        return list(self._queued)

    def _worker(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            self._queued.remove(job)
            job.started_at = time.time()
            self.current = job
            try:
                self._run(job)
            finally:
                self.current = None

    def _run(self, job: Job) -> None:
        cat = self.catalogue()
        run_id = cat.start_run(job.kind)
        buf = io.StringIO()
        self._joblog.buf, self._joblog.thread = buf, threading.get_ident()
        status, summary = "ok", {}
        try:
            summary = self._do(job, cat) or {}
            if summary.get("failed"):
                status = "partial"
        except Exception:
            status = "error"
            buf.write(traceback.format_exc())
            log.exception("job %s failed", job.kind)
        finally:
            self._joblog.buf = None
            cat.finish_run(run_id, status, summary, buf.getvalue())
            self.publish_state(cat)

    def _do(self, job: Job, cat: Catalogue) -> dict | None:
        kind, s = job.kind, self.settings()
        http = Http()
        if kind == "nightly":
            return jobs.nightly(cat, http, self.data, self.dest, s, self.today())
        if kind == "crawl-photos":
            photo_sources = [x for x in s.sources if x in PHOTO_SOURCES]
            if not photo_sources:
                return {"crawled": {}, "note": "no photo source enabled"}
            return jobs.crawl(
                cat,
                http,
                self.data / "cache",
                ("photograph",),
                s.crawl_limit,
                photo_sources,
            )
        if kind == "crawl":
            return jobs.crawl(
                cat,
                http,
                self.data / "cache",
                s.crawl_kinds(),
                s.crawl_limit,
                s.sources,
            )
        if kind == "publish":
            res = jobs.publish(cat, http, self.dest, s)
            return {"added": res.added, "retired": res.retired, "failed": res.failed}
        if kind == "compare":
            sheets = jobs.compare(cat, http, self.compare_dir, s)
            return {"sheets": [p.name for p in sheets]}
        if kind == "sync":
            return {"retired": jobs.publisher(cat, http, self.dest, s).sync()}
        if kind == "generate":
            return self._generate(cat, s, **job.args)
        if kind == "versions":
            pub = jobs.publisher(cat, http, self.dest, s)
            return variants.apply_versions(
                cat, http, self.data, pub, keys=set(job.args["keys"])
            )
        raise ValueError(kind)

    def _generate(
        self,
        cat: Catalogue,
        s: Settings,
        type: str,
        keys: list[str],
        prompt: str | None,
    ) -> dict:
        """Make a ``type`` variant of each work, by hand (outside the nightly limit)."""
        api_key = _openrouter_key()
        http = Http(timeout=300)  # edits take a minute or more each
        pub = jobs.publisher(cat, http, self.dest, s)
        live = {p.key for p in cat.published(active=True)}
        today = s.today_shares(self.today(), {t.slug for t in cat.variant_types()})
        generated: list[str] = []
        failed: list[str] = []
        cost = 0.0
        for key in keys:
            try:
                v = variants.generate(
                    cat, http, self.data, api_key, pub, key, type, prompt
                )
            except Exception as ex:  # noqa: BLE001 - one bad work must not stop the rest
                log.warning(
                    "%s: could not make a %s variant: %s: %s",
                    key, type, ex.__class__.__name__, ex,
                )  # fmt: skip
                failed.append(key)
                continue
            generated.append(key)
            cost += v.cost or 0.0
            # Made by hand, so show it, even past the type's share.
            if key in live and type in today:
                cat.set_slot(key, type, manual=True)
        res = variants.apply_versions(cat, http, self.data, pub, keys=set(generated))
        failed += [k for k in res["failed"] if k not in failed]
        return {"generated": generated, "failed": failed, "cost": round(cost, 4)}

    # -- schedule --------------------------------------------------------------

    def next_run(self, now: datetime.datetime | None = None) -> datetime.datetime:
        now = now or datetime.datetime.now(self.tz)
        hh, mm = map(int, self.settings().run_at.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return at if at > now else at + datetime.timedelta(days=1)

    def _due(self, cat: Catalogue) -> bool:
        """Has today's run time passed without a nightly run since?"""
        now = datetime.datetime.now(self.tz)
        last_due = self.next_run(now) - datetime.timedelta(days=1)
        if (now - last_due).total_seconds() > CATCH_UP:
            return False
        started = cat.get("last_nightly", 0)
        return started < last_due.timestamp()

    def _scheduler(self) -> None:
        cat = self.catalogue()
        while not self._stop.is_set():
            if not self.settings().paused and self._due(cat):
                cat.put("last_nightly", time.time())
                log.info("schedule: starting the nightly run")
                self.submit("nightly")
            wait = (self.next_run() - datetime.datetime.now(self.tz)).total_seconds()
            self._wake.wait(timeout=min(max(wait, 1), 300))
            self._wake.clear()

    # -- Home Assistant --------------------------------------------------------

    def publish_state(self, cat: Catalogue | None = None) -> dict:
        """Tell HA what to do next. ``seq`` goes up only when there is new work."""
        cat = cat or self.catalogue()
        with self._state_lock:  # worker, MQTT and web threads all call this
            todo = pending(cat, self.dest)
            work = sorted(todo["to_upload"]) + sorted(todo["retire"])
            seq = cat.get("ha_seq", 0)
            if set(work) - set(cat.get("ha_work", [])):
                seq += 1
                cat.put("ha_seq", seq)
            cat.put("ha_work", work)
            state = {
                "seq": seq,
                "pending": bool(work),
                "to_upload": len(todo["to_upload"]),
                "retire": todo["retire"],
                "live": todo["live"],
                "updated": datetime.datetime.now(self.tz).isoformat(timespec="seconds"),
            }
            if self.link:
                self.link.publish(state)
        return state

    def _on_ack(self, payload: dict) -> None:
        cat = self.catalogue()
        if cid := payload.get("deleted"):
            log.info("ha: deleted %s from the TV", cid)
            cat.record_deleted(str(cid))
            self._sync_progress(cat, deleted=1)
        if "sync" in payload:
            self._on_sync(cat, payload["sync"])
            return
        if "command" in payload:
            self._on_command(cat, payload["command"])
            return
        if "upload" in payload:
            result = payload["upload"] if isinstance(payload["upload"], dict) else {}
            log.info("ha: upload result %s", result)
            cat.put("last_upload", {"at": time.time(), "result": result})
            uploaded = len(result.get("uploaded") or [])
            failed = len(result.get("failed") or [])
            progress = {"uploaded": uploaded, "failed": failed}
            if "error" in result:
                progress["error"] = str(result["error"])
            else:
                # The real count: HA's guess at the start can differ from what
                # art_upload_batch found new in the folder.
                progress["total"] = uploaded + failed
            self._sync_progress(cat, **progress)
        self.publish_state(cat)
        # A fresh upload may be what the TV is already showing.
        self.publish_now(cat)

    def tv_command(self, action: str) -> dict:
        """Ask HA to act on the TV; the outcome arrives later on the ack topic."""
        cat = self.catalogue()
        cmd: dict = {"id": time.time_ns() // 1_000_000, "action": action}
        if action != "upload":
            cmd["duration"] = self.settings().slideshow
        # Recorded before sending: HA can answer within milliseconds.
        record = {**cmd, "at": time.time(), "status": "sent"}
        cat.put("tv_command", record)
        if not (self.link and self.link.command(cmd)):
            log.info("ha: could not send %s", cmd)
            record = {**record, "status": "unsent"}
            cat.put("tv_command", record)
            return record
        log.info("ha: asked for %s", cmd)
        return cat.get("tv_command")

    def _on_command(self, cat: Catalogue, reply) -> None:
        last = cat.get("tv_command") or {}
        if not isinstance(reply, dict) or reply.get("id") != last.get("id"):
            return  # a stale reply, or someone else's request
        error = reply.get("error")
        log.info("ha: %s: %s", last.get("action"), error or "done")
        status = "failed" if error else "done"
        cat.put("tv_command", {**last, "status": status, "error": error and str(error)})

    def _on_sync(self, cat: Catalogue, report) -> None:
        """Progress of HA's upload automation, for the web UI."""
        if not isinstance(report, dict):
            return
        phase, now = report.get("phase"), time.time()
        total = report.get("total")
        total = total if isinstance(total, int) and total >= 0 else None
        if phase == "uploading":
            log.info("ha: uploading %s works", total)
            cat.put(
                "tv_sync", {"phase": phase, "total": total, "started": now, "at": now}
            )
        elif phase in ("deleting", "done"):
            last = cat.get("tv_sync") or {"started": now}
            sync = {**last, "phase": phase, "at": now}
            if phase == "deleting":
                sync |= {"delete_total": total, "deleted": 0}
            cat.put("tv_sync", sync)

    def _sync_progress(self, cat: Catalogue, deleted: int = 0, **result) -> None:
        sync = cat.get("tv_sync")
        if not sync or sync["phase"] == "done":
            return  # an automation that doesn't report progress
        if deleted:
            sync["deleted"] = sync.get("deleted", 0) + deleted
        sync.update(result)
        cat.put("tv_sync", {**sync, "at": time.time()})

    def _on_uploads(self, payload: dict) -> None:
        enabled = payload.get("enabled")
        if isinstance(enabled, bool):
            # Retained: it can outlive the automation, so it's "last known".
            self.catalogue().put(
                "uploads_enabled", {"enabled": enabled, "at": time.time()}
            )

    def _on_current(self, payload: dict) -> None:
        cid = payload.get("content_id")
        if not isinstance(cid, str) or not CONTENT_ID.fullmatch(cid):
            log.warning("ha: ignoring current artwork %r", payload)
            return
        cat = self.catalogue()
        prev = cat.get("tv_current") or {}
        # "since" is when this artwork came up, not when HA last re-sent it.
        since = prev.get("at") if prev.get("content_id") == cid else time.time()
        on = payload.get("on")
        on = on if isinstance(on, bool) else None
        cat.put("tv_current", {"content_id": cid, "at": since, "on": on})
        p = find_content(cat, self.dest, cid)
        # An unknown power state counts as on: HA only reports while it can see the TV.
        cat.note_shown(cid, p.key if p else None, showing=on is not False)
        self.publish_now(cat)

    def variant_labels(self, cat: Catalogue) -> dict[str, dict]:
        """filename -> {"type": <type name>, "n": <n>} for published variant files."""
        names = {t.slug: t.name for t in cat.variant_types()}
        labels = {}
        for filename, vid in cat.published_variants().items():
            v = cat.variant(vid)
            if v:
                labels[filename] = {"type": names.get(v.type, v.type), "n": v.n}
        return labels

    def now_showing(self, cat: Catalogue | None = None) -> dict:
        """What the TV shows, as far as frame-art knows it."""
        cat = cat or self.catalogue()
        current = cat.get("tv_current") or {}
        cid = current.get("content_id")
        info: dict = {
            "content_id": cid,
            "since": current.get("at"),
            # Whether the screen is on (TV or Art Mode); None if HA hasn't said.
            "tv_on": current.get("on"),
            "ours": False,
        }
        p = find_content(cat, self.dest, cid) if cid else None
        w = next((w for w in cat.works() if p and w.key == p.key), None)
        if p is None or w is None:
            return info
        info.update(
            ours=True,
            key=w.key,
            file=f"{p.folder}/{p.filename}",
            title=w.title,
            artist=w.artist,
            date=w.date,
            kind=w.kind,
            source=SOURCE_NAMES.get(w.source, w.source),
            license=w.license,
            url=w.page_url,
            style=w.style,
            genre=w.genre,
            place=w.place,
            lat=w.lat,
            lon=w.lon,
            description=w.description,
            subjects=w.subjects[:15],
            colors=self._palette(cat, w.key, self.dest / p.folder / p.filename),
        )
        variant = self.variant_labels(cat).get(p.filename)
        if variant:
            info["variant"] = variant
        return info

    def _palette(self, cat: Catalogue, key: str, path: Path) -> list[str]:
        """The work's palette, computed from its render if it predates palettes."""
        colors = cat.palettes().get(key)
        if colors is None and path.exists():
            try:
                with Image.open(path) as im:
                    im.draft("RGB", (480, 270))
                    colors = imaging.palette(im)
                cat.set_palette(key, colors)
            except OSError as ex:
                log.warning("palette for %s: %s", key, ex)
        return colors or []

    def publish_now(self, cat: Catalogue | None = None) -> dict:
        info = self.now_showing(cat)
        if self.link:
            self.link.publish_now(info)
        return info
