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
        self.db = sqlite3.connect(path)
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

    def record_render(self, key: str, phash: int, mode: str, scale: float, path: Path) -> None:
        with self.db:
            self.db.execute(
                "insert or replace into rendered values (?, ?, ?, ?, ?, ?)",
                (key, f"{phash:016x}", mode, scale, str(path), time.time()),
            )

    def rendered_hashes(self) -> dict[str, int]:
        return {k: int(h, 16) for k, h in self.db.execute("select key, phash from rendered")}

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
