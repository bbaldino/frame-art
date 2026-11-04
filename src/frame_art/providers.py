"""Image models behind one interface, so another provider can be added later.

OpenRouter is the only one today. A provider paints from one picture and a
prompt; it raises PaintFailed on any failure, and ``plain_error`` turns that
into words for a page (never the key).
"""

from __future__ import annotations

from typing import Protocol

from PIL import Image

from . import keys, openrouter
from .net import Http

Painted = openrouter.Edit
PaintFailed = openrouter.EditFailed


class ImageProvider(Protocol):
    name: str
    model: str

    def paint(
        self, im: Image.Image, prompt: str, aspect: str | None = None, size: str = "4K"
    ) -> Painted: ...


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, http: Http, api_key: str, model: str = openrouter.MODEL):
        self._http = http
        self._key = api_key
        self.model = model

    def paint(
        self, im: Image.Image, prompt: str, aspect: str | None = None, size: str = "4K"
    ) -> Painted:
        return openrouter.edit_image(
            self._http,
            self._key,
            im,
            prompt,
            model=self.model,
            size=size,
            aspect=aspect,
        )


def image_provider(http: Http) -> ImageProvider | None:
    """The configured provider, or None when it has no key."""
    api_key = keys.read_key("openrouter")
    return OpenRouterProvider(http, api_key) if api_key else None


def plain_error(e: Exception) -> str:
    """A failed paint in words Brian can act on."""
    msg = str(e)
    if "402" in msg:
        return "OpenRouter: balance too low"
    if "returned no image" in msg:
        return "OpenRouter: no image came back"
    return msg
