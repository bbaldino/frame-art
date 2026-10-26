from __future__ import annotations

from collections.abc import Iterator

from ..classify import is_nude_subject, plain_text, seasons_for
from ..model import Work
from ..net import Http

API = "https://api.artic.edu/api/v1/artworks/search"
IIIF = "https://www.artic.edu/iiif/2"
# AIC asks API clients to identify themselves with this header.
HEADERS = {"AIC-User-Agent": "frame-art (personal Frame TV art fetcher)"}

# Our kinds -> AIC artwork_type ids (14 is "Drawing and Watercolor").
KINDS = {"painting": 1, "drawing": 14, "print": 18}
FIELDS = [
    "id", "title", "artist_title", "date_display", "image_id", "thumbnail",
    "artwork_type_id", "is_boosted", "subject_titles", "main_reference_number",
    "style_title", "place_of_origin", "short_description", "description",
    "term_titles",
]  # fmt: skip


class Aic:
    name = "aic"

    def __init__(self, kind: str = "painting"):
        self.kind = kind

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        page, seen = 1, 0
        while seen < limit:
            body = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"is_public_domain": True}},
                            {"term": {"artwork_type_id": KINDS[self.kind]}},
                            {"exists": {"field": "image_id"}},
                        ]
                    }
                },
                # Boosted (highlight) works first, so a capped crawl gets the best.
                "sort": [{"is_boosted": "desc"}, {"_score": "desc"}],
                "fields": FIELDS,
                "limit": 100,
                "page": page,
            }
            data = http.post_json(API, body, headers=HEADERS)
            rows = data["data"]
            if not rows:
                return
            for r in rows:
                thumb = r.get("thumbnail") or {}
                if not r.get("image_id"):
                    continue
                seen += 1
                subjects = r.get("subject_titles") or []
                yield Work(
                    source=self.name,
                    source_id=str(r["id"]),
                    title=r.get("title") or "Untitled",
                    artist=r.get("artist_title"),
                    date=r.get("date_display"),
                    kind=self.kind,
                    license="CC0",
                    iiif=f"{IIIF}/{r['image_id']}",
                    width=thumb.get("width"),
                    height=thumb.get("height"),
                    page_url=f"https://www.artic.edu/artworks/{r['id']}",
                    inventory=r.get("main_reference_number"),
                    highlight=bool(r.get("is_boosted")),
                    tags=seasons_for(r.get("title"), *subjects)
                    + (["nude"] if is_nude_subject(*subjects) else []),
                    subjects=subjects,
                    style=r.get("style_title"),
                    place=r.get("place_of_origin"),
                    description=plain_text(
                        r.get("short_description") or r.get("description")
                    ),
                )
                if seen >= limit:
                    return
            if page >= data["pagination"]["total_pages"]:
                return
            page += 1
