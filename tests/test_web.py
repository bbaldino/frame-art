import dataclasses

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
    return TestClient(create_app(svc)), svc


@pytest.mark.parametrize(
    "path", ["/", "/pool", "/catalogue", "/catalogue?status=eligible", "/settings",
             "/compare", "/runs", "/healthz", "/collection", "/history",
             "/history?todo=1"],
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


def test_settings_load_keeps_valid_fields(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"crop": "extreme", "count": 3}')
    assert Settings.load(p) == Settings(count=3)
    p.write_text('{"count": 3, "unknown_key": 1, "pool": "lots"}')
    assert Settings.load(p) == Settings(count=3)
    # A source from a newer version is dropped; the rest of the list stays.
    p.write_text('{"sources": ["nga", "from-the-future"], "count": 5}')
    s = Settings.load(p)
    assert (s.sources, s.count) == (["nga"], 5)
    p.write_text("[1, 2]")
    assert Settings.load(p) == Settings()


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
    assert s.shown_kinds() == ("painting", "photograph")
    zero = Settings(photo_share=0)
    # Photos are catalogued whenever a photo source is on, but not published at 0%.
    assert zero.crawl_kinds() == ("painting", "photograph")
    assert zero.shown_kinds() == ("painting",)
    assert Settings(sources=["nga"]).crawl_kinds() == ("painting",)
    assert "Not saved" in c.post("/settings", data={**form, "photo_share": "150"}).text


def test_now_showing_maps_tv_id_to_work(client):
    import json

    c, svc = client
    assert "hasn't reported" in c.get("/now").text
    cat = svc.catalogue()
    cat.record_publish("haystacks.jpg", "full", "nga:1", 0)
    cat.set_palette("nga:1", ["#112233"])
    (svc.dest / "full").mkdir(parents=True)
    (svc.dest / "full" / "haystacks.jpg").write_bytes(b"jpeg")
    sidecar = {"haystacks.jpg": {"content_id": "MY_F0042", "modified": 1.0}}
    (svc.dest / "full" / ".samsungtv_upload.json").write_text(json.dumps(sidecar))

    svc._on_current({"content_id": "SAM-S1110748"})
    assert "didn't put there" in c.get("/now").text

    svc._on_current({"content_id": "MY_F0042"})
    now = c.get("/now.json").json()
    assert now["ours"] and now["title"] == "Haystacks" and now["colors"] == ["#112233"]
    page = c.get("/now").text
    assert "Haystacks" in page and "/render/full/haystacks.jpg" in page
    assert c.get("/render/full/haystacks.jpg").content == b"jpeg"
    assert c.get("/render/full/..%2Fsecret.jpg").status_code == 404

    svc._on_current({"content_id": "../../etc"})  # ignored
    assert c.get("/now.json").json()["content_id"] == "MY_F0042"


def test_overview_never_embeds_ha_thumbnail_and_reports_tv_off(client):
    c, svc = client
    svc._on_current({"content_id": "SAM-S1110748", "on": False})
    page = c.get("/").text
    assert "/local/" not in page and "The TV is off" in page
    assert svc.now_showing()["tv_on"] is False
    first = svc.catalogue().get("tv_current")["at"]
    svc._on_current({"content_id": "SAM-S1110748", "on": True})
    assert (
        svc.catalogue().get("tv_current")["at"] == first
    )  # same artwork, same "since"
    assert "didn't put there" in c.get("/").text


def test_catalogue_filters_on_metadata(client):
    c, svc = client
    cat = svc.catalogue()
    w = cat.works()[0]
    w.style, w.genre, w.place = "Impressionism", "landscape painting", "France"
    w.subjects = ["haystacks", "Winter"]
    cat.upsert([w])
    assert "1 matching" in c.get("/catalogue?style=Impressionism").text
    assert "0 matching" in c.get("/catalogue?style=Cubism").text
    assert "1 matching" in c.get("/catalogue?subject=winter").text
    assert "1 matching" in c.get("/catalogue?q=france").text
    assert "1 matching" in c.get("/catalogue?tag=france&tag=WINTER").text
    assert "0 matching" in c.get("/catalogue?tag=france&tag=summer").text
    page = c.get("/catalogue?tag=france").text
    # Card chips add to the filter; active tags come off one at a time.
    assert "tag=france&amp;tag=haystacks" in page
    assert 'href="/catalogue?page=1" title="Remove this tag"' in page


def test_tag_suggestions(client):
    c, svc = client
    cat = svc.catalogue()
    a = cat.works()[0]
    a.subjects = ["Winter", "snow"]
    b = dataclasses.replace(a, source_id="2", subjects=["winter landscape", "boats"])
    cat.upsert([a, b])
    tags = [t["tag"] for t in c.get("/tags.json?q=win").json()]
    assert tags[:2] == ["Winter", "winter landscape"]
    # Suggestions narrow to works that have the tags already chosen.
    got = c.get("/tags.json?q=&tag=snow").json()
    assert {t["tag"] for t in got} >= {"Winter"} and "boats" not in {
        t["tag"] for t in got
    }
    assert "snow" not in {t["tag"].lower() for t in got}


def test_collect_from_the_catalogue(client):
    c, svc = client
    assert "0 in your collection" in c.get("/catalogue").text
    assert "No works match" in c.get("/collection").text
    c.post("/works/nga:1/override?next=/catalogue", data={"state": "collect"})
    assert set(svc.catalogue().collection()) == {"nga:1"}
    page = c.get("/collection").text
    assert "1 matching" in page and "Haystacks" in page and "✓ Collected" in page
    c.post("/works/nga:1/override?next=/catalogue", data={"state": "uncollect"})
    assert svc.catalogue().collection() == {}


def test_history_records_what_the_tv_showed(client):
    import json

    c, svc = client
    cat = svc.catalogue()
    cat.record_publish("haystacks.jpg", "full", "nga:1", 0)
    (svc.dest / "full").mkdir(parents=True)
    sidecar = {"haystacks.jpg": {"content_id": "MY_F0042", "modified": 1.0}}
    (svc.dest / "full" / ".samsungtv_upload.json").write_text(json.dumps(sidecar))

    svc._on_current({"content_id": "SAM-S1110748", "on": True})
    svc._on_current({"content_id": "SAM-S1110748", "on": True})  # a repeat, not new
    svc._on_current({"content_id": "MY_F0042", "on": True})
    svc._on_current({"content_id": "MY_F0042", "on": False})  # screen off
    svc._on_current({"content_id": "MY_F0042", "on": True})  # back on: a new stretch
    rows = cat.shown()
    assert [r["content_id"] for r in rows] == ["MY_F0042", "MY_F0042", "SAM-S1110748"]
    assert rows[0]["ended_at"] is None and rows[1]["ended_at"] is not None
    assert rows[0]["key"] == "nga:1" and rows[2]["key"] is None

    page = c.get("/history").text
    assert (
        "Haystacks" in page and "Not from frame-art" in page and "showing now" in page
    )
    c.post("/works/nga:1/override?next=/history", data={"state": "collect"})
    todo = c.get("/history?todo=1").text
    assert "Haystacks" not in todo and "Nothing left to sort" in todo


def test_pins_move_into_the_collection_once(client):
    from frame_art.service import pins_to_collection

    _, svc = client
    cat = svc.catalogue()
    cat.set_override("nga:1", "pinned")
    pins_to_collection(cat)
    assert set(cat.collection()) == {"nga:1"} and cat.overrides() == {}
    cat.set_override("nga:1", "pinned")  # a pin made after the move stays a pin
    pins_to_collection(cat)
    assert cat.overrides() == {"nga:1": "pinned"}


class FakeLink:
    connected = True

    def __init__(self):
        self.sent = []

    def command(self, payload):
        self.sent.append(payload)
        return True

    def publish(self, state):
        pass

    def publish_now(self, info):
        pass


def test_tv_buttons_ask_ha_and_show_its_answer(client):
    c, svc = client
    svc.link = FakeLink()
    s = svc.settings()
    s.slideshow = "15min"
    svc.save_settings(s)
    r = c.post("/tv/samsung", follow_redirects=False)
    assert r.status_code == 303
    (sent,) = svc.link.sent
    assert sent["action"] == "samsung" and sent["duration"] == "15min"
    assert "waiting for Home Assistant" in c.get("/").text
    cat = svc.catalogue()
    cat.put("tv_command", {**cat.get("tv_command"), "at": 0})  # long ago
    assert "no answer from Home Assistant" in c.get("/").text
    svc._on_ack({"command": {"id": sent["id"] - 1, "ok": True}})  # stale: ignored
    assert svc.catalogue().get("tv_command")["status"] == "sent"
    svc._on_ack({"command": {"id": sent["id"], "error": "The TV is off"}})
    assert "The TV is off" in c.get("/").text
    c.post("/tv/upload")
    assert "duration" not in svc.link.sent[-1]
    svc._on_ack({"command": {"id": svc.link.sent[-1]["id"], "ok": True}})
    assert svc.catalogue().get("tv_command")["status"] == "done"
    assert c.post("/tv/power-off").status_code == 404


def test_tv_button_without_mqtt_says_so(client):
    c, svc = client
    c.post("/tv/mine")
    assert svc.catalogue().get("tv_command")["status"] == "unsent"
    assert "not sent" in c.get("/").text


def test_tv_request_answered_before_send_returns(client):
    c, svc = client

    class Quick(FakeLink):
        def command(self, payload):  # HA answers before publish() returns
            svc._on_ack({"command": {"id": payload["id"], "ok": True}})
            return True

    svc.link = Quick()
    c.post("/tv/mine")
    assert svc.catalogue().get("tv_command")["status"] == "done"
