from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .model import Work

# Work fields stored in work_meta rather than works.data (see the schema).
META_FIELDS = ("subjects", "style", "genre", "place", "lat", "lon", "description")

SCHEMA = """
create table if not exists works (
    key text primary key,
    source text not null,
    data text not null,
    crawled_at real not null
);
-- Descriptive metadata added in 0.3.0, kept apart from works.data so that
-- older versions (whose Work rejects unknown fields) can still read the catalogue.
create table if not exists work_meta (
    key text primary key references works(key),
    data text not null
);
create table if not exists rendered (
    key text primary key references works(key),
    phash text not null,
    mode text not null,
    scale real not null,
    path text not null,
    rendered_at real not null
);
create table if not exists published (
    filename text primary key,
    folder text not null,
    key text not null references works(key),
    phash text not null,
    published_at real not null,
    retired_at real,
    content_id text
);
create table if not exists rejected (
    key text primary key references works(key),
    reason text not null,
    rejected_at real not null
);
-- Brian's choices from the web UI: 'banned' (never show, retire if live) or
-- 'pinned' (publish soon, never retire).
create table if not exists overrides (
    key text primary key references works(key),
    state text not null check (state in ('banned', 'pinned')),
    set_at real not null
);
-- Works Brian picked for the TV (0.6.0). Nightly runs draw from these first, or only.
create table if not exists collection (
    key text primary key references works(key),
    added_at real not null
);
-- What the TV showed, from HA's reports: one row per stretch on screen.
-- key is null for art frame-art didn't put there.
create table if not exists shown (
    id integer primary key,
    content_id text not null,
    key text,
    started_at real not null,
    ended_at real
);
-- Content ids HA has confirmed deleting from the TV, and other small state.
create table if not exists deleted (
    content_id text primary key,
    deleted_at real not null
);
-- Dominant colours of each rendered work, as a JSON list of hex strings.
create table if not exists palettes (
    key text primary key references works(key),
    colors text not null
);
create table if not exists kv (
    key text primary key,
    value text not null
);
create table if not exists runs (
    id integer primary key,
    kind text not null,
    started_at real not null,
    finished_at real,
    status text not null,
    summary text,
    log text
);
-- Seasonal variants (0.7.0): AI-edited copies of works, added as new tables so
-- older versions keep reading the catalogue.
create table if not exists variant_types (
    slug text primary key,
    name text not null,
    prompt text not null,
    created_at real not null
);
create table if not exists variants (
    id integer primary key,
    key text not null references works(key),
    type text not null,
    n integer not null,
    prompt text not null,
    model text not null,
    cost real,
    path text not null,
    created_at real not null,
    unique (key, type, n)
);
-- Which variant of a (work, type) is current, whether Brian vetoed it, and how
-- many nightly attempts have failed in a row.
create table if not exists variant_state (
    key text not null,
    type text not null,
    current integer,
    dont_use integer not null default 0,
    failures integer not null default 0,
    primary key (key, type)
);
-- The work -> type assignments for the next publish; manual ones survive re-planning.
create table if not exists slots (
    key text primary key,
    type text not null,
    manual integer not null
);
-- Beside published (which older versions read as-is): which variant a file is.
create table if not exists published_variants (
    filename text primary key,
    variant integer not null
);
"""


@dataclass
class Published:
    filename: str
    folder: str
    key: str
    phash: int
    published_at: float
    retired_at: float | None
    content_id: str | None


@dataclass
class VariantType:
    slug: str
    name: str
    prompt: str


@dataclass
class Variant:
    id: int
    key: str
    type: str
    n: int
    prompt: str
    model: str
    cost: float | None
    path: str
    created_at: float


@dataclass
class VariantState:
    current: int | None
    dont_use: bool
    failures: int


@dataclass
class Slot:
    type: str
    manual: bool


VARIANT_COLS = "id, key, type, n, prompt, model, cost, path, created_at"


