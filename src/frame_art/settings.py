"""Settings for the service, edited from the web UI and kept in ``settings.json``."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import imaging, pipeline
from .sources import ART_KINDS

log = logging.getLogger(__name__)

SOURCES = ("aic", "cleveland", "met", "nga", "rijks", "smk", "commons", "loc")
PHOTO_SOURCES = ("commons", "loc")
SOURCE_NAMES = {
    "aic": "Art Institute of Chicago",
    "cleveland": "Cleveland Museum of Art",
    "met": "The Met",
    "nga": "National Gallery of Art",
    "rijks": "Rijksmuseum",
    "smk": "SMK (National Gallery of Denmark)",
    "commons": "Wikimedia Commons (Featured and Quality photos)",
    "loc": "Library of Congress (Carol M. Highsmith archive)",
}

# Where the nightly run looks for new works.
CHOOSE_FROM = {
    "collection_first": "Your collection first, then automatic picks",
    "collection": "Your collection only",
    "auto": "Automatic picks only",
}

# How often the TV moves to the next picture: art_set_slideshow's presets.
SLIDESHOW = {
    "3min": "3 minutes",
    "15min": "15 minutes",
    "1h": "1 hour",
    "12h": "12 hours",
    "1d": "1 day",
    "7d": "7 days",
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
    # Style mix: the share of each night's new works that are photographs.
    photo_share: int = 0  # percent
    # Photos are mostly 3:2 (a 16% crop), so they get their own, looser level.
    photo_crop: str = "medium"
    # The pool on the TV.
    choose_from: str = "collection_first"
    # A collection work may go back on the TV this long after it was retired.
    repeat_days: int = 60
    count: int = 7  # new works per nightly run
    pool: int = 150  # works kept live
    slideshow: str = "1h"  # set on the TV by the "Show" buttons
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

    def photo_prefs(self) -> pipeline.Prefs:
        return pipeline.Prefs(
            imaging.CROP_LEVELS[self.photo_crop],
            self.full_screen_only,
            self.allow_nudes,
        )

    def crawl_kinds(self) -> tuple[str, ...]:
        """Art kinds, plus photographs whenever a photo source is enabled.

        The style mix only decides what gets published; photos are catalogued
        regardless, so they can be browsed, pinned and banned beforehand.
        """
        if set(self.sources) & set(PHOTO_SOURCES):
            return (*self.kinds, "photograph")
        return tuple(self.kinds)

    def shown_kinds(self) -> tuple[str, ...]:
        """Kinds the nightly run may publish."""
        return (*self.kinds, "photograph") if self.photo_share else tuple(self.kinds)

    def validate(self) -> list[str]:
        errors = []
        if self.crop not in imaging.CROP_LEVELS:
            errors.append(f"crop must be one of {list(imaging.CROP_LEVELS)}")
        if self.pad_style not in imaging.PAD_STYLES:
            errors.append(f"pad style must be one of {list(imaging.PAD_STYLES)}")
        if not self.kinds or set(self.kinds) - set(ART_KINDS):
            errors.append(f"kinds must be some of {list(ART_KINDS)}")
        if self.photo_crop not in imaging.CROP_LEVELS:
            errors.append(f"photo crop must be one of {list(imaging.CROP_LEVELS)}")
        if not 0 <= self.photo_share <= 100:
            errors.append("photography share must be 0-100%")
        if not self.sources or set(self.sources) - set(SOURCES):
            errors.append(f"sources must be some of {list(SOURCES)}")
        if self.choose_from not in CHOOSE_FROM:
            errors.append(f"choose from must be one of {list(CHOOSE_FROM)}")
        if not 1 <= self.repeat_days <= 3650:
            errors.append("repeat interval must be 1-3650 days")
        if not 0 <= self.count <= 25:
            # The integration's batch upload fails beyond about 25 at once.
            errors.append("new works per night must be 0-25")
        if not 1 <= self.pool <= 1000:
            errors.append("pool size must be 1-1000")
        if self.slideshow not in SLIDESHOW:
            errors.append(f"slideshow interval must be one of {list(SLIDESHOW)}")
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
        if not isinstance(raw, dict):
            log.warning("settings: %s is not an object; using defaults", path)
            return cls()
        # Keep every field that is valid on its own, so a file written by a
        # newer version (an unknown source, say) still keeps the rest.
        s = cls()
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name in ("sources", "kinds") and isinstance(value, list):
                value = [
                    v
                    for v in value
                    if v in (SOURCES if f.name == "sources" else ART_KINDS)
                ]
            trial = replace(s, **{f.name: value})
            try:
                errors = trial.validate()
            except (TypeError, ValueError):
                errors = ["wrong type"]
            if errors:
                log.warning("settings: ignoring %s=%r (%s)", f.name, value, errors)
                continue
            s = trial
        return s

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)
