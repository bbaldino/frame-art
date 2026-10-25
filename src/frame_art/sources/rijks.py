from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from .. import notability
from ..classify import seasons_for
from ..model import Work
from ..net import Http

log = logging.getLogger(__name__)

# The keyless Rijksmuseum data services: a Linked Art search that returns object
# ids, each of which resolves to a Linked Art record with a IIIF image.
SEARCH = "https://data.rijksmuseum.nl/search/collection"
LD = {"Accept": "application/ld+json"}
AAT_ENGLISH = "http://vocab.getty.edu/aat/300388277"
AAT_DUTCH = "http://vocab.getty.edu/aat/300388256"
AAT_ACCESSION = "http://vocab.getty.edu/aat/300312355"


def _langs(node: dict) -> set[str]:
    return {lang.get("id") for lang in node.get("language") or []}


def _title(obj: dict) -> str:
    names = [n for n in obj.get("identified_by") or [] if n.get("type") == "Name"]
    for lang in (AAT_ENGLISH, AAT_DUTCH):
        for n in names:
            if lang in _langs(n):
                return n["content"]
    return names[0]["content"] if names else "Untitled"


def _artist(obj: dict) -> str | None:
    for part in (obj.get("produced_by") or {}).get("part") or []:
        for who in part.get("carried_out_by") or []:
            # A notation is a language-tagged {"@value", "@language"} or a bare
            # string, and comes either alone or in a list.
            notes = who.get("notation") or []
            if not isinstance(notes, list):
                notes = [notes]
            for note in notes:
                if isinstance(note, dict) and note.get("@language") == "en":
                    return note["@value"]
            if notes:
                return notes[0]["@value"] if isinstance(notes[0], dict) else notes[0]
    return None


def _date(obj: dict) -> str | None:
    span = (obj.get("produced_by") or {}).get("timespan") or {}
    names = span.get("identified_by") or []
    for n in names:
        if AAT_ENGLISH in _langs(n):
            return n["content"]
    return names[0]["content"] if names else None


def _image(obj: dict) -> tuple[str, str] | None:
    """Return (iiif_base, license_url) of the object's public-domain image."""
    for subj in obj.get("subject_of") or []:
        for carrier in subj.get("digitally_carried_by") or []:
            licenses = [
                r.get("content", "") for r in carrier.get("referred_to_by") or []
            ]
            for ap in carrier.get("access_point") or []:
                url = ap.get("id", "")
                if "iiif" in url and url.endswith("/full/max/0/default.jpg"):
                    lic = next((x for x in licenses if "publicdomain" in x), None)
                    if lic:
                        return url.removesuffix("/full/max/0/default.jpg"), lic
    return None


def _object_number(obj: dict) -> str | None:
    for i in obj.get("identified_by") or []:
        if i.get("type") == "Identifier" and any(
            c.get("id") == AAT_ACCESSION for c in i.get("classified_as") or []
        ):
            return i.get("content")
    return None


def _wikidata(obj: dict) -> str | None:
    for eq in obj.get("equivalent") or []:
        if "wikidata.org" in eq.get("id", ""):
            return eq["id"].rsplit("/", 1)[-1]
    return None


class Rijks:
    name = "rijks"

    def __init__(self, cache_dir: Path, kind: str = "painting"):
        self.cache_dir = cache_dir
        self.kind = kind

    def _notable_uris(self, http: Http) -> Iterator[str]:
        """Object URIs of works with Wikipedia articles, most-linked first.

        Rijksmuseum has no highlight flag, so Wikidata notability stands in for it.
        """
        table = notability.collection_sitelinks(http, self.name, self.cache_dir)
        # Wikidata lists some works under old "SA" numbers too; the SK-/RP- forms
        # are the ones the museum's search knows.
        ranked = sorted(table.items(), key=lambda kv: kv[1][1], reverse=True)
        for inv, _ in ranked:
            page = http.get_json(
                SEARCH,
                params={
                    "objectNumber": inv,
                    "type": self.kind,
                    "imageAvailable": "true",
                },
            )
            for item in page.get("orderedItems") or []:
                yield item["id"]

    def _search_uris(self, http: Http) -> Iterator[str]:
        url: str | None = SEARCH
        params: dict | None = {"type": self.kind, "imageAvailable": "true"}
        while url:
            page = http.get_json(url, params=params)
            params = None  # the "next" link carries its own query string
            for item in page.get("orderedItems") or []:
                yield item["id"]
            url = (page.get("next") or {}).get("id")

    def iter_works(self, http: Http, limit: int) -> Iterator[Work]:
        seen: set[str] = set()
        for uris in (self._notable_uris(http), self._search_uris(http)):
            for uri in uris:
                if uri in seen:
                    continue
                seen.add(uri)
                try:
                    work = self._work(http, uri)
                except (KeyError, TypeError, ValueError, AttributeError) as ex:
                    # Linked Art records vary in shape; skip one we can't read.
                    log.warning("rijks %s: unreadable record: %r", uri, ex)
                    continue
                if work is None:
                    continue
                yield work
                limit -= 1
                if limit <= 0:
                    return

    def _work(self, http: Http, uri: str) -> Work | None:
        resp = http.request("GET", uri, headers=LD)
        if resp.status_code != 200:
            log.debug("rijks %s: HTTP %s", uri, resp.status_code)
            return None
        obj = resp.json()
        image = _image(obj)
        if image is None:
            return None
        iiif, lic = image
        info = http.request("GET", f"{iiif}/info.json")
        if info.status_code != 200:
            return None
        info = info.json()
        title = _title(obj)
        return Work(
            source=self.name,
            source_id=uri.rsplit("/", 1)[-1],
            title=title,
            artist=_artist(obj),
            date=_date(obj),
            kind=self.kind,
            license="PDM" if "mark" in lic else "CC0",
            iiif=iiif,
            width=info.get("width"),
            height=info.get("height"),
            page_url=uri,
            wikidata=_wikidata(obj),
            inventory=_object_number(obj),
            tags=seasons_for(title),
        )
