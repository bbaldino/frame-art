from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .model import Work

SCHEMA = """
create table if not exists works (
    key text primary key,
    source text not null,
    data text not null,
    crawled_at real not null
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
-- Content ids HA has confirmed deleting from the TV, and other small state.
create table if not exists deleted (
    content_id text primary key,
    deleted_at real not null
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
        rows = [(w.key, w.source, json.dumps(w.to_dict()), now) for w in works]
        with self.db:
            self.db.executemany(
                "insert into works values (?, ?, ?, ?) on conflict(key) do update set "
                "data = excluded.data, crawled_at = excluded.crawled_at",
                rows,
            )
        return len(rows)

    def works(self, source: str | None = None) -> list[Work]:
        q, args = "select data from works", ()
        if source:
            q, args = q + " where source = ?", (source,)
        return [Work.from_dict(json.loads(d)) for (d,) in self.db.execute(q, args)]

    def record_render(
        self, key: str, phash: int, mode: str, scale: float, path: Path
    ) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into rendered values (?, ?, ?, ?, ?, ?)",
                (key, f"{phash:016x}", mode, scale, str(path), time.time()),
            )

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
            if state is None:
                self.db.execute("delete from overrides where key = ?", (key,))
            else:
                self.db.execute(
                    "insert or replace into overrides values (?, ?, ?)",
                    (key, state, time.time()),
                )

    def overrides(self) -> dict[str, str]:
        return dict(self.db.execute("select key, state from overrides"))

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

    def record_publish(self, filename: str, folder: str, key: str, phash: int) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into published values (?, ?, ?, ?, ?, null, null)",
                (filename, folder, key, f"{phash:016x}", time.time()),
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
