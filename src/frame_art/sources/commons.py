"""Wikimedia Commons: landscape photographs the community rated Featured or Quality.

Featured pictures are the strictest tier (a few thousand of ~120 million
files); Quality images are technically sound but less selective. Both are
categorised by subject, and we walk the landscape trees a few levels deep.
Licences are free (mostly CC BY-SA, some CC0/public domain); attribution is
kept in the catalogue and the published index.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator

from ..classify import seasons_for
from ..model import Work
from ..net import Http

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
# Root category -> whether files under it count as highlights (Featured).
CATEGORIES = {
    "Category:Featured pictures of landscapes": True,
    "Category:Featured night photography": True,
    "Category:Quality images of landscapes": False,
}
DEPTH = 3
META = "LicenseShortName|Artist|ObjectName|DateTimeOriginal|ImageDescription"
_TAGS = re.compile(r"<[^>]+>")


def _artist(meta: dict) -> str | None:
    """The photographer's name, without the notes some put in the author field."""
    text = _text(meta, "Artist")
    if not text:
        return None
    if m := re.match(r"This (?:photo|image) was taken by ([^.]+)\.", text, re.I):
        text = m.group(1)
    text = re.sub(r"\s*\((?:more photos|talk)\)", "", text)
    return text.split(". ", 1)[0][:80].strip() or None


def _date(meta: dict) -> str | None:
    text = _text(meta, "DateTimeOriginal") or ""
    if m := re.search(r"\b(1[89]|20)\d\d-\d\d-\d\d\b", text):
        return m.group(0)
    if m := re.search(r"\b(1[89]|20)\d\d\b", text):
        return m.group(0)
    return None


def _text(meta: dict, key: str) -> str | None:
    value = (meta.get(key) or {}).get("value")
    if not value:
        return None
    return " ".join(html.unescape(_TAGS.sub("", str(value))).split()) or None


class Commons:
    name = "commons"

    def __init__(self, kind: str = "photograph"):
        self.kind = kind

    def _subcats(self, http: Http, root: str) -> list[str]:
        out, frontier, seen = [root], [root], {root}
        for _ in range(DEPTH):
            nxt = []
            for cat in frontier:
                for m in self._members(http, cat, "subcat"):
                    if m["title"] not in seen:
                        seen.add(m["title"])
                        nxt.append(m["title"])
            out += nxt
            frontier = nxt
        return out

    def _members(self, http: Http, cat: str, cmtype: str) -> Iterator[dict]:
        params = {
            "action": "query", "format": "json", "list": "categorymembers",
            "cmtitle": cat, "cmtype": cmtype, "cmlimit": 500,
        }  # fmt: skip
        while True:
            d = http.get_json(API, params=params)
            yield from d.get("query", {}).get("categorymembers", [])
            if "continue" not in d:
                return
            params.update(d["continue"])

    def _files(self, http: Http, cat: str) -> Iterator[dict]:
        params = {
            "action": "query", "format": "json", "generator": "categorymembers",
            "gcmtitle": cat, "gcmtype": "file", "gcmlimit": 100,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": 3840, "iiextmetadatafilter": META,
        }  # fmt: skip
        while True:
            d = http.get_json(API, params=params)
            yield from d.get("query", {}).get("pages", {}).values()
            if "continue" not in d:
                return
            params.update(d["continue"])

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[int] = set()
        yielded = 0
        for root, featured in CATEGORIES.items():
            for cat in self._subcats(http, root):
                for page in self._files(http, cat):
                    if page["pageid"] in seen:
                        continue
                    seen.add(page["pageid"])
                    work = self._work(page, featured)
                    if work is None:
                        continue
                    yield work
                    yielded += 1
                    if yielded >= limit:
                        return

    def _work(self, page: dict, featured: bool) -> Work | None:
        info = (page.get("imageinfo") or [{}])[0]
        w, h = info.get("width"), info.get("height")
        if info.get("mime") != "image/jpeg" or not w or not h or w < 3840:
            return None
        meta = info.get("extmetadata") or {}
        title = _text(meta, "ObjectName") or page["title"].removeprefix("File:")
        title = re.sub(r"\.jpe?g$", "", title, flags=re.IGNORECASE)
        # A 3840px rendition is plenty unless the photo is wider than 16:9,
        # where the crop needs the full 2160px height.
        url = info["thumburl"] if w / h <= 16 / 9 else info["url"]
        description = _text(meta, "ImageDescription")
        return Work(
            source=self.name,
            source_id=str(page["pageid"]),
            title=title,
            artist=_artist(meta),
            date=_date(meta),
            kind=self.kind,
            license=_text(meta, "LicenseShortName") or "see file page",
            image_url=url,
            width=w,
            height=h,
            page_url=info.get("descriptionurl"),
            highlight=featured,
            tags=seasons_for(title, description),
        )
