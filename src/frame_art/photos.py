"""Brian's own photos: kept private, apart from the museum catalogue.

They live in their own table and folder, never in ``works``, so crawls,
publishing, retirement and the catalogue pages never see them. A file's name
on disk is a random id; the name it came with is for display only.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

KINDS = ("portrait", "scene")
# Pillow's format name -> the extension a stored file gets.
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

SCHEMA = """
create table if not exists photos (
  id text primary key,
  source text not null,
  source_id text not null,
  sha256 text not null unique,
  filename text not null,
  path text not null,
  width integer not null,
  height integer not null,
  added real not null,
  kind text,
  kind_reason text,
  kind_auto integer not null default 0,
  unique (source, source_id)
);
"""

COLUMNS = (
    "id, source, source_id, sha256, filename, path, width, height, added, "
    "kind, kind_reason, kind_auto"
)


@dataclass
class Photo:
    id: str
    source: str
    source_id: str
    sha256: str
    filename: str
    path: str
    width: int
    height: int
    added: float
    kind: str | None
    kind_reason: str | None
    kind_auto: bool


def _photo(row) -> Photo:
    *rest, auto = row
    return Photo(*rest, kind_auto=bool(auto))


def open_image(p: Photo) -> Image.Image:
    """The photo the right way up (EXIF orientation applied)."""
    return ImageOps.exif_transpose(Image.open(p.path))


class PhotoStore:
    def __init__(self, db_path: Path, files_dir: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=30)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        self.dir = files_dir

    def add(
        self, source: str, source_id: str, data: bytes, filename: str
    ) -> Photo | None:
        """Store a new photo; None if it is already here (same id or same bytes).

        Raises ValueError for anything that isn't a JPEG, PNG or WebP image.
        """
        sha = hashlib.sha256(data).hexdigest()
        seen = self.db.execute(
            "select 1 from photos where (source = ? and source_id = ?) or sha256 = ?",
            (source, source_id, sha),
        ).fetchone()
        if seen:
            return None
        try:
            im = Image.open(io.BytesIO(data))
            fmt = im.format
            w, h = ImageOps.exif_transpose(im).size
        except Exception as e:
            raise ValueError("not a photo") from e
        if fmt not in FORMATS:
            raise ValueError("not a photo")
        pid = secrets.token_hex(8)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{pid}.{FORMATS[fmt]}"
        path.write_bytes(data)
        p = Photo(pid, source, source_id, sha, filename, str(path), w, h,
                  time.time(), None, None, False)  # fmt: skip
        with self.db:
            self.db.execute(
                f"insert into photos ({COLUMNS}) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.id, p.source, p.source_id, p.sha256, p.filename, p.path,
                 p.width, p.height, p.added, None, None, 0),
            )  # fmt: skip
        return p

    def all(self) -> list[Photo]:
        rows = self.db.execute(
            f"select {COLUMNS} from photos order by added desc, rowid desc"
        )
        return [_photo(r) for r in rows]

    def get(self, id: str) -> Photo | None:
        row = self.db.execute(
            f"select {COLUMNS} from photos where id = ?", (id,)
        ).fetchone()
        return _photo(row) if row else None

    def set_kind(
        self, id: str, kind: str | None, reason: str | None, auto: bool
    ) -> None:
        if kind is not None and kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        with self.db:
            self.db.execute(
                "update photos set kind = ?, kind_reason = ?, kind_auto = ? where id = ?",
                (kind, reason, int(auto), id),
            )

    def remove(self, id: str) -> bool:
        p = self.get(id)
        if p is None:
            return False
        with self.db:
            self.db.execute("delete from photos where id = ?", (id,))
        Path(p.path).unlink(missing_ok=True)
        return True
