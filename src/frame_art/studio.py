"""The Studio: painting Brian's photos in the manner of a chosen painter.

A painter's entry holds only that painter's manner. The prompt adds the rest:
the base instruction, a setting rule (keep the place, or reinvent it), the
people and restraint rules, the shape and any nudges, so every painter works
with either setting. The restraint rules are the anti-kitsch guard and go into
every prompt (see the portrait studio design).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .photos import KINDS
from .pipeline import slug as make_slug

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
"""


@dataclass(frozen=True)
class Painter:
    slug: str
    name: str
    manner: str
    suits: tuple[str, ...]


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
