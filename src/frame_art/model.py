from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Work:
    """One candidate artwork, normalised across sources."""

    source: str
    source_id: str
    title: str
    artist: str | None
    date: str | None
    kind: str  # painting | drawing | print
    license: str
    # Either a IIIF image service base (preferred: we can ask for an exact size)
    # or a direct URL to the largest image the source offers.
    iiif: str | None = None
    image_url: str | None = None
    width: int | None = None
    height: int | None = None
    page_url: str | None = None
    wikidata: str | None = None
    inventory: str | None = None  # museum accession / object number
    sitelinks: int = 0  # Wikipedia articles about the work (notability)
    highlight: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Work:
        return cls(**d)
