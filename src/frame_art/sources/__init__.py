from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..model import Work
from ..net import Http


class Source(Protocol):
    name: str
    kind: str  # painting | drawing | print | photograph

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        """Yield up to ``limit`` public-domain candidates with an image."""
        ...


@dataclass(frozen=True)
class SourceInfo:
    """What kind of source this is, for the parts of the app that must tell."""

    name: str
    label: str
    mode: Literal["crawled", "picked"]  # crawled on a schedule, or picked by Brian
    private: bool  # personal: never in the catalogue, crawls or public pages
    needs_login: bool


# Settings re-exports these; they live here so the registry can use them.
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

SOURCE_INFO: dict[str, SourceInfo] = {
    **{
        n: SourceInfo(n, label, "crawled", private=False, needs_login=False)
        for n, label in SOURCE_NAMES.items()
    },
    "upload": SourceInfo(
        "upload", "Uploaded", "picked", private=True, needs_login=False
    ),
    "google-photos": SourceInfo(
        "google-photos", "Google Photos", "picked", private=True, needs_login=True
    ),
}


def info(name: str) -> SourceInfo:
    return SOURCE_INFO[name]


ART_KINDS = ("painting", "drawing", "print")
KINDS = (*ART_KINDS, "photograph")


def all_sources(
    cache_dir: Path, kinds: tuple[str, ...] = ("painting",)
) -> list[Source]:
    """One adapter instance per (source, kind)."""
    from . import aic, cleveland, commons, loc, met, nga, rijks, smk

    out: list[Source] = []
    if "photograph" in kinds:
        out += [commons.Commons(), loc.Loc()]
    for kind in kinds:
        if kind not in ART_KINDS:
            continue
        out += [
            aic.Aic(kind),
            cleveland.Cleveland(kind),
            met.Met(kind),
            nga.Nga(cache_dir, kind),
            rijks.Rijks(cache_dir, kind),
            smk.Smk(kind),
        ]
    return out
