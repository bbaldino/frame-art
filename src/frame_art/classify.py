from __future__ import annotations

import html
import re

# Words in a title or subject tag that place a work in a season. Matched on
# word boundaries, so "waterfall" doesn't count as autumn.
SEASON_WORDS = {
    "winter": [
        "winter",
        "snow",
        "snowy",
        "snowfall",
        "frost",
        "frosty",
        "ice",
        "icy",
        "skating",
        "skaters",
        "sleigh",
        "christmas",
        "december",
        "january",
        "february",
        "hiver",
        "vinter",
        "sne",
        "sneeuw",
        "ijs",
    ],
    "spring": [
        "spring",
        "blossom",
        "blossoms",
        "blossoming",
        "orchard",
        "april",
        "may",
        "easter",
        "printemps",
        "forår",
        "lente",
        "tulips",
        "cherry",
    ],
    "summer": [
        "summer",
        "beach",
        "bathers",
        "bathing",
        "seaside",
        "june",
        "july",
        "august",
        "été",
        "sommer",
        "zomer",
        "haymaking",
        "poppies",
        "wheat",
    ],
    "autumn": [
        "autumn",
        "fall",
        "harvest",
        "october",
        "november",
        "automne",
        "efterår",
        "herfst",
        "pumpkins",
        "vintage",
    ],
}

_SEASON_RE = {
    season: re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE)
    for season, words in SEASON_WORDS.items()
}


def seasons_for(*texts: str | None) -> list[str]:
    text = " ".join(t for t in texts if t)
    # "May" as a month is ambiguous with the verb and with names; only trust it
    # when the text also looks like a date ("May 1888", "in May").
    out = []
    for season, rx in _SEASON_RE.items():
        for m in rx.finditer(text):
            word = m.group(1).lower()
            if word == "may" and not re.search(
                r"\b(in|of) may\b|\bmay \d", text, re.IGNORECASE
            ):
                continue
            if word == "fall" and re.search(r"\bfall of\b", text, re.IGNORECASE):
                continue
            out.append(season)
            break
    return out


# Museum subject terms for nudity ("Nudes", "Female nude", "nudity").
_NUDE_SUBJECT = re.compile(r"\b(nudes?|nudity|naked|semi-nude)\b", re.IGNORECASE)
# Titles that name a nude outright, in the collections' languages, or a subject
# classical painters almost always painted nude. Deliberately broad: a false
# positive only drops one work from a large pool.
_NUDE_TITLE = re.compile(
    r"\b("
    r"nudes?|naked|nu|nue|nus|nues|nudo|nuda|nudi|desnudo|desnuda|naakt|naakte|nackt|nackte|"
    r"akt|aktstudie|nøgen|nøgne|"
    r"venus|vénus|bathers?|baigneuses?|badende|odalisque|odalisk|susanna|suzanna|bathsheba|"
    r"danaë|danae|leda|andromeda|galatea|lucretia|three graces|judge?ment of paris|"
    r"diana and actaeon|bath of diana|cupid and psyche|amor and psyche|adam and eve|"
    r"bacchanal|bacchanalia|nymphs?|sebastian"
    r")\b",
    re.IGNORECASE,
)


def is_nude_subject(*terms: str | None) -> bool:
    return any(t and _NUDE_SUBJECT.search(t) for t in terms)


def nude_title(title: str | None) -> bool:
    return bool(title and _NUDE_TITLE.search(title))


def current_season(month: int) -> str:
    return {
        12: "winter",
        1: "winter",
        2: "winter",
        3: "spring",
        4: "spring",
        5: "spring",
        6: "summer",
        7: "summer",
        8: "summer",
    }.get(month, "autumn")


_TAG = re.compile(r"<[^>]+>")


def plain_text(text: str | None, limit: int = 1500) -> str | None:
    """Collapse HTML or multi-line museum text to one plain paragraph, capped."""
    if not text:
        return None
    text = " ".join(html.unescape(_TAG.sub(" ", text)).split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text or None
