from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import classify, imaging
from .model import Work
from .net import Http

log = logging.getLogger(__name__)

# Reject works that would need more upscaling than this to fill their slot.
MAX_UPSCALE = 1.35
# Perceptual hashes this close are treated as the same picture.
PHASH_DISTANCE = 8
# Round works are skipped; titles catch some before download, the scan-card
# check in render() catches the rest.
TONDO = re.compile(r"\btondo\b", re.IGNORECASE)


@dataclass(frozen=True)
class Prefs:
    """Which works are wanted and how they are fitted to the screen."""

    max_crop: float = imaging.CROP_LEVELS["strict"]
    # Skip anything that would need a pad rather than crop.
    full_screen_only: bool = False
    # Nudes (museum subject tags, Wikidata, or the title) are left out unless allowed.
    allow_nudes: bool = False


DEFAULT_PREFS = Prefs()


@dataclass
class Candidate:
    work: Work
    plan: imaging.Plan
    score: float
    prefs: Prefs = DEFAULT_PREFS


@dataclass
class FilterReport:
    total: int = 0
    rejected: Counter = field(default_factory=Counter)
    modes: Counter = field(default_factory=Counter)
    seasons: Counter = field(default_factory=Counter)

    @property
    def passed(self) -> int:
        return self.total - sum(self.rejected.values())


# Letters NFKD doesn't decompose into ASCII.
_TRANSLIT = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l", "đ": "d"}
)


def slug(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLIT))
    text = text.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def identity_key(w: Work) -> str:
    """Cross-source identity: Wikidata QID when known, else artist + title."""
    if w.wikidata:
        return f"wd:{w.wikidata}"
    return f"{slug(w.artist)}|{slug(w.title)}"


def score(w: Work, plan: imaging.Plan, season: str | None) -> float:
    s = 0.0
    if w.highlight:
        s += 3
    if w.sitelinks:
        # Notability: Wikipedia articles about the work, with diminishing returns.
        s += min(4.0, math.log2(1 + w.sitelinks))
    if season and season in w.tags:
        s += 1.5
    if plan.mode == "crop":
        s += 0.5
    if plan.scale > 1:
        s -= (plan.scale - 1) * 4
    return s


def exclusion(w: Work, prefs: Prefs = DEFAULT_PREFS) -> str | imaging.Plan:
    """Why a work is left out, or its plan when it is in."""
    if not w.width or not w.height:
        return "no pixel size"
    if not (w.iiif or w.image_url):
        return "no image url"
    if not prefs.allow_nudes and ("nude" in w.tags or classify.nude_title(w.title)):
        return "nude"
    if TONDO.search(w.title or ""):
        return "round (tondo)"
    if not imaging.AR_MIN <= w.width / w.height <= imaging.AR_MAX:
        return "too tall or too wide"
    plan = imaging.plan_for(w.width, w.height, prefs.max_crop)
    if prefs.full_screen_only and plan.mode == "pad":
        return "not full screen at this crop level"
    if plan.scale > MAX_UPSCALE:
        return f"too small to {plan.mode} (>{MAX_UPSCALE}x upscale)"
    return plan


def filter_works(
    works: list[Work], season: str | None, prefs: Prefs = DEFAULT_PREFS
) -> tuple[list[Candidate], FilterReport]:
    report = FilterReport(total=len(works))
    out: list[Candidate] = []
    seen: dict[str, Candidate] = {}
    for w in works:
        plan = exclusion(w, prefs)
        if isinstance(plan, str):
            report.rejected[plan] += 1
            continue
        cand = Candidate(w, plan, score(w, plan, season), prefs)
        ident = identity_key(w)
        prev = seen.get(ident)
        if prev is not None:
            report.rejected["duplicate (same artist+title or QID)"] += 1
            # Keep whichever copy needs less upscaling.
            if cand.plan.scale < prev.plan.scale:
                out[out.index(prev)] = cand
                seen[ident] = cand
            continue
        seen[ident] = cand
        out.append(cand)
        report.modes[plan.mode] += 1
        for t in w.tags:
            report.seasons[t] += 1
    return out, report


# Round widths to fall back on when a IIIF server won't render the exact one.
WIDTH_LADDER = (2000, 2500, 3000, 4000, 5000, 6000)


def image_request(w: Work, width: int | None = None) -> str:
    if w.iiif:
        return f"{w.iiif}/full/{width},/0/default.jpg"
    return w.image_url


