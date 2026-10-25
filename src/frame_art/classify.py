from __future__ import annotations

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
            if word == "may" and not re.search(r"\b(in|of) may\b|\bmay \d", text, re.IGNORECASE):
                continue
            if word == "fall" and re.search(r"\bfall of\b", text, re.IGNORECASE):
                continue
            out.append(season)
            break
    return out


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
