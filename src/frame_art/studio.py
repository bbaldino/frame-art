"""The Studio: painting Brian's photos in the manner of a chosen painter.

A painter's entry holds only that painter's manner. The prompt adds the rest:
the base instruction, a setting rule (keep the place, or reinvent it), the
people and restraint rules, the shape and any nudges, so every painter works
with either setting. The restraint rules are the anti-kitsch guard and go into
every prompt (see the portrait studio design).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .openrouter import nearest_aspect
from .photos import KINDS, PhotoStore, open_image
from .pipeline import slug as make_slug
from .providers import ImageProvider, PaintFailed, plain_error

log = logging.getLogger(__name__)

SETTINGS = ("keep", "reinvent")
SHAPES = ("fill", "keep")
NUDGES = ("closer to the photo", "more painterly", "simpler background", "warmer light")

BASE = (
    "Paint this photograph as {name} would have painted it: an original oil "
    "painting in their manner, not a filtered photograph. Use their palette, "
    "their way of handling light, and clearly visible brushwork and paint texture."
)
SETTING_TEXT = {
    "keep": (
        "Like the artist, simplify and edit: soften or reduce background detail "
        "and clutter, drop photographic sharpness everywhere, and let the people "
        "be the subject, even when they are small in the frame. Keep the place "
        "recognisable."
    ),
    "reinvent": (
        "The setting is yours to reinvent in the painter's own world: keep the "
        "people and what they are doing, but repaint or transform the place "
        "around them freely."
    ),
}
PEOPLE = (
    "Keep the people, their faces, expressions, poses and clothing recognisably "
    "as in the photograph."
)
RESTRAINT = (
    "Add nothing that is not there (no props, costumes, frames, borders, "
    "signatures or text), and nothing cute, glossy or theatrical: it should look "
    "like a real painting in a museum."
)
SHAPE_TEXT = {
    "fill": (
        "Paint it as a wide canvas, extending the scene naturally to the left and "
        "right; no borders."
    ),
    "keep": "",
}
RETOUCH = (
    "Keep this painting as it is, the same painter, people, composition and "
    "finish, changing only what follows."
)

DEFAULT_PAINTERS = (
    ("sargent", "John Singer Sargent", ("portrait",),
     "Fluid, confident strokes over warm dark grounds, with quick bright "
     "highlights on faces and fabric."),
    ("monet", "Claude Monet", ("portrait", "scene"),
     "Broken, visible strokes of high-key colour and shimmering outdoor light."),
    ("homer", "Winslow Homer", ("portrait", "scene"),
     "Direct, sturdy handling, clear natural light and a restrained, earthy palette."),
    ("hopper", "Edward Hopper", ("portrait",),
     "Flat, simplified planes, strong raking light and a quiet, still mood."),
    ("dali", "Salvador Dalí", ("scene",),
     "His surrealist manner, with his precise, luminous technique, deep dreamlike "
     "perspective and clear Catalan light. Let one or two quiet, strange touches "
     "grow out of the scene itself (stretched shadows, an uncanny horizon, a "
     "subtly impossible landscape) rather than pasted-on symbols: no melting "
     "clocks, elephants, ants, crutches or moustaches."),
)  # fmt: skip

SCHEMA = """
create table if not exists painters (
  slug text primary key,
  name text not null unique,
  manner text not null,
  suits text not null
);
create table if not exists studio_kv (key text primary key, value text);
create table if not exists tries (
  id integer primary key autoincrement,
  photo_id text not null,
  painter text not null,
  setting text not null,
  shape text not null,
  nudges text not null,
  from_try integer,
  prompt text not null,
  status text not null,
  error text,
  path text,
  cost real,
  made_at real not null
);
create table if not exists paintings (
  id integer primary key autoincrement,
  try_id integer not null unique,
  photo_id text,
  painter text not null,
  prompt text not null,
  path text not null,
  cost real,
  saved_at real not null
);
"""

# Unsaved tries are deleted after this long.
KEEP_TRIES = 14 * 24 * 3600
TRY_COLUMNS = (
    "id, photo_id, painter, setting, shape, nudges, from_try, prompt, status, "
    "error, path, cost, made_at"
)
PAINTING_COLUMNS = "id, try_id, photo_id, painter, prompt, path, cost, saved_at"


@dataclass(frozen=True)
class Painter:
    slug: str
    name: str
    manner: str
    suits: tuple[str, ...]


@dataclass(frozen=True)
class Try:
    id: int
    photo_id: str
    painter: str
    setting: str
    shape: str
    nudges: list[str]
    from_try: int | None
    prompt: str
    status: str  # "painting", "done" or "failed"
    error: str | None
    path: str | None
    cost: float | None
    made_at: float


@dataclass(frozen=True)
class Painting:
    id: int
    try_id: int
    photo_id: str | None  # None once the photo is deleted
    painter: str
    prompt: str
    path: str
    cost: float | None
    saved_at: float


def _try(row) -> Try:
    id, photo_id, painter, setting, shape, nudges, *rest = row
    return Try(id, photo_id, painter, setting, shape, json.loads(nudges), *rest)


def _also(nudges: list[str]) -> str:
    return f"Also: {'; '.join(nudges)}." if nudges else ""


def build_prompt(
    painter_name: str, manner: str, setting: str, shape: str, nudges: list[str]
) -> str:
    parts = [BASE.format(name=painter_name), manner, SETTING_TEXT[setting], PEOPLE,
             RESTRAINT, SHAPE_TEXT[shape], _also(nudges)]  # fmt: skip
    return " ".join(p.strip() for p in parts if p and p.strip())


def retouch_prompt(nudges: list[str]) -> str:
    return " ".join(p for p in (RETOUCH, _also(nudges), RESTRAINT) if p)


def suggested(painters: list[Painter], kind: str | None) -> list[Painter]:
    """Painters suited to ``kind`` first, then the rest; each group by name."""
    by_name = sorted(painters, key=lambda p: p.name)
    return sorted(by_name, key=lambda p: kind not in p.suits)


def _painter(row) -> Painter:
    slug, name, manner, suits = row
    return Painter(slug, name, manner, tuple(s for s in suits.split(",") if s))


def _check(name: str, suits: tuple[str, ...]) -> None:
    if not name.strip():
        raise ValueError("a painter needs a name")
    if set(suits) - set(KINDS):
        raise ValueError(f"suits must be some of {list(KINDS)}")


class StudioStore:
    def __init__(self, db_path: Path, files_dir: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=30)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        self.dir = files_dir
        self._seed()

    def _seed(self) -> None:
        """Once: the default painters, so pruning them all sticks."""
        if self.db.execute("select 1 from studio_kv where key = 'seeded'").fetchone():
            return
        with self.db:
            for slug, name, suits, manner in DEFAULT_PAINTERS:
                self.db.execute(
                    "insert or ignore into painters values (?, ?, ?, ?)",
                    (slug, name, manner, ",".join(suits)),
                )
            self.db.execute("insert into studio_kv values ('seeded', '1')")

    # -- painters --------------------------------------------------------------

    def painters(self) -> list[Painter]:
        rows = self.db.execute("select * from painters order by name")
        return [_painter(r) for r in rows]

    def painter(self, slug: str) -> Painter | None:
        row = self.db.execute(
            "select * from painters where slug = ?", (slug,)
        ).fetchone()
        return _painter(row) if row else None

    def add_painter(self, name: str, manner: str, suits: tuple[str, ...]) -> Painter:
        _check(name, suits)
        p = Painter(
            make_slug(name) or "painter", name.strip(), manner.strip(), tuple(suits)
        )
        try:
            with self.db:
                self.db.execute("insert into painters values (?, ?, ?, ?)",
                                (p.slug, p.name, p.manner, ",".join(p.suits)))  # fmt: skip
        except sqlite3.IntegrityError:
            raise ValueError(f"{p.name} is already on the list") from None
        return p

    def update_painter(
        self, slug: str, name: str, manner: str, suits: tuple[str, ...]
    ) -> None:
        _check(name, suits)
        try:
            with self.db:
                self.db.execute(
                    "update painters set name = ?, manner = ?, suits = ? where slug = ?",
                    (name.strip(), manner.strip(), ",".join(suits), slug),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"{name} is already on the list") from None

    def remove_painter(self, slug: str) -> None:
        with self.db:
            self.db.execute("delete from painters where slug = ?", (slug,))

    # -- tries -----------------------------------------------------------------

    def start_try(
        self,
        photo_id: str,
        painter: Painter,
        setting: str,
        shape: str,
        nudges: list[str],
        from_try: int | None,
    ) -> Try:
        """A new try, waiting to be painted (see ``paint``)."""
        if setting not in SETTINGS or shape not in SHAPES:
            raise ValueError("unknown setting or shape")
        if from_try is not None:
            prompt = retouch_prompt(nudges)
        else:
            prompt = build_prompt(painter.name, painter.manner, setting, shape, nudges)
        with self.db:
            cur = self.db.execute(
                "insert into tries (photo_id, painter, setting, shape, nudges, "
                "from_try, prompt, status, made_at) values (?,?,?,?,?,?,?,?,?)",
                (photo_id, painter.name, setting, shape, json.dumps(nudges), from_try,
                 prompt, "painting", time.time()),
            )  # fmt: skip
        return self.get_try(cur.lastrowid)

    def get_try(self, id: int) -> Try | None:
        row = self.db.execute(
            f"select {TRY_COLUMNS} from tries where id = ?", (id,)
        ).fetchone()
        return _try(row) if row else None

    def tries(self, photo_id: str) -> list[Try]:
        rows = self.db.execute(
            f"select {TRY_COLUMNS} from tries where photo_id = ? order by id desc",
            (photo_id,),
        )
        return [_try(r) for r in rows]

    def finish_try(self, id: int, im: Image.Image, cost: float | None) -> None:
        path = self.dir / "tries" / f"{id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        im.convert("RGB").save(tmp, "JPEG", quality=95)
        tmp.replace(path)
        with self.db:
            self.db.execute(
                "update tries set status = 'done', path = ?, cost = ? where id = ?",
                (str(path), cost, id),
            )

    def fail_try(self, id: int, error: str) -> None:
        with self.db:
            self.db.execute(
                "update tries set status = 'failed', error = ? where id = ?",
                (error, id),
            )

    def spent(self, photo_id: str) -> float:
        (total,) = self.db.execute(
            "select coalesce(sum(cost), 0) from tries where photo_id = ?", (photo_id,)
        ).fetchone()
        return total

    def saved_try_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("select try_id from paintings")}

    def delete_try(self, id: int) -> bool:
        """Delete an unsaved try and its file; False when it is already gone."""
        t = self.get_try(id)
        if t is None:
            return False
        if id in self.saved_try_ids():
            raise ValueError("that try is saved; remove it from Portraits instead")
        with self.db:
            self.db.execute("delete from tries where id = ?", (id,))
        if t.path:
            Path(t.path).unlink(missing_ok=True)
        return True

    def prune(self, now: float) -> int:
        """Delete unsaved tries older than KEEP_TRIES, files included."""
        kept = self.saved_try_ids()
        rows = self.db.execute(
            "select id, path from tries where made_at < ?", (now - KEEP_TRIES,)
        ).fetchall()
        doomed = [(id, path) for id, path in rows if id not in kept]
        with self.db:
            for id, path in doomed:
                self.db.execute("delete from tries where id = ?", (id,))
        for _, path in doomed:
            if path:
                Path(path).unlink(missing_ok=True)
        return len(doomed)

    # -- paintings --------------------------------------------------------------

    def save(self, try_id: int) -> Painting:
        """Keep a finished try as a painting: a copy of its file, no model call."""
        existing = self.db.execute(
            f"select {PAINTING_COLUMNS} from paintings where try_id = ?", (try_id,)
        ).fetchone()
        if existing:
            return Painting(*existing)
        t = self.get_try(try_id)
        if t is None or t.status != "done" or not t.path:
            raise ValueError("only a finished try can be saved")
        with self.db:
            cur = self.db.execute(
                "insert into paintings (try_id, photo_id, painter, prompt, path, "
                "cost, saved_at) values (?,?,?,?,?,?,?)",
                (t.id, t.photo_id, t.painter, t.prompt, "", t.cost, time.time()),
            )
            path = self.dir / "paintings" / f"{cur.lastrowid}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(t.path, path)
            self.db.execute("update paintings set path = ? where id = ?",
                            (str(path), cur.lastrowid))  # fmt: skip
        return self.get_painting(cur.lastrowid)

    def get_painting(self, id: int) -> Painting | None:
        row = self.db.execute(
            f"select {PAINTING_COLUMNS} from paintings where id = ?", (id,)
        ).fetchone()
        return Painting(*row) if row else None

    def paintings(self, photo_id: str | None = None) -> list[Painting]:
        """Saved paintings, newest first; only one photo's when given."""
        where, args = ("where photo_id = ?", (photo_id,)) if photo_id else ("", ())
        rows = self.db.execute(
            f"select {PAINTING_COLUMNS} from paintings {where} order by id desc", args
        )
        return [Painting(*r) for r in rows]

    def remove_painting(self, id: int) -> bool:
        p = self.get_painting(id)
        if p is None:
            return False
        with self.db:
            self.db.execute("delete from paintings where id = ?", (id,))
        Path(p.path).unlink(missing_ok=True)
        return True

    def forget_photo(self, photo_id: str) -> None:
        """The photo is gone: drop its tries, keep its paintings unlinked."""
        rows = self.db.execute(
            "select path from tries where photo_id = ?", (photo_id,)
        ).fetchall()
        with self.db:
            self.db.execute("delete from tries where photo_id = ?", (photo_id,))
            self.db.execute("update paintings set photo_id = null where photo_id = ?",
                            (photo_id,))  # fmt: skip
        for (path,) in rows:
            if path:
                Path(path).unlink(missing_ok=True)

    def counts(self) -> dict[str, tuple[int, int]]:
        """photo id -> (tries, saved paintings), for photos that have either."""
        out: dict[str, list[int]] = {}
        for pid, n in self.db.execute(
            "select photo_id, count(*) from tries group by photo_id"
        ):
            out.setdefault(pid, [0, 0])[0] = n
        for pid, n in self.db.execute(
            "select photo_id, count(*) from paintings where photo_id is not null "
            "group by photo_id"
        ):
            out.setdefault(pid, [0, 0])[1] = n
        return {k: tuple(v) for k, v in out.items()}


def paint(
    store: StudioStore, photos: PhotoStore, provider: ImageProvider, try_id: int
) -> Try | None:
    """Paint a started try. A model failure fails the try; it never raises.

    None when the try was deleted before its turn came.
    """
    t = store.get_try(try_id)
    if t is None:
        return None
    p = photos.get(t.photo_id)
    try:
        if p is None:
            raise LookupError("the photo is gone")
        if t.from_try is not None:
            earlier = store.get_try(t.from_try)
            if earlier is None or earlier.path is None:
                raise LookupError("the earlier try is gone")
            src = Image.open(earlier.path)
        else:
            src = open_image(p)
        aspect = "16:9" if t.shape == "fill" else nearest_aspect(p.width, p.height)
        out = provider.paint(src, t.prompt, aspect=aspect, size="4K")
    except (PaintFailed, LookupError, OSError) as e:
        log.warning("try %s failed: %s", try_id, plain_error(e))
        store.fail_try(try_id, plain_error(e))
    else:
        store.finish_try(try_id, out.image, out.cost)
        log.info("try %s painted (cost %s)", try_id, out.cost)
    return store.get_try(try_id)