class Catalogue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection per thread (web requests, the scheduler); WAL lets them
        # read while a run writes.
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)

    def upsert(self, works: Iterable[Work]) -> int:
        now = time.time()
        rows, meta = [], []
        for w in works:
            d = w.to_dict()
            extra = {k: d.pop(k) for k in META_FIELDS}
            rows.append((w.key, w.source, json.dumps(d), now))
            meta.append((w.key, json.dumps(extra)))
        with self.db:
            self.db.executemany(
                "insert into works values (?, ?, ?, ?) on conflict(key) do update set "
                "data = excluded.data, crawled_at = excluded.crawled_at",
                rows,
            )
            self.db.executemany("insert or replace into work_meta values (?, ?)", meta)
        return len(rows)

    def works(self, source: str | None = None) -> list[Work]:
        q = "select w.data, m.data from works w left join work_meta m using (key)"
        args: tuple = ()
        if source:
            q, args = q + " where w.source = ?", (source,)
        return [
            Work.from_dict({**json.loads(d), **json.loads(m or "{}")})
            for d, m in self.db.execute(q, args)
        ]

    def record_render(
        self, key: str, phash: int, mode: str, scale: float, path: Path
    ) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into rendered values (?, ?, ?, ?, ?, ?)",
                (key, f"{phash:016x}", mode, scale, str(path), time.time()),
            )

    def set_palette(self, key: str, colors: list[str]) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into palettes values (?, ?)",
                (key, json.dumps(colors)),
            )

    def palettes(self) -> dict[str, list[str]]:
        return {
            k: json.loads(c)
            for k, c in self.db.execute("select key, colors from palettes")
        }

    def rendered_hashes(self) -> dict[str, int]:
        return {
            k: int(h, 16) for k, h in self.db.execute("select key, phash from rendered")
        }

    def record_reject(self, key: str, reason: str) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into rejected values (?, ?, ?)",
                (key, reason, time.time()),
            )

    def rejected_keys(self) -> set[str]:
        return {k for (k,) in self.db.execute("select key from rejected")}

    def set_override(self, key: str, state: str | None) -> None:
        with self.db:
            if state == "banned":
                self.db.execute("delete from collection where key = ?", (key,))
            if state is None:
                self.db.execute("delete from overrides where key = ?", (key,))
            else:
                self.db.execute(
                    "insert or replace into overrides values (?, ?, ?)",
                    (key, state, time.time()),
                )

    def overrides(self) -> dict[str, str]:
        return dict(self.db.execute("select key, state from overrides"))

    def collect(self, key: str, on: bool = True) -> None:
        """Add a work to the collection (clearing a ban), or take it out."""
        with self.db:
            if not on:
                self.db.execute("delete from collection where key = ?", (key,))
                return
            self.db.execute(
                "delete from overrides where key = ? and state = 'banned'", (key,)
            )
            self.db.execute(
                "insert or ignore into collection values (?, ?)", (key, time.time())
            )

    def collection(self) -> dict[str, float]:
        """key -> when it was added."""
        return dict(self.db.execute("select key, added_at from collection"))

    def note_shown(self, content_id: str, key: str | None, showing: bool) -> None:
        """Record what the TV shows now, closing the previous stretch if it ended."""
        now = time.time()
        with self.db:
            row = self.db.execute(
                "select id, content_id from shown where ended_at is null "
                "order by id desc limit 1"
            ).fetchone()
            if row and showing and row[1] == content_id:
                return
            if row:
                self.db.execute(
                    "update shown set ended_at = ? where id = ?", (now, row[0])
                )
            if showing:
                self.db.execute(
                    "insert into shown (content_id, key, started_at) values (?, ?, ?)",
                    (content_id, key, now),
                )

    def shown(self, limit: int = 300) -> list[dict]:
        cols = ("id", "content_id", "key", "started_at", "ended_at")
        rows = self.db.execute(
            f"select {', '.join(cols)} from shown order by started_at desc, id desc "
            "limit ?",
            (limit,),
        )
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def rejected(self) -> dict[str, str]:
        return dict(self.db.execute("select key, reason from rejected"))

    def get(self, key: str, default=None):
        row = self.db.execute("select value from kv where key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into kv values (?, ?)", (key, json.dumps(value))
            )

    def record_deleted(self, content_id: str) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into deleted values (?, ?)",
                (content_id, time.time()),
            )

    def deleted_ids(self) -> set[str]:
        return {c for (c,) in self.db.execute("select content_id from deleted")}

    def start_run(self, kind: str) -> int:
        with self.db:
            cur = self.db.execute(
                "insert into runs (kind, started_at, status) values (?, ?, 'running')",
                (kind, time.time()),
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, summary: dict, log: str) -> None:
        with self.db:
            self.db.execute(
                "update runs set finished_at = ?, status = ?, summary = ?, log = ? "
                "where id = ?",
                (time.time(), status, json.dumps(summary), log, run_id),
            )

    def runs(self, limit: int = 30) -> list[dict]:
        cols = ("id", "kind", "started_at", "finished_at", "status", "summary", "log")
        rows = self.db.execute(
            f"select {', '.join(cols)} from runs order by id desc limit ?", (limit,)
        )
        out = []
        for row in rows:
            r = dict(zip(cols, row, strict=True))
            r["summary"] = json.loads(r["summary"]) if r["summary"] else {}
            out.append(r)
        return out

    def record_publish(
        self,
        filename: str,
        folder: str,
        key: str,
        phash: int,
        variant: int | None = None,
        published_at: float | None = None,
    ) -> None:
        """``published_at`` defaults to now; a swap passes on the replaced file's."""
        at = time.time() if published_at is None else published_at
        with self.db:
            self.db.execute(
                "insert or replace into published values (?, ?, ?, ?, ?, null, null)",
                (filename, folder, key, f"{phash:016x}", at),
            )
            if variant is not None:
                self.db.execute(
                    "insert or replace into published_variants values (?, ?)",
                    (filename, variant),
                )

    def published_variants(self) -> dict[str, int]:
        """filename -> variant id, for published files that are variants."""
        return dict(self.db.execute("select filename, variant from published_variants"))

    def unused_filename(self, name: str) -> str:
        """`name`, or the first of stem-2, stem-3, ... that was never published."""
        used = {f for (f,) in self.db.execute("select filename from published")}
        if name not in used:
            return name
        p = Path(name)
        n = 2
        while f"{p.stem}-{n}{p.suffix}" in used:
            n += 1
        return f"{p.stem}-{n}{p.suffix}"

    def add_variant_type(self, slug: str, name: str, prompt: str) -> None:
        try:
            with self.db:
                self.db.execute(
                    "insert into variant_types values (?, ?, ?, ?)",
                    (slug, name, prompt, time.time()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"variant type {slug!r} already exists") from None

    def update_variant_type(self, slug: str, name: str, prompt: str) -> None:
        with self.db:
            self.db.execute(
                "update variant_types set name = ?, prompt = ? where slug = ?",
                (name, prompt, slug),
            )

    def variant_types(self) -> list[VariantType]:
        rows = self.db.execute(
            "select slug, name, prompt from variant_types order by created_at, rowid"
        )
        return [VariantType(*r) for r in rows]

    def next_variant_n(self, key: str, type: str) -> int:
        (n,) = self.db.execute(
            "select coalesce(max(n), 0) + 1 from variants where key = ? and type = ?",
            (key, type),
        ).fetchone()
        return n

    def add_variant(
        self,
        key: str,
        type: str,
        n: int,
        prompt: str,
        model: str,
        cost: float | None,
        path: str,
    ) -> Variant:
        """Record a new variant and make it the current one (a fresh start)."""
        now = time.time()
        with self.db:
            cur = self.db.execute(
                "insert into variants (key, type, n, prompt, model, cost, path, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, type, n, prompt, model, cost, path, now),
            )
            self.db.execute(
                "insert or replace into variant_state values (?, ?, ?, 0, 0)",
                (key, type, cur.lastrowid),
            )
        return Variant(cur.lastrowid, key, type, n, prompt, model, cost, path, now)

    def variants(self, key: str | None = None) -> list[Variant]:
        q, args = f"select {VARIANT_COLS} from variants", ()
        if key is not None:
            q, args = q + " where key = ?", (key,)
        return [Variant(*r) for r in self.db.execute(q + " order by id", args)]

    def variant(self, variant_id: int) -> Variant | None:
        row = self.db.execute(
            f"select {VARIANT_COLS} from variants where id = ?", (variant_id,)
        ).fetchone()
        return Variant(*row) if row else None

    def _ensure_variant_state(self, key: str, type: str) -> None:
        self.db.execute(
            "insert or ignore into variant_state values (?, ?, null, 0, 0)", (key, type)
        )

    def set_current_variant(self, key: str, type: str, variant_id: int) -> None:
        with self.db:
            self._ensure_variant_state(key, type)
            self.db.execute(
                "update variant_state set current = ?, dont_use = 0 "
                "where key = ? and type = ?",
                (variant_id, key, type),
            )

    def set_dont_use(self, key: str, type: str, on: bool) -> None:
        with self.db:
            self._ensure_variant_state(key, type)
            self.db.execute(
                "update variant_state set dont_use = ? where key = ? and type = ?",
                (int(on), key, type),
            )

    def note_variant_failure(self, key: str, type: str) -> int:
        with self.db:
            self._ensure_variant_state(key, type)
            self.db.execute(
                "update variant_state set failures = failures + 1 "
                "where key = ? and type = ?",
                (key, type),
            )
            (n,) = self.db.execute(
                "select failures from variant_state where key = ? and type = ?",
                (key, type),
            ).fetchone()
        return n

    def variant_states(self) -> dict[tuple[str, str], VariantState]:
        rows = self.db.execute(
            "select key, type, current, dont_use, failures from variant_state"
        )
        return {(k, t): VariantState(c, bool(d), f) for k, t, c, d, f in rows}

    def usable_variants(self) -> dict[tuple[str, str], Variant]:
        """The current variant of each (work, type), unless Brian said not to use it."""
        cols = ", ".join(f"v.{c.strip()}" for c in VARIANT_COLS.split(","))
        rows = self.db.execute(
            f"select {cols} from variant_state s join variants v on v.id = s.current "
            "where s.dont_use = 0"
        )
        return {(v.key, v.type): v for v in (Variant(*r) for r in rows)}

    def slots(self) -> dict[str, Slot]:
        return {
            k: Slot(t, bool(m))
            for k, t, m in self.db.execute("select key, type, manual from slots")
        }

    def set_slots(self, slots: dict[str, Slot]) -> None:
        """Replace every slot."""
        with self.db:
            self.db.execute("delete from slots")
            self.db.executemany(
                "insert into slots values (?, ?, ?)",
                [(k, s.type, int(s.manual)) for k, s in slots.items()],
            )

    def set_slot(self, key: str, type: str, manual: bool = True) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into slots values (?, ?, ?)",
                (key, type, int(manual)),
            )

    def record_retire(self, filename: str, content_id: str | None) -> None:
        with self.db:
            self.db.execute(
                "update published set retired_at = ?, content_id = ? where filename = ?",
                (time.time(), content_id, filename),
            )

    def set_content_id(self, filename: str, content_id: str) -> None:
        with self.db:
            self.db.execute(
                "update published set content_id = ? where filename = ?",
                (content_id, filename),
            )

    def published(self, active: bool | None = None) -> list[Published]:
        q = "select * from published"
        if active is True:
            q += " where retired_at is null"
        elif active is False:
            q += " where retired_at is not null"
        rows = self.db.execute(q + " order by published_at, filename")
        return [
            Published(f, folder, k, int(h, 16), pub, ret, cid)
            for f, folder, k, h, pub, ret, cid in rows
        ]
