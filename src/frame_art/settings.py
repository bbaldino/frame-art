"""Settings for the service, edited from the web UI and kept in ``settings.json``."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import imaging, pipeline
from .sources import KINDS

log = logging.getLogger(__name__)

SOURCES = ("aic", "cleveland", "met", "nga", "rijks", "smk")
SOURCE_NAMES = {
    "aic": "Art Institute of Chicago",
    "cleveland": "Cleveland Museum of Art",
    "met": "The Met",
    "nga": "National Gallery of Art",
    "rijks": "Rijksmuseum",
    "smk": "SMK (National Gallery of Denmark)",
}


@dataclass
class Settings:
    # Which works, and how they fill the screen.
    crop: str = "strict"
    full_screen_only: bool = False
    allow_nudes: bool = False
    pad_style: str = "blur"
    kinds: list[str] = field(default_factory=lambda: ["painting"])
    sources: list[str] = field(default_factory=lambda: list(SOURCES))
    # The pool on the TV.
    count: int = 7  # new works per nightly run
    pool: int = 150  # works kept live
    # Schedule.
    run_at: str = "02:00"  # local time, HH:MM
    paused: bool = False
    recrawl_days: float = 7
    # Works per source and kind. 2000 takes ~40 min and gives ~660 full-screen
    # paintings at the strict crop level (AIC caps its search at 1000).
    crawl_limit: int = 2000

    def prefs(self) -> pipeline.Prefs:
        return pipeline.Prefs(
            imaging.CROP_LEVELS[self.crop], self.full_screen_only, self.allow_nudes
        )

    def validate(self) -> list[str]:
        errors = []
        if self.crop not in imaging.CROP_LEVELS:
            errors.append(f"crop must be one of {list(imaging.CROP_LEVELS)}")
        if self.pad_style not in imaging.PAD_STYLES:
            errors.append(f"pad style must be one of {list(imaging.PAD_STYLES)}")
        if not self.kinds or set(self.kinds) - set(KINDS):
            errors.append(f"kinds must be some of {list(KINDS)}")
        if not self.sources or set(self.sources) - set(SOURCES):
            errors.append(f"sources must be some of {list(SOURCES)}")
        if not 0 <= self.count <= 25:
            # The integration's batch upload fails beyond about 25 at once.
            errors.append("new works per night must be 0-25")
        if not 1 <= self.pool <= 1000:
            errors.append("pool size must be 1-1000")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.run_at):
            errors.append("run time must be HH:MM (24-hour)")
        if not 1 <= self.recrawl_days <= 365:
            errors.append("recrawl interval must be 1-365 days")
        if not 10 <= self.crawl_limit <= 10000:
            errors.append("crawl depth must be 10-10000")
        return errors

    @classmethod
    def load(cls, path: Path) -> Settings:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as ex:
            log.warning("settings: %s unreadable (%s); using defaults", path, ex)
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in raw.items() if k in known})
        if errors := s.validate():
            log.warning("settings: %s invalid (%s); using defaults", path, errors)
            return cls()
        return s

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)
