from __future__ import annotations

from collections.abc import Iterator

from ..classify import seasons_for
from ..model import Work
from ..net import Http

API = "https://openaccess-api.clevelandart.org/api/artworks/"
KINDS = {"Painting": "painting", "Drawing": "drawing", "Print": "print"}


class Cleveland:
    name = "cleveland"

    def __init__(self, types: tuple[str, ...] = ("Painting",)):
        self.types = types

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[str] = set()
        # Highlights first, so a capped crawl starts with the museum's own picks.
        for highlight in (True, False):
            for type_ in self.types:
                skip = 0
                while len(seen) < limit:
                    params = {
                        "type": type_, "cc0": 1, "has_image": 1, "limit": 100, "skip": skip,
                    }  # fmt: skip
                    if highlight:
                        params["highlight"] = 1
                    rows = http.get_json(API, params=params)["data"]
                    if not rows:
                        break
                    for r in rows:
                        sid = str(r["id"])
                        if sid in seen:
                            continue
                        work = self._work(r, type_)
                        if work is None:
                            continue
                        seen.add(sid)
                        yield work
                        if len(seen) >= limit:
                            return
                    skip += len(rows)

    def _work(self, r: dict, type_: str) -> Work | None:
        images = r.get("images") or {}
        # "print" is a ~3400px JPEG; "full" is a TIFF that can top 100MB. Take
        # the print unless it is too small to fill the panel.
        best = None
        for size in ("print", "full"):
            img = images.get(size) or {}
            # Sizes arrive as strings, sometimes empty.
            dims = (img.get("width") or "", img.get("height") or "")
            if img.get("url") and all(str(d).isdigit() for d in dims):
                best = img
                if int(dims[0]) >= 3840 or int(dims[1]) >= 2160:
                    break
        if best is None:
            return None
        creators = r.get("creators") or []
        artist = creators[0]["description"].split(" (")[0] if creators else None
        return Work(
            source=self.name,
            source_id=str(r["id"]),
            title=r.get("title") or "Untitled",
            artist=artist,
            date=r.get("creation_date"),
            kind=KINDS.get(type_, "painting"),
            license="CC0",
            image_url=best["url"],
            width=int(best["width"]),
            height=int(best["height"]),
            page_url=r.get("url"),
            wikidata=r.get("wikidata"),
            highlight=bool(r.get("is_highlight")),
            tags=seasons_for(r.get("title"), r.get("description")),
        )
