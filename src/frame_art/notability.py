"""How well known a work is, measured by its Wikidata sitelink count.

Sitelinks are the number of Wikipedia (and sister) articles about the item, a
good proxy for "a piece people have heard of". Works are matched either by a
Wikidata QID the source already gives us, or by inventory number within the
museum's Wikidata collection.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .model import Work
from .net import Http

log = logging.getLogger(__name__)

SPARQL = "https://query.wikidata.org/sparql"
# Http adds the User-Agent Wikimedia requires (see net.WIKIMEDIA_UA).
HEADERS = {"Accept": "application/sparql-results+json"}
MAX_AGE = 7 * 24 * 3600

# Our source name -> the museum's Wikidata item (the P195 "collection" value).
COLLECTIONS = {
    "aic": "Q239303",
    "cleveland": "Q657415",
    "met": "Q160236",
    "nga": "Q214867",
    "rijks": "Q190804",
    "smk": "Q671384",
}


def norm_inventory(inv: str | None) -> str:
    return "".join((inv or "").split()).upper()


def _query(http: Http, sparql: str) -> list[dict]:
    resp = http.request(
        "GET", SPARQL, params={"query": sparql}, headers=HEADERS, timeout=120
    )
    if resp.status_code != 200:
        raise RuntimeError(f"WDQS HTTP {resp.status_code}")
    return resp.json()["results"]["bindings"]


def collection_sitelinks(
    http: Http, source: str, cache_dir: Path
) -> dict[str, tuple[str, int]]:
    """Map normalised inventory number -> (QID, sitelinks) for a museum's notable items."""
    qid = COLLECTIONS.get(source)
    if qid is None:
        return {}
    path = cache_dir / f"wikidata-{source}.json"
    if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return {k: tuple(v) for k, v in json.loads(path.read_text()).items()}
    sparql = (
        "SELECT ?item ?inv ?links WHERE { "
        # Inventory numbers qualified with this collection only: an item held
        # by several museums carries each museum's number.
        f"?item p:P217 ?st; wikibase:sitelinks ?links. ?st ps:P217 ?inv; pq:P195 wd:{qid}. "
        "FILTER(?links > 0) }"
    )
    try:
        rows = _query(http, sparql)
    except Exception as ex:  # noqa: BLE001 - notability is a bonus, never a blocker
        log.warning("wikidata: %s collection query failed: %s", source, ex)
        return {}
    out: dict[str, tuple[str, int]] = {}
    for r in rows:
        inv = norm_inventory(r["inv"]["value"])
        links = int(r["links"]["value"])
        if links > out.get(inv, ("", -1))[1]:
            out[inv] = (r["item"]["value"].rsplit("/", 1)[-1], links)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    log.info("wikidata: %s has %d notable items", source, len(out))
    return out


def qid_sitelinks(http: Http, qids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(qids), 200):
        values = " ".join(f"wd:{q}" for q in qids[i : i + 200])
        sparql = f"SELECT ?item ?links WHERE {{ VALUES ?item {{ {values} }} ?item wikibase:sitelinks ?links. }}"
        try:
            rows = _query(http, sparql)
        except Exception as ex:  # noqa: BLE001 - notability is a bonus, never a blocker
            log.warning("wikidata: sitelink lookup failed: %s", ex)
            continue
        for r in rows:
            out[r["item"]["value"].rsplit("/", 1)[-1]] = int(r["links"]["value"])
    return out


# Wikidata values of "depicts" (P180) or "genre" (P136) that mark a nude: the nude
# genre, nudity, female nudity, male nudity, male nude.
NUDE = ("Q40446", "Q10791", "Q121066575", "Q5804321", "Q1317896")


def nude_qids(http: Http, qids: list[str]) -> set[str]:
    out: set[str] = set()
    nude = " ".join(f"wd:{q}" for q in NUDE)
    for i in range(0, len(qids), 200):
        values = " ".join(f"wd:{q}" for q in qids[i : i + 200])
        sparql = (
            f"SELECT DISTINCT ?item WHERE {{ VALUES ?item {{ {values} }} VALUES ?n {{ {nude} }} "
            "{ ?item wdt:P180 ?n } UNION { ?item wdt:P136 ?n } }"
        )
        try:
            rows = _query(http, sparql)
        except Exception as ex:  # noqa: BLE001 - titles and museum tags still apply
            log.warning("wikidata: depicts lookup failed: %s", ex)
            continue
        out.update(r["item"]["value"].rsplit("/", 1)[-1] for r in rows)
    return out


# Wikidata property -> the facet it fills: movement, genre, depicts.
FACETS = {"P135": "movement", "P136": "genre", "P180": "depicts"}


def facets(http: Http, qids: list[str]) -> dict[str, dict[str, list[str]]]:
    """English labels for each work's movement, genre and depicted things."""
    out: dict[str, dict[str, list[str]]] = {}
    props = " ".join(f"wdt:{p}" for p in FACETS)
    for i in range(0, len(qids), 200):
        values = " ".join(f"wd:{q}" for q in qids[i : i + 200])
        sparql = (
            f"SELECT ?item ?p ?label WHERE {{ VALUES ?item {{ {values} }} "
            f"VALUES ?p {{ {props} }} ?item ?p ?v. "
            '?v rdfs:label ?label. FILTER(LANG(?label) = "en") }'
        )
        try:
            rows = _query(http, sparql)
        except Exception as ex:  # noqa: BLE001 - descriptive extras, never a blocker
            log.warning("wikidata: facet lookup failed: %s", ex)
            continue
        for r in rows:
            qid = r["item"]["value"].rsplit("/", 1)[-1]
            facet = FACETS[r["p"]["value"].rsplit("/", 1)[-1]]
            labels = out.setdefault(qid, {}).setdefault(facet, [])
            if r["label"]["value"] not in labels:
                labels.append(r["label"]["value"])
    return out


def annotate(http: Http, works: list[Work], cache_dir: Path) -> None:
    """Fill in ``wikidata`` and ``sitelinks`` on works in place, add movement,
    genre and depicted things, and tag nudes that Wikidata knows about."""
    by_source: dict[str, list[Work]] = {}
    for w in works:
        by_source.setdefault(w.source, []).append(w)
    for source, ws in by_source.items():
        table = collection_sitelinks(http, source, cache_dir)
        for w in ws:
            hit = table.get(norm_inventory(w.inventory))
            if hit:
                w.wikidata = w.wikidata or hit[0]
                w.sitelinks = hit[1]
    missing = sorted({w.wikidata for w in works if w.wikidata and not w.sitelinks})
    if missing:
        links = qid_sitelinks(http, missing)
        for w in works:
            if w.wikidata in links:
                w.sitelinks = links[w.wikidata]
    qids = sorted({w.wikidata for w in works if w.wikidata})
    found = facets(http, qids)
    for w in works:
        f = found.get(w.wikidata or "")
        if not f:
            continue
        w.style = w.style or next(iter(f.get("movement", [])), None)
        w.genre = w.genre or next(iter(f.get("genre", [])), None)
        known = {x.lower() for x in w.subjects}
        w.subjects += [x for x in f.get("depicts", [])[:15] if x.lower() not in known]
    log.info("wikidata: movement, genre or depicts for %d works", len(found))
    nudes = nude_qids(http, qids)
    for w in works:
        if w.wikidata in nudes and "nude" not in w.tags:
            w.tags.append("nude")
    log.info(
        "wikidata: %d works depict nudity", sum(1 for w in works if w.wikidata in nudes)
    )
