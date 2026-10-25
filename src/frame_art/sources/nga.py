from __future__ import annotations

import csv
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from ..classify import is_nude_subject, seasons_for
from ..model import Work
from ..net import Http

log = logging.getLogger(__name__)

# NGA publishes its open data as CSVs on GitHub (~90MB each); cache them.
RAW = "https://github.com/NationalGalleryOfArt/opendata/raw/main/data/{}.csv"
MAX_AGE = 7 * 24 * 3600
KINDS = {"painting": "Painting", "drawing": "Drawing", "print": "Print"}

csv.field_size_limit(2**31 - 1)
# One adapter per kind crawls in parallel; only one of them should download a CSV.
_download_lock = threading.Lock()


class Nga:
    name = "nga"

    def __init__(self, cache_dir: Path, kind: str = "painting"):
        self.cache_dir = cache_dir / "nga"
        self.kind = kind

    def _csv(self, http: Http, name: str) -> Path:
        path = self.cache_dir / f"{name}.csv"
        with _download_lock:
            if not path.exists() or time.time() - path.stat().st_mtime > MAX_AGE:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                log.info("nga: downloading %s.csv", name)
                tmp = path.with_suffix(".part")
                http.download(RAW.format(name), tmp)
                tmp.replace(path)
        return path

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        images: dict[str, dict] = {}
        with open(self._csv(http, "published_images"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["viewtype"] == "primary" and row["openaccess"] == "1":
                    images[row["depictstmsobjectid"]] = row

        # Keyword and theme terms ("Female nude", ...), only the ones we filter on.
        nudes: set[str] = set()
        with open(self._csv(http, "objects_terms"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["termtype"] in ("Keyword", "Theme") and is_nude_subject(row["term"]):
                    nudes.add(row["objectid"])

        works = []
        with open(self._csv(http, "objects"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["classification"] != KINDS[self.kind]:
                    continue
                img = images.get(row["objectid"])
                if img is None:
                    continue
                works.append(self._work(row, img, row["objectid"] in nudes))
        # Works with a gallery location are on view: NGA's nearest thing to highlights.
        works.sort(key=lambda w: not w.highlight)
        yield from works[:limit]

    def _work(self, row: dict, img: dict, nude: bool) -> Work:
        width, height = int(img["width"]), int(img["height"])
        if img.get("maxpixels"):
            cap = int(img["maxpixels"])
            scale = min(1.0, cap / max(width, height))
            width, height = int(width * scale), int(height * scale)
        return Work(
            source=self.name,
            source_id=row["objectid"],
            title=row["title"] or "Untitled",
            artist=row["attribution"] or None,
            date=row["displaydate"] or None,
            kind=self.kind,
            license="CC0",
            iiif=img["iiifurl"],
            width=width,
            height=height,
            page_url=f"https://www.nga.gov/collection/art-object-page.{row['objectid']}.html",
            wikidata=row.get("wikidataid") or None,
            inventory=row.get("accessionnum"),
            highlight=bool(row.get("locationid")),
            tags=seasons_for(row["title"]) + (["nude"] if nude else []),
        )
