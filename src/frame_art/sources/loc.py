"""Library of Congress: Carol M. Highsmith's photographs of America.

Highsmith gave her archive to the public, so it is public domain. Older items
have a IIIF image service; newer ones only have TIFF masters (60-120 MB), whose
pixel size we read from the file header and which are downloaded whole. Most are
4:3 or 3:2, so they suit the medium or loose photo crop levels.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator

from ..classify import seasons_for
from ..model import Work
from ..net import Http

log = logging.getLogger(__name__)

SEARCH = "https://www.loc.gov/collections/carol-m-highsmith/"
# The collection is ~70,000 photos of everything; these searches find the scenery.
QUERIES = (
    "landscape", "national park", "mountains", "canyon", "lake", "waterfall",
    "coast", "desert", "river", "forest", "sunset", "valley", "prairie",
)  # fmt: skip
IIIF = re.compile(r"^(https://tile\.loc\.gov/image-services/iiif/[^/]+)/full/")
SERVICE_JPG = re.compile(r"/storage-services/service/(.+?)v\.jpg$")
LICENSE = "Public domain (Carol M. Highsmith gift)"
# "landscape" also finds gardens and yards.
NOT_SCENERY = re.compile(r"\blandscap(?:ed|ing)\b", re.IGNORECASE)


def master_tiff(image_urls: list[str]) -> str | None:
    """The uncompressed TIFF master for an item that has no IIIF service."""
    for url in image_urls:
        if m := SERVICE_JPG.search(url.split("#", 1)[0]):
            return f"https://tile.loc.gov/storage-services/master/{m.group(1)}u.tif"
    return None


def iiif_base(image_urls: list[str]) -> str | None:
    for url in image_urls:
        if m := IIIF.match(url):
            return m.group(1)
    return None


class Loc:
    name = "loc"

    def __init__(self, kind: str = "photograph"):
        self.kind = kind

    def _results(self, http: Http, query: str) -> Iterator[dict]:
        page = 1
        while True:
            params = {"fo": "json", "q": query, "c": 100, "sp": page,
                      "at": "results,pagination"}  # fmt: skip
            d = http.get_json(SEARCH, params=params, timeout=120)
            yield from d.get("results") or []
            if not (d.get("pagination") or {}).get("next"):
                return
            page += 1

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[str] = set()
        yielded = 0
        for query in QUERIES:
            for r in self._results(http, query):
                rid = r.get("id") or ""
                if rid in seen or r.get("access_restricted"):
                    continue
                seen.add(rid)
                work = self._work(http, r)
                if work is None:
                    continue
                yield work
                yielded += 1
                if yielded >= limit:
                    return

    def _work(self, http: Http, r: dict) -> Work | None:
        if NOT_SCENERY.search(r.get("title") or ""):
            return None
        urls = r.get("image_url") or []
        iiif = iiif_base(urls)
        image_url = None
        if iiif:
            resp = http.request("GET", f"{iiif}/info.json")
            if resp.status_code != 200:
                return None
            info = resp.json()
            size = info.get("width"), info.get("height")
        else:
            image_url = master_tiff(urls)
            size = image_url and http.probe_image_size(image_url)
        if not size or not size[0] or not size[1] or max(size) < 3840:
            return None
        sid = rid_from(r.get("id") or "")
        if not sid:
            return None
        title = r.get("title") or "Untitled"
        subjects = r.get("subject") or []
        return Work(
            source=self.name,
            source_id=sid,
            title=title,
            artist="Carol M. Highsmith",
            date=(r.get("date") or "")[:4] or None,
            kind=self.kind,
            license=LICENSE,
            iiif=iiif,
            image_url=image_url,
            width=size[0],
            height=size[1],
            page_url=r.get("id"),
            tags=seasons_for(title, *subjects),
            subjects=[x for x in subjects if x not in GENERIC_SUBJECTS],
            place=_place(r),
        )


# LoC subject headings every Highsmith photo carries.
GENERIC_SUBJECTS = {"color", "digital photographs", "united states", "america"}


def _place(r: dict) -> str | None:
    parts = [(r.get(k) or [None])[0] for k in ("location_city", "location_state")]
    return ", ".join(p.title() for p in parts if p) or None


def rid_from(url: str) -> str | None:
    """The LCCN from an item URL like https://www.loc.gov/item/2018703046/."""
    m = re.search(r"/item/([^/]+)/?$", url)
    return m.group(1) if m else None
