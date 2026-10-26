"""API keys for the sources that need one. Keys never live in settings or the repo.

Each key is a file ``<keys dir>/<service>/key`` (chmod 600) holding just the key.
The keys dir is ``$FRAME_ART_KEYS``, else ``~/.config``. The web UI reports
only whether a key is present; nothing here ever returns the key to a page.
docs/api-keys.md describes the same list for whoever signs up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KeyedService:
    slug: str  # directory name under the keys dir
    name: str
    signup: str
    limits: str
    adds: str


SERVICES = (
    KeyedService(
        "smithsonian", "Smithsonian Open Access (api.data.gov)",
        "https://api.data.gov/signup/", "1,000 requests/hour",
        "CC0 paintings and photographs across the Smithsonian museums",
    ),
    KeyedService(
        "harvard", "Harvard Art Museums",
        "https://harvardartmuseums.org/collections/api", "2,500 requests/day",
        "Harvard's paintings; check each image's reuse terms",
    ),
    KeyedService(
        "cooper-hewitt", "Cooper Hewitt",
        "https://collection.cooperhewitt.org/api/", "not published",
        "design and drawings; small images, and the API may be retired",
    ),
    KeyedService(
        "parismusees", "Paris Musées",
        "https://apicollections.parismusees.paris.fr/", "not published",
        "CC0 works from the 14 Paris city museums (Petit Palais, Carnavalet...)",
    ),
    KeyedService(
        "europeana", "Europeana",
        "https://pro.europeana.eu/pages/get-api", "no hard limit (fair use)",
        "open-licence works aggregated from thousands of European museums",
    ),
    KeyedService(
        "pexels", "Pexels",
        "https://www.pexels.com/api/", "200 requests/hour, 20,000/month",
        "landscape photography at full resolution, free licence",
    ),
    KeyedService(
        "unsplash", "Unsplash",
        "https://unsplash.com/developers", "50/hour (demo), 5,000/hour approved",
        "landscape photography; API terms want hotlinking, so use is doubtful",
    ),
    KeyedService(
        "pixabay", "Pixabay",
        "https://pixabay.com/api/docs/", "100 requests/minute",
        "photography, but only 1280px without approved full API access",
    ),
    KeyedService(
        "nps", "US National Park Service",
        "https://www.nps.gov/subjects/developer/get-started.htm", "1,000 requests/hour",
        "park photos; most are small, low priority",
    ),
    KeyedService(
        "flickr", "Flickr (Flickr Commons)",
        "https://www.flickr.com/services/apps/create/", "3,600 requests/hour",
        "archive photos with no known restrictions; few are 4K landscapes",
    ),
)  # fmt: skip


def keys_dir() -> Path:
    env = os.environ.get("FRAME_ART_KEYS")
    return Path(env) if env else Path.home() / ".config"


def key_path(slug: str) -> Path:
    return keys_dir() / slug / "key"


def has_key(slug: str) -> bool:
    try:
        return bool(key_path(slug).read_text().strip())
    except OSError:
        return False


def read_key(slug: str) -> str | None:
    """For source adapters only; never pass the result to a template or log."""
    try:
        return key_path(slug).read_text().strip() or None
    except OSError:
        return None
