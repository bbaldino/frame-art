import pytest
from starlette.testclient import TestClient

from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.service import Service
from frame_art.settings import Settings
from frame_art.web import create_app


@pytest.fixture
def client(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "out"
    Catalogue(data / "frame-art.db").upsert(
        [
            Work(
                source="nga", source_id="1", title="Haystacks", artist="Monet",
                date="1891", kind="painting", license="CC0",
                iiif="https://example.org/iiif/1", width=4000, height=2250,
            )
        ]
    )  # fmt: skip
    svc = Service(data, dest, "/config/frame_art_pool", None, 1883, "test")
    # No start(): the worker and scheduler threads aren't needed to render pages.
    return TestClient(create_app(svc, None)), svc


@pytest.mark.parametrize(
    "path", ["/", "/pool", "/catalogue", "/catalogue?status=eligible", "/settings",
             "/compare", "/runs", "/healthz"],
)  # fmt: skip
def test_pages_render(client, path):
    c, _ = client
    assert c.get(path).status_code == 200


def test_ban_and_unban(client):
    c, svc = client
    r = c.post(
        "/works/nga:1/override?next=/catalogue",
        data={"state": "banned"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/catalogue"
    assert svc.catalogue().overrides() == {"nga:1": "banned"}
    c.post("/works/nga:1/override", data={"state": "clear"})
    assert svc.catalogue().overrides() == {}


def test_override_rejects_offsite_redirect(client):
    c, _ = client
    r = c.post(
        "/works/nga:1/override?next=//evil.example",
        data={"state": "pinned"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_settings_validate_and_persist(client):
    c, svc = client
    form = {"crop": "medium", "pad_style": "blur", "kinds": "painting",
            "sources": "nga", "count": "5", "pool": "100", "run_at": "03:30",
            "recrawl_days": "7", "crawl_limit": "300"}  # fmt: skip
    assert "Saved" in c.post("/settings", data=form).text
    s = svc.settings()
    assert (s.crop, s.count, s.run_at, s.sources) == ("medium", 5, "03:30", ["nga"])
    bad = {**form, "run_at": "25:00", "count": "99"}
    assert "Not saved" in c.post("/settings", data=bad).text
    assert svc.settings().run_at == "03:30"


def test_settings_load_falls_back_on_garbage(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"crop": "extreme", "count": 3}')
    assert Settings.load(p) == Settings()
    p.write_text('{"count": 3, "unknown_key": 1}')
    assert Settings.load(p).count == 3


def test_settings_shows_key_presence_but_never_the_key(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "keys"))
    (tmp_path / "keys" / "pexels").mkdir(parents=True)
    (tmp_path / "keys" / "pexels" / "key").write_text("sekrit-value-123\n")
    page = c.get("/settings").text
    assert "sekrit-value-123" not in page
    assert page.count("present") == 1
    assert str(tmp_path / "keys" / "pexels" / "key") in page


def test_settings_style_mix_validates(client):
    c, svc = client
    form = {"crop": "strict", "pad_style": "blur", "kinds": "painting",
            "sources": ["nga", "commons"], "count": "5", "pool": "100",
            "run_at": "02:00", "recrawl_days": "7", "crawl_limit": "300",
            "photo_share": "40", "photo_crop": "medium"}  # fmt: skip
    assert "Saved" in c.post("/settings", data=form).text
    s = svc.settings()
    assert (s.photo_share, s.photo_crop) == (40, "medium")
    assert s.crawl_kinds() == ("painting", "photograph")
    assert "Not saved" in c.post("/settings", data={**form, "photo_share": "150"}).text