def thumb_url(w: Work) -> str | None:
    """A small image the museum serves, for browsing the catalogue."""
    if w.iiif:
        return f"{w.iiif}/full/!400,400/0/default.jpg"
    if not w.image_url:
        return None
    if w.source == "met":
        return w.image_url.replace("/original/", "/web-large/")
    if w.source == "cleveland":
        return re.sub(r"_(print|full)\.(jpg|tif)$", "_web.jpg", w.image_url)
    if w.source == "commons":
        return commons_thumb(w.image_url, 500)
    if w.source == "loc":
        # The TIFF master's 1024px service JPEG.
        return re.sub(
            r"/storage-services/master/(.+?)u\.tif$",
            r"/storage-services/service/\1v.jpg",
            w.image_url,
        )
    return w.image_url


def commons_thumb(url: str, width: int) -> str:
    """A Wikimedia thumbnail URL for an original or another thumbnail."""
    url = url.split("?", 1)[0]
    if m := re.search(r"/thumb/(.+?)/\d+px-[^/]+$", url):
        path = m.group(1)
    elif m := re.search(r"/wikipedia/commons/(.+)$", url):
        path = m.group(1)
    else:
        return url
    name = path.rsplit("/", 1)[-1]
    return (
        f"https://upload.wikimedia.org/wikipedia/commons/thumb/{path}/{width}px-{name}"
    )


class Rejected(Exception):
    """The work is unsuitable once fetched; don't try it again."""


class TooSmall(Rejected):
    pass


class NotRectangular(Rejected):
    pass


class NotFullScreen(Exception):
    """Trimming changed the shape so it would need a pad; skipped only under this setting."""


def fetch(http: Http, w: Work, plan: imaging.Plan) -> bytes:
    """Download the image, checking the server really sent the size we asked for.

    AIC's CDN answers many widths with a long-cached 843px fallback and caps
    real renders at about 3000px, so when that happens step through round
    widths (3000 last) and accept anything within the upscale limit.
    """
    needed = plan.needed_width(w.width)
    if not w.iiif:
        return http.get_bytes(image_request(w))
    widths = (
        [needed] + [x for x in WIDTH_LADDER if needed < x < w.width] + [3000, w.width]
    )
    got = (0, 0)
    for width in dict.fromkeys(widths):
        data = http.get_bytes(image_request(w, width))
        got = imaging.peek_size(data)
        if got[0] * MAX_UPSCALE >= needed:
            return data
        log.info("%s: asked for %dpx wide, got %dpx", w.key, width, got[0])
    raise TooSmall(f"server keeps sending {got[0]}px, needed {needed}px")


def filename(w: Work) -> str:
    parts = [w.source, slug(w.source_id), slug(w.artist)[:40], slug(w.title)[:60]]
    return "-".join(p for p in parts if p) + ".jpg"


@dataclass
class Output:
    work: Work
    plan: imaging.Plan
    phash: int
    image: Image.Image  # final pixels, ready to save
    colors: list[str] = field(default_factory=list)  # dominant colours, hex


def load_work(http: Http, cand: Candidate) -> Image.Image:
    """Download a work at the size its plan needs, reject round works, trim scan margins."""
    w = cand.work
    data = fetch(http, w, cand.plan)
    needed = cand.plan.needed_width(w.width)
    im = imaging.load(data, max_side=max(needed, round(needed * w.height / w.width)))
    del data
    if w.kind == "photograph":
        # No scan card or margins, and trimming would eat a clear blue sky.
        return im
    if imaging.artwork_mask(im) is not None:
        # Round or irregular work on a white scan card (tondi, fans): it can't
        # fill the screen, and padding them leaves the white card showing.
        raise NotRectangular("round or irregular work on a scan card")
    return imaging.trim_borders(im)


def render(http: Http, cand: Candidate, pad_style: str = "blur") -> Output:
    im = load_work(http, cand)
    # The fetched image may be smaller than the catalogued master, and trimming
    # changes the shape, so plan again from what we actually have.
    plan = imaging.plan_for(*im.size, cand.prefs.max_crop)
    if cand.prefs.full_screen_only and plan.mode == "pad":
        raise NotFullScreen(f"{im.size[0]}x{im.size[1]} after trimming")
    if plan.scale > MAX_UPSCALE:
        raise TooSmall(f"{im.size[0]}x{im.size[1]} needs {plan.scale:.2f}x upscaling")
    return Output(
        cand.work,
        plan,
        imaging.phash(im),
        imaging.render(im, plan, pad_style),
        imaging.palette(im),
    )


def folder_for(plan: imaging.Plan, pad_style: str) -> str:
    """Full-screen renders upload with no matte; native-shape ones get the TV's matte."""
    return "matted" if pad_style == "matte" and plan.mode == "pad" else "full"


def write_atomic(im: Image.Image, path: Path, staging: Path) -> None:
    """Save via a staging dir and rename, so HA never sees a half-written file."""
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / path.name
    imaging.save_jpeg(im, tmp)
    tmp.replace(path)
