from __future__ import annotations

from collections.abc import Iterator

from ..classify import seasons_for
from ..model import Work
from ..net import Http

API = "https://api.smk.dk/api/v1/art/search/"
# SMK's Danish object names -> our kinds.
KINDS = {"maleri": "painting", "tegning": "drawing", "grafik": "print"}


def _title(titles: list[dict]) -> str:
    for want in ("engelsk", None):
        for t in titles:
            if want is None or t.get("language") == want:
                return t.get("title") or "Untitled"
    return "Untitled"


def _artist(production: list[dict]) -> str | None:
    for p in production:
        if p.get("creator_forename") or p.get("creator_surname"):
            return " ".join(
                x for x in (p.get("creator_forename"), p.get("creator_surname")) if x
            )
        if p.get("creator"):
            return p["creator"]
    return None


class Smk:
    name = "smk"

    def __init__(self, object_name: str = "maleri"):
        self.object_name = object_name

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[str] = set()
        # Works currently on display first: the closest thing SMK has to highlights.
        for on_display in (True, False):
            offset = 0
            while len(seen) < limit:
                filters = [
                    "[public_domain:true]", "[has_image:true]", f"[object_names:{self.object_name}]",
                ]  # fmt: skip
                if on_display:
                    filters.append("[on_display:true]")
                params = {
                    "keys": "*",
                    "filters": ",".join(filters),
                    "rows": 100,
                    "offset": offset,
                }
                items = http.get_json(API, params=params).get("items") or []
                if not items:
                    break
                for i in items:
                    sid = i["object_number"]
                    if sid in seen or not i.get("image_iiif_id"):
                        continue
                    seen.add(sid)
                    dates = i.get("production_date") or [{}]
                    yield Work(
                        source=self.name,
                        source_id=sid,
                        title=_title(i.get("titles") or []),
                        artist=_artist(i.get("production") or []),
                        date=dates[0].get("period"),
                        kind=KINDS.get(self.object_name, "painting"),
                        license="PDM",
                        iiif=i["image_iiif_id"],
                        width=i.get("image_width"),
                        height=i.get("image_height"),
                        page_url=i.get("frontend_url"),
                        highlight=bool(i.get("on_display")),
                        tags=seasons_for(
                            _title(i.get("titles") or []),
                            *(i.get("content_subject") or []),
                        ),
                    )
                    if len(seen) >= limit:
                        return
                offset += len(items)
