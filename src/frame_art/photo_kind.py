"""Suggest whether a photo is a portrait or a scene; Brian can override it.

The kind decides which painters the Studio suggests first and whether the
setting is kept or reinvented (see the portrait studio design).
"""

from __future__ import annotations

import json
import logging
import re

from PIL import Image

from . import openrouter
from .net import Http
from .photos import KINDS, PhotoStore, open_image

log = logging.getLogger(__name__)

VISION_MODEL = "google/gemini-3.1-flash-lite"

PROMPT = (
    "Classify this photograph for a painter. Answer with JSON only, like "
    '{"kind": "portrait", "reason": "close portrait, 1 person"}. '
    'kind is "portrait" when one or more faces fill much of the frame, and '
    '"scene" when the people are small within a larger setting. reason is at '
    'most 8 words, e.g. "close portrait, 1 person" or "2 people, water, outdoors".'
)


def suggest(http: Http, api_key: str, im: Image.Image) -> tuple[str, str]:
    """(kind, reason) for ``im``. Raises ValueError on a reply it can't use."""
    reply = openrouter.describe(http, api_key, im, PROMPT, VISION_MODEL)
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise ValueError(f"unusable reply: {reply[:80]!r}")
    return data["kind"], str(data.get("reason") or "")[:80]


def classify_new(store: PhotoStore, http: Http, api_key: str) -> dict:
    """Suggest a kind for each photo that has none; never touch Brian's choice."""
    done, failed = 0, []
    for p in store.all():
        if p.kind is not None or p.kind_auto:
            continue
        try:
            with open_image(p) as im:
                kind, reason = suggest(http, api_key, im)
        except (openrouter.EditFailed, ValueError, OSError) as e:
            log.warning("%s: no kind suggested: %s", p.id, e)
            failed.append({"id": p.id, "error": str(e)})
            continue
        store.set_kind(p.id, kind, reason, True)
        done += 1
    return {"classified": done, "failed": failed}
