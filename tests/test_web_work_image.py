import io

import httpx
import pytest
from PIL import Image
from starlette.testclient import TestClient

from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.service import Service
from frame_art.web import create_app

IIIF = "https://example.org/iiif/1"


def jpeg(size=(1200, 800)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "JPEG")
    return buf.getvalue()


class FakeHttp:
    """Answers from ``pages``; anything else is a 404 from the museum."""

    def __init__(self, pages: dict[str, bytes]):
        self.pages, self.calls = pages, []
        self.pages_error: Exception | None = None  # raised for a missing page instead

    def get_bytes(self, url: str, **kw) -> bytes:
        self.calls.append(url)
        if url not in self.pages and self.pages_error:
            raise self.pages_error
        if url not in self.pages:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404, request=req)
            )
        return self.pages[url]


@pytest.fixture
def app(tmp_path):
    data = tmp_path / "data"
    Catalogue(data / "frame-art.db").upsert(
        [
            Work(source="nga", source_id="1", title="Haystacks", artist="Monet",
                 date="1891", kind="painting", license="CC0", iiif=IIIF,
                 width=4000, height=2250),
            Work(source="nga", source_id="2", title="Gone", artist="Monet",
                 date="1891", kind="painting", license="CC0",
                 iiif="https://example.org/iiif/2", width=4000, height=2250),
        ]
    )  # fmt: skip
    http = FakeHttp({f"{IIIF}/full/!400,400/0/default.jpg": jpeg(),
                     f"{IIIF}/full/!1920,1920/0/default.jpg": jpeg()})  # fmt: skip
    svc = Service(data, tmp_path / "out", "/config/frame_art_pool", None, 1883, "test")
    return TestClient(create_app(svc, http=http)), http, data


def test_library_cards_use_same_origin_work_images(app):
    c, _, _ = app
    page = c.get("/library?scope=all").text
    assert 'src="/work-image/thumb/nga:1"' in page
    assert 'data-full="/work-image/preview/nga:1"' in page
    assert "example.org" not in page


def test_thumb_is_fetched_once_shrunk_and_cached(app):
    c, http, data = app
    r = c.get("/work-image/thumb/nga:1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 400
    assert c.get("/work-image/thumb/nga:1").content == r.content
    assert http.calls == [f"{IIIF}/full/!400,400/0/default.jpg"]
    assert list((data / "thumbs" / "works").glob("*.jpg"))


def test_preview_is_passed_through(app):
    c, http, _ = app
    r = c.get("/work-image/preview/nga:1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert http.calls == [f"{IIIF}/full/!1920,1920/0/default.jpg"]


def test_missing_image_is_404_and_not_refetched_soon(app):
    c, http, _ = app
    assert c.get("/work-image/thumb/nga:2").status_code == 404
    assert c.get("/work-image/thumb/nga:2").status_code == 404
    assert len(http.calls) == 1


def test_unknown_work_or_size_is_404_without_fetching(app):
    c, http, _ = app
    assert c.get("/work-image/thumb/nga:999").status_code == 404
    assert c.get("/work-image/huge/nga:1").status_code == 404
    assert http.calls == []


def test_rate_limited_picture_is_retried_on_the_next_request(app):
    c, http, _ = app
    url = f"{IIIF}/full/!400,400/0/default.jpg"
    good = http.pages.pop(url)
    req = httpx.Request("GET", url)
    http.pages_error = httpx.HTTPStatusError(
        "429", request=req, response=httpx.Response(429, request=req)
    )
    assert c.get("/work-image/thumb/nga:1").status_code == 404
    http.pages[url], http.pages_error = good, None
    assert c.get("/work-image/thumb/nga:1").status_code == 200
