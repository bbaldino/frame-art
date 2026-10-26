from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from ..model import Work
from ..net import Http


class Source(Protocol):
    name: str
    kind: str  # painting | drawing | print | photograph

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        """Yield up to ``limit`` public-domain candidates with an image."""
        ...


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
