from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import httpx
from PIL import ImageFile

log = logging.getLogger(__name__)

CONTACT = "frame-art (personal Frame TV art fetcher)"
# Some hosts sit behind Cloudflare rules that reject unusual User-Agent strings
# (AIC does), so keep the UA browser-neutral and identify via headers instead.
DEFAULT_HEADERS = {"User-Agent": f"python-httpx/{httpx.__version__} {CONTACT}"}

# Minimum seconds between requests to a host. Unlisted hosts get DEFAULT_INTERVAL.
HOST_INTERVALS = {
    "collectionapi.metmuseum.org": 0.15,
    "images.metmuseum.org": 0.15,
    "api.artic.edu": 0.25,
}
DEFAULT_INTERVAL = 0.1

# The Met's Cloudflare front answers bursts with 403 rather than 429.
RETRY_STATUS = {403, 429, 500, 502, 503, 504}


class Http:
    """A small polite HTTP client: per-host pacing plus retry with backoff."""

    def __init__(self, timeout: float = 60.0):
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True
        )
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}

    def _wait_turn(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        interval = HOST_INTERVALS.get(host, DEFAULT_INTERVAL)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + interval
        if slot > now:
            time.sleep(slot - now)

    def request(
        self, method: str, url: str, *, retries: int = 4, **kw
    ) -> httpx.Response:
        for attempt in range(retries + 1):
            self._wait_turn(url)
            try:
                resp = self._client.request(method, url, **kw)
            except httpx.TransportError as ex:
                if attempt == retries:
                    raise
                log.debug("transport error on %s: %s", url, ex)
            else:
                if resp.status_code not in RETRY_STATUS or attempt == retries:
                    return resp
                log.debug("HTTP %s on %s, retrying", resp.status_code, url)
            time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def get_json(self, url: str, **kw):
        resp = self.request("GET", url, **kw)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, url: str, body: dict, **kw):
        resp = self.request("POST", url, json=body, **kw)
        resp.raise_for_status()
        return resp.json()

    def get_bytes(self, url: str, **kw) -> bytes:
        resp = self.request("GET", url, **kw)
        resp.raise_for_status()
        return resp.content

    def probe_image_size(
        self, url: str, max_bytes: int = 256 * 1024
    ) -> tuple[int, int] | None:
        """Read just enough of a remote image to learn its pixel size."""
        parser = ImageFile.Parser()
        self._wait_turn(url)
        try:
            with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                read = 0
                for chunk in resp.iter_bytes(16 * 1024):
                    parser.feed(chunk)
                    if parser.image:
                        return parser.image.size
                    read += len(chunk)
                    if read >= max_bytes:
                        return None
        except (httpx.HTTPError, OSError) as ex:
            log.debug("size probe failed for %s: %s", url, ex)
        return None

    def download(self, url: str, dest, **kw) -> None:
        self._wait_turn(url)
        with self._client.stream("GET", url, **kw) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.writelines(resp.iter_bytes(1024 * 1024))
