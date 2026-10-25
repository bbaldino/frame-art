from __future__ import annotations

import logging
from collections.abc import Iterator

from ..classify import seasons_for
from ..model import Work
from ..net import Http

log = logging.getLogger(__name__)

# v1/search was retired 2026-10-01; v1.1 is paginated with offset/limit.
SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"


class Met:
    name = "met"

    def __init__(self, medium: str = "Paintings"):
        self.medium = medium

    def _ids(self, http: Http, highlight: bool) -> Iterator[int]:
        offset = 0
        while True:
            params = {
                "q": "*", "medium": self.medium, "hasImages": "true",
                "isPublicDomain": "true", "limit": 100, "offset": offset,
            }  # fmt: skip
            if highlight:
                params["isHighlight"] = "true"
            ids = http.get_json(SEARCH, params=params).get("objectIDs") or []
            if not ids:
                return
            yield from ids
            offset += len(ids)

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[int] = set()
        for highlight in (True, False):
            for oid in self._ids(http, highlight):
                if oid in seen:
                    continue
                seen.add(oid)
                work = self._work(http, oid)
                if work is None:
                    continue
                yield work
                limit -= 1
                if limit <= 0:
                    return

    def _work(self, http: Http, oid: int) -> Work | None:
        resp = http.request("GET", OBJECT.format(oid))
        if resp.status_code != 200:
            log.debug("met object %s: HTTP %s", oid, resp.status_code)
            return None
        d = resp.json()
        url = d.get("primaryImage")
        if not d.get("isPublicDomain") or not url:
            return None
        # The API doesn't report pixel sizes, so read the JPEG header.
        size = http.probe_image_size(url)
        if size is None:
            return None
        wikidata = (d.get("objectWikidata_URL") or "").rsplit("/", 1)[-1] or None
        return Work(
            source=self.name,
            source_id=str(oid),
            title=d.get("title") or "Untitled",
            artist=d.get("artistDisplayName") or None,
            date=d.get("objectDate") or None,
            kind="painting",
            license="CC0",
            image_url=url,
            width=size[0],
            height=size[1],
            page_url=d.get("objectURL"),
            wikidata=wikidata,
            highlight=bool(d.get("isHighlight")),
            tags=seasons_for(d.get("title"), *(t["term"] for t in d.get("tags") or [])),
        )
