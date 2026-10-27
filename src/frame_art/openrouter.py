from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import httpx
from PIL import Image

from .net import Http

MODEL = "google/gemini-nano-banana-2.1"
URL = "https://openrouter.ai/api/v1/chat/completions"
ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
MAX_SIDE = 2048


class EditFailed(Exception):
    pass


@dataclass
class Edit:
    image: Image.Image
    cost: float | None
    generation_id: str | None


def _ratio(aspect: str) -> float:
    w, h = aspect.split(":")
    return int(w) / int(h)


def nearest_aspect(width: int, height: int) -> str:
    target = width / height
    return min(ASPECTS, key=lambda a: abs(_ratio(a) - target))


def _data_url(im: Image.Image) -> str:
    im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _error_message(data) -> str | None:
    err = data.get("error") if isinstance(data, dict) else None
    if err is None:
        return None
    if isinstance(err, dict):
        return str(err.get("message") or "unknown error")
    return str(err)


def edit_image(
    http: Http,
    api_key: str,
    im: Image.Image,
    prompt: str,
    model: str = MODEL,
    size: str = "4K",
    aspect: str | None = None,
) -> Edit:
    """Ask OpenRouter to edit `im` per `prompt`. Raises EditFailed on any failure.

    The result has shape ``aspect`` (one of ASPECTS), by default the nearest to
    ``im``'s. The request is never retried: each attempt may be billed, and a
    timeout can come after OpenRouter has done (and charged for) the edit.
    """
    aspect = aspect or nearest_aspect(*im.size)
    body = {
        "model": model,
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": aspect, "image_size": size},
        "usage": {"include": True},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(im)}},
                ],
            }
        ],
    }
    try:
        resp = http.request(
            "POST",
            URL,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            retries=0,
        )
    except httpx.HTTPError as ex:
        # Never chain: the request, with its key, hangs off the exception.
        raise EditFailed(f"OpenRouter request failed: {type(ex).__name__}") from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    message = _error_message(data)
    if not 200 <= resp.status_code < 300:
        raise EditFailed(
            f"OpenRouter returned {resp.status_code}: {message or 'no details'}"
        )
    if message is not None:
        raise EditFailed(f"OpenRouter error: {message}")
    try:
        url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        out = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        out.load()
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        raise EditFailed("OpenRouter returned no image") from None
    usage = data.get("usage") or {}
    return Edit(image=out, cost=usage.get("cost"), generation_id=data.get("id"))
