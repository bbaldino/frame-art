from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from ..model import Work
from ..net import Http


class Source(Protocol):
    name: str

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        """Yield up to ``limit`` public-domain candidates with an image."""
        ...


def all_sources(cache_dir: Path) -> list[Source]:
    from . import aic, cleveland, met, nga, rijks, smk

    return [
        aic.Aic(),
        cleveland.Cleveland(),
        met.Met(),
        nga.Nga(cache_dir),
        rijks.Rijks(),
        smk.Smk(),
    ]
