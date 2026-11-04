import dataclasses
import html
import json
import time
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from frame_art import publish, studio
from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.schedules import everyday
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
    assert "Home Assistant never answered" in c.get("/").text
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


def test_tv_panel_follows_an_upload(client):
    c, svc = client
    assert "data-live" not in c.get("/tv/status").text
    svc._on_ack({"sync": {"phase": "uploading", "total": 9}})
    panel = c.get("/tv/status").text
    assert "data-live" in panel and "Uploading to the TV" in panel
    assert "About 0 of 9" in panel  # HA hasn't reported yet: an estimate
    cat = svc.catalogue()
    cat.put("tv_sync", {**cat.get("tv_sync"), "started": time.time() - 20})
    assert "About 3 of 9" in c.get("/tv/status").text
    svc._on_ack({"upload": {"uploaded": ["MY_F0001"] * 7, "failed": ["x.jpg"]}})
    # The real count replaces HA's guess at the start.
    assert "7 of 8 uploaded" in c.get("/tv/status").text
    svc._on_ack({"sync": {"phase": "deleting", "total": 2}})
    svc._on_ack({"deleted": "MY_F0001"})
    assert "1 of 2 deleted" in c.get("/tv/status").text
    svc._on_ack({"sync": {"phase": "done"}})
    panel = c.get("/tv/status").text
    assert "data-live" not in panel and "Last upload: finished" in panel
    assert '7 uploaded, <span class="warn">1 failed' in panel


def test_tv_panel_notices_a_silent_upload(client):
    c, svc = client
    svc._on_ack({"sync": {"phase": "uploading", "total": 2}})
    cat = svc.catalogue()
    cat.put("tv_sync", {**cat.get("tv_sync"), "started": 0, "at": 0})
    panel = c.get("/tv/status").text
    assert "stopped reporting" in panel and "data-live" not in panel


def test_upload_button_says_when_nothing_will_happen(client):
    c, svc = client
    svc._on_uploads({"enabled": False})
    page = c.get("/").text
    assert "Uploads are turned off in Home Assistant" in page
    # Last known only (the retained value can outlive the automation), so the
    # button still asks HA rather than refusing.
    assert 'title="Uploads are turned off' not in page
    svc._on_uploads({"enabled": True})
    assert "Uploads are turned off" not in c.get("/").text
    svc._on_uploads({"enabled": "yes"})  # ignored
    assert svc.catalogue().get("uploads_enabled")["enabled"] is True


def test_schedules_validate():
    assert Settings(schedules=[everyday({"halloween": 50})]).validate() == []
    assert Settings(schedules=[everyday({"halloween": 60, "autumn": 50})]).validate()
    assert Settings(schedules=[everyday({"halloween": -1})]).validate()
    assert Settings(variant_limit=51).validate()
    dup = everyday({"halloween": 5})
    assert Settings(schedules=[dup, dup]).validate()  # ids must be unique


def test_old_mix_migrates_to_everyday(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mix": {"halloween": 75}, "count": 5}))
    s = Settings.load(p)
    assert [(x.name, x.start, x.end, x.shares) for x in s.schedules] == [
        ("Everyday", "01-01", "12-31", {"halloween": 75})
    ]
    assert s.count == 5
    assert Settings.load(p).schedules[0].id == s.schedules[0].id  # stable until saved
    p.write_text(json.dumps({"mix": {}}))
    assert Settings.load(p).schedules == []
    p.write_text(json.dumps({"mix": {"halloween": "lots"}, "count": 5}))
    assert Settings.load(p) == Settings(count=5)


def test_schedules_round_trip_and_bad_ones_are_dropped(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(schedules=[everyday({"halloween": 50})], count=4)
    s.save(p)
    assert Settings.load(p) == s
    raw = json.loads(p.read_text())
    raw["schedules"][0]["start"] = "13-40"
    p.write_text(json.dumps(raw))
    back = Settings.load(p)
    assert back.schedules == [] and back.count == 4
    for junk in ("nope", [{"id": "x"}], [{**raw["schedules"][0], "extra": 1}], [5]):
        p.write_text(json.dumps({"schedules": junk, "count": 4}))
        back = Settings.load(p)
        assert back.schedules == [] and back.count == 4


def test_saving_settings_keeps_schedules_and_limit(client):
    c, svc = client
    sched = [everyday({"halloween": 50})]
    svc.save_settings(
        dataclasses.replace(svc.settings(), schedules=sched, variant_limit=4)
    )
    form = {"crop": "medium", "pad_style": "blur", "kinds": "painting",
            "sources": "nga", "count": "5", "pool": "100", "run_at": "03:30",
            "recrawl_days": "7", "crawl_limit": "300"}  # fmt: skip
    assert "Saved" in c.post("/settings", data=form).text
    assert svc.settings().schedules == sched
    assert svc.settings().variant_limit == 4


def test_generate_job_pins_slot_and_dedupes(client):
    c, svc = client
    assert svc.submit(
        "generate", {"type": "halloween", "keys": ["nga:1"], "prompt": None}
    )
    assert not svc.submit(
        "generate", {"type": "halloween", "keys": ["nga:1"], "prompt": None}
    )
    assert svc.submit(
        "generate", {"type": "halloween", "keys": ["nga:2"], "prompt": None}
    )
    assert svc.submit("versions", {"keys": ["nga:1"]})
    assert c.post("/jobs/generate").status_code == 404
    assert c.post("/jobs/versions").status_code == 404
    with pytest.raises(ValueError):
        svc.submit("bogus")
    assert c.get("/").status_code == 200  # the queue shows the new kinds


def test_generate_job_makes_a_variant_and_puts_it_on_the_tv(
    client, tmp_path, monkeypatch
):
    from PIL import Image

    from frame_art import openrouter, pipeline, variants
    from frame_art.service import Job

    _, svc = client
    keys = tmp_path / "keys"
    (keys / "openrouter").mkdir(parents=True)
    (keys / "openrouter" / "key").write_text("sk-test\n")
    monkeypatch.setenv("FRAME_ART_KEYS", str(keys))
    seen = []

    def fake_edit(
        http, api_key, im, prompt, model=openrouter.MODEL, size="4K", aspect=None
    ):
        seen.append((api_key, prompt))
        return openrouter.Edit(Image.new("RGB", (3840, 2160), "orange"), 0.12, "g")

    monkeypatch.setattr(openrouter, "edit_image", fake_edit)
    monkeypatch.setattr(
        pipeline, "load_work", lambda http, cand: Image.new("RGB", (3840, 2160))
    )
    cat = svc.catalogue()
    variants.seed_types(cat)
    cat.record_publish("nga-1-monet-haystacks.jpg", "full", "nga:1", 0)
    svc.save_settings(Settings(schedules=[everyday({"halloween": 10})]))
    svc._run(
        Job("generate", {"type": "halloween", "keys": ["nga:1"], "prompt": "spooky"})
    )
    [run] = cat.runs(1)
    assert run["kind"] == "generate" and run["status"] == "ok", run["log"]
    assert run["summary"] == {"generated": ["nga:1"], "failed": [], "cost": 0.12}
    assert seen == [("sk-test", "spooky")]
    assert cat.slots()["nga:1"].manual
    [live] = cat.published(active=True)
    assert live.filename.endswith("--halloween-1.jpg")
    assert "sk-test" not in run["log"]
    # The runs page shows it.
    c, _ = client
    assert c.get(f"/runs?id={run['id']}").status_code == 200


def test_nightly_with_a_failed_edit_is_partial(client, tmp_path, monkeypatch):
    from frame_art import jobs, variants
    from frame_art.publish import PublishResult
    from frame_art.service import Job

    _, svc = client
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "no-keys"))
    monkeypatch.setattr(jobs, "crawl", lambda *a, **k: {})
    monkeypatch.setattr(jobs, "publish", lambda *a, **k: PublishResult(["x"], [], []))
    calls = []

    def fake_nightly(cat, http, data, pub, settings, api_key, today, portraits=None):
        calls.append(api_key)
        assert portraits == 0  # no Portraits share, so none live
        assert today == svc.today()
        return {
            "slots": {},
            "generated": [],
            "gen_failed": ["nga:1"],
            "swapped": [],
            "swap_failed": ["nga:2"],
            "cost": 0.0,
        }

    monkeypatch.setattr(variants, "nightly", fake_nightly)
    svc._run(Job("nightly"))
    [run] = svc.catalogue().runs(1)
    assert calls == [None]  # no key on disk
    assert run["status"] == "partial"
    assert run["summary"]["added"] == ["x"]
    assert run["summary"]["failed"] == ["nga:1", "nga:2"]
    assert run["summary"]["variants"]["gen_failed"] == ["nga:1"]
    assert run["summary"]["portraits"] == {"live": 0, "added": [], "retired": []}


def test_nightly_keeps_the_publish_summary_when_variants_break(
    client, tmp_path, monkeypatch
):
    from frame_art import jobs, variants
    from frame_art.publish import PublishResult
    from frame_art.service import Job

    _, svc = client
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "no-keys"))
    monkeypatch.setattr(jobs, "crawl", lambda *a, **k: {})
    monkeypatch.setattr(
        jobs, "publish", lambda *a, **k: PublishResult(["x"], ["y"], ["z"])
    )

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(variants, "nightly", broken)
    svc._run(Job("nightly"))
    [run] = svc.catalogue().runs(1)
    assert run["status"] == "partial"
    s = run["summary"]
    assert (s["added"], s["retired"]) == (["x"], ["y"])
    assert s["failed"] == ["z", "variants"]
    assert s["variants"] == {"error": "RuntimeError"}
    assert "database is locked" in run["log"]


def test_variants_page_shows_progress_and_never_the_key(client, tmp_path, monkeypatch):
    from frame_art import variants

    c, svc = client
    (tmp_path / "keys" / "openrouter").mkdir(parents=True)
    (tmp_path / "keys" / "openrouter" / "key").write_text("sk-secret\n")
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "keys"))
    cat = svc.catalogue()
    variants.seed_types(cat)
    cat.record_publish("nga-1-monet-haystacks.jpg", "full", "nga:1", 0)
    page = c.get("/variants").text
    assert "Halloween" in page and "Seasonal" in page
    assert "present" in page and "sk-secret" not in page
    assert "Haystacks" in page and "/thumb/full/nga-1-monet-haystacks.jpg" in page
    assert "/catalogue?tag=halloween%20version" in page


def test_variant_limit_saves_and_rejects(client):
    c, svc = client
    r = c.post("/variants/limit", data={"variant_limit": "5"})
    assert r.status_code == 200 and "Saved" in r.text
    assert svc.settings().variant_limit == 5
    r = c.post("/variants/limit", data={"variant_limit": "x"})
    assert r.status_code == 400 and "whole number" in r.text
    r = c.post("/variants/limit", data={"variant_limit": "99"})
    assert r.status_code == 400 and "0-50" in r.text
    assert svc.settings().variant_limit == 5


def test_variant_types_add_and_edit(client):
    from frame_art import variants

    c, svc = client
    variants.seed_types(svc.catalogue())
    r = c.post("/variants/types", data={"name": "Halloween", "prompt": "boo"})
    assert "already a type" in r.text
    r = c.post("/variants/types", data={"name": "", "prompt": "boo"})
    assert "give the type a name" in r.text
    assert (
        "Christmas"
        in c.post("/variants/types", data={"name": "Christmas", "prompt": "snow"}).text
    )
    assert [t.slug for t in svc.catalogue().variant_types()] == [
        "halloween",
        "christmas",
    ]
    c.post("/variants/types/christmas", data={"name": "Xmas", "prompt": "more snow"})
    t = svc.catalogue().variant_types()[1]
    assert (t.name, t.prompt) == ("Xmas", "more snow")
    assert (
        c.post("/variants/types/nope", data={"name": "a", "prompt": "b"}).status_code
        == 404
    )


def test_variants_generate_queues_a_job(client):
    from frame_art import variants

    c, svc = client
    variants.seed_types(svc.catalogue())
    r = c.post(
        "/variants/generate",
        data={"type": "halloween", "keys": ["nga:1", "nga:2"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/runs"
    [job] = svc.queued()
    assert job.kind == "generate"
    assert job.args == {"type": "halloween", "keys": ["nga:1", "nga:2"], "prompt": None}
    r = c.post("/variants/generate", data={"type": "halloween"})
    assert "choose at least one" in r.text
    assert len(svc.queued()) == 1


def _work_variant(svc, tmp_path):
    from PIL import Image

    from frame_art import variants

    cat = svc.catalogue()
    variants.seed_types(cat)
    path = tmp_path / "v.jpg"
    Image.new("RGB", (1920, 1080), "orange").save(path, "JPEG")
    return cat, cat.add_variant(
        "nga:1", "halloween", 1, "make it spooky", "g", 0.12, str(path)
    )


def test_work_page_renders_and_404s(client, tmp_path):
    c, svc = client
    assert c.get("/works/nga:1").status_code == 200
    assert c.get("/works/nga:999").status_code == 404
    cat, v = _work_variant(svc, tmp_path)
    default = next(t for t in cat.variant_types() if t.slug == "halloween").prompt
    page = c.get("/works/nga:1").text
    assert "make it spooky" in page and "$0.12" in page
    assert f"Default prompt: {html.escape(default)}" in page
    assert f"{html.escape(default)}</textarea>" in page
    assert f"/variant-file/{v.id}" in page


def test_variant_file_serves_by_id(client, tmp_path):
    c, svc = client
    cat, v = _work_variant(svc, tmp_path)
    r = c.get(f"/variant-file/{v.id}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    r = c.get(f"/variant-file/{v.id}?thumb=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (svc.data / "thumbs" / "variants" / f"{v.id}.jpg").exists()
    assert c.get("/variant-file/999").status_code == 404
    (tmp_path / "v.jpg").unlink()
    assert c.get(f"/variant-file/{v.id}").status_code == 404


def test_work_dont_use_queues_versions(client, tmp_path):
    c, svc = client
    cat, v = _work_variant(svc, tmp_path)
    r = c.post(
        "/works/nga:1/variants/halloween/dont-use",
        data={"on": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/works/nga:1"
    assert cat.variant_states()[("nga:1", "halloween")].dont_use
    [job] = svc.queued()
    assert job.kind == "versions" and job.args == {"keys": ["nga:1"]}
    c.post("/works/nga:1/variants/halloween/dont-use", data={"on": "0"})
    assert not cat.variant_states()[("nga:1", "halloween")].dont_use


def test_work_make_current_pins_slot_only_in_the_mix(client, tmp_path):
    c, svc = client
    cat, v1 = _work_variant(svc, tmp_path)
    v2 = cat.add_variant("nga:1", "halloween", 2, "again", "g", None, v1.path)
    cat.record_publish("nga-1-monet-haystacks.jpg", "full", "nga:1", 0)
    assert c.post(f"/works/nga:1/variants/{v1.id}/current").status_code == 200
    assert cat.variant_states()[("nga:1", "halloween")].current == v1.id
    assert "nga:1" not in cat.slots()  # halloween has no share
    svc.save_settings(Settings(schedules=[everyday({"halloween": 10})]))
    c.post(f"/works/nga:1/variants/{v2.id}/current")
    assert cat.slots()["nga:1"].manual
    assert {j.kind for j in svc.queued()} == {"versions"}
    assert c.post("/works/nga:2/variants/%d/current" % v2.id).status_code == 404


def test_work_generate_prompt(client, tmp_path):
    c, svc = client
    cat, _ = _work_variant(svc, tmp_path)
    default = next(t for t in cat.variant_types() if t.slug == "halloween").prompt
    for prompt in (default, "with bats"):
        r = c.post(
            "/works/nga:1/variants/generate",
            data={"type": "halloween", "prompt": prompt},
            follow_redirects=False,
        )
        assert r.status_code == 303
    first, second = svc.queued()
    assert first.args == {"type": "halloween", "keys": ["nga:1"], "prompt": None}
    assert second.args["prompt"] == "with bats"


def test_work_routes_keep_override_working(client):
    c, svc = client
    r = c.post(
        "/works/nga:1/override", data={"state": "pinned"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert svc.catalogue().overrides() == {"nga:1": "pinned"}


def test_work_posts_404_for_unknown_type_or_key(client, tmp_path):
    c, svc = client
    _work_variant(svc, tmp_path)
    for path, data in (
        ("/works/nga:1/variants/nope/dont-use", {"on": "1"}),
        ("/works/nga:999/variants/halloween/dont-use", {"on": "1"}),
        ("/works/nga:1/variants/generate", {"type": "nope"}),
        ("/works/nga:999/variants/generate", {"type": "halloween"}),
    ):
        assert c.post(path, data=data).status_code == 404, path
    assert not svc.queued()


def _show_variant(svc):
    import json

    cat = svc.catalogue()
    cat.add_variant_type("halloween", "Halloween", "boo")
    v = cat.add_variant("nga:1", "halloween", 1, "p", "m", None, "/d/1.jpg")
    cat.record_publish("haystacks--halloween-1.jpg", "full", "nga:1", 0, variant=v.id)
    (svc.dest / "full").mkdir(parents=True, exist_ok=True)
    sidecar = {
        "haystacks--halloween-1.jpg": {"content_id": "MY_F0099", "modified": 1.0}
    }
    (svc.dest / "full" / ".samsungtv_upload.json").write_text(json.dumps(sidecar))
    svc._on_current({"content_id": "MY_F0099", "on": True})


def test_now_showing_names_the_variant(client):
    c, svc = client
    svc.catalogue().record_publish("haystacks.jpg", "full", "nga:1", 0)
    _show_variant(svc)
    assert svc.now_showing()["variant"] == {"type": "Halloween", "n": 1}
    page = c.get("/now").text
    assert "Halloween version" in page and "/works/nga:1" in page


def test_history_names_the_variant(client):
    c, svc = client
    _show_variant(svc)
    assert "Halloween version" in c.get("/history").text


def test_history_shows_the_live_version_not_the_one_it_replaced(client):
    c, svc = client
    cat = svc.catalogue()
    cat.record_publish("haystacks.jpg", "full", "nga:1", 0)
    [orig] = cat.published()
    _show_variant(svc)
    # A swap keeps the original's publish time, so the two rows tie.
    cat.db.execute(
        "update published set published_at = ? where filename like 'haystacks--%'",
        (orig.published_at,),
    )
    cat.db.commit()
    cat.record_retire("haystacks.jpg", None)
    page = c.get("/history").text
    assert "/thumb/full/haystacks--halloween-1.jpg" in page
    assert "/thumb/full/haystacks.jpg" not in page


def test_history_reads_the_sidecars_once(client, monkeypatch):
    from frame_art import publish

    c, svc = client
    svc._on_current({"content_id": "SAM-S1", "on": True})
    svc._on_current({"content_id": "SAM-S2", "on": True})
    reads = []
    real = publish.read_sidecar

    def counting(folder):
        reads.append(folder.name)
        return real(folder)

    monkeypatch.setattr(publish, "read_sidecar", counting)
    assert c.get("/history").status_code == 200
    assert sorted(reads) == ["full", "matted"]


def test_catalogue_tags_works_with_a_seasonal_version(client, tmp_path):
    c, svc = client
    cat, v = _work_variant(svc, tmp_path)
    cat.upsert(
        [
            Work(
                source="nga", source_id="2", title="Water Lilies", artist="Monet",
                date="1916", kind="painting", license="CC0",
                iiif="https://example.org/iiif/2", width=4000, height=2250,
                subjects=["halloween"],
            )
        ]
    )  # fmt: skip
    page = c.get("/catalogue").text
    assert "Halloween version" in page and "Water Lilies" in page
    # The seasonal tag is its own tag: a subject called "halloween" doesn't match.
    page = c.get("/catalogue?tag=halloween+version").text
    assert "Haystacks" in page and "Water Lilies" not in page
    assert f"/variant-file/{v.id}?thumb=1" in page
    page = c.get("/collection?tag=halloween+version").text
    assert "Haystacks" not in page
    got = c.get("/tags.json?q=hallo").json()
    assert {"tag": "Halloween version", "kind": "seasonal", "count": 1} in got


def _schedule_form(name="Halloween", start=(10, 1), end=(10, 31), **shares):
    return {
        "name": name,
        "start_month": str(start[0]),
        "start_day": str(start[1]),
        "end_month": str(end[0]),
        "end_day": str(end[1]),
        **{f"share-{k}": v for k, v in shares.items()},
    }


def test_schedule_add_edit_delete(client):
    from frame_art import variants

    c, svc = client
    variants.seed_types(svc.catalogue())
    r = c.post("/variants/schedules", data=_schedule_form(halloween="50"))
    assert r.status_code == 200 and "Saved" in r.text
    [s] = svc.settings().schedules
    assert (s.name, s.start, s.end, s.shares) == (
        "Halloween", "10-01", "10-31", {"halloween": 50}
    )  # fmt: skip
    page = c.get("/variants").text
    assert f'href="/variants?edit={s.id}#editor"' in page
    assert "Halloween · 50%" in page
    editor = c.get(f"/variants?edit={s.id}").text
    assert 'value="Halloween"' in editor and "every year" in editor
    form = _schedule_form(name="Spooky", halloween="60")
    assert c.post(f"/variants/schedules/{s.id}", data=form).status_code == 200
    [s2] = svc.settings().schedules
    assert (s2.id, s2.name, s2.shares) == (s.id, "Spooky", {"halloween": 60})
    form = _schedule_form(name="Spooky")  # the row was removed
    c.post(f"/variants/schedules/{s.id}", data=form)
    assert svc.settings().schedules[0].shares == {}
    r = c.post(f"/variants/schedules/{s.id}/delete")
    assert r.status_code == 200 and "Deleted Spooky" in r.text
    assert svc.settings().schedules == []


def test_schedule_editor_rejects_and_shows_what_was_typed(client):
    from frame_art import variants

    c, svc = client
    cat = svc.catalogue()
    variants.seed_types(cat)
    cat.add_variant_type("fall", "Fall", "make it autumn")
    r = c.post("/variants/schedules", data=_schedule_form(name="Boo", halloween="x"))
    assert r.status_code == 400 and "not a whole number" in r.text
    assert 'value="x"' in r.text and 'value="Boo"' in r.text
    form = _schedule_form(start=(2, 30), halloween="5")
    r = c.post("/variants/schedules", data=form)
    assert r.status_code == 400 and "not a real date" in r.text
    form = _schedule_form(start=(13, 1), halloween="5")
    assert c.post("/variants/schedules", data=form).status_code == 400
    r = c.post("/variants/schedules", data=_schedule_form(name=" ", halloween="5"))
    assert r.status_code == 400 and "needs a name" in r.text
    form = _schedule_form(halloween="60", fall="50")
    r = c.post("/variants/schedules", data=form)
    assert r.status_code == 400 and "at most 100" in r.text
    assert svc.settings().schedules == []


def test_schedule_unknown_id_is_404(client):
    c, _ = client
    form = _schedule_form(halloween="5")
    assert c.post("/variants/schedules/nope", data=form).status_code == 404
    assert c.post("/variants/schedules/nope/delete").status_code == 404
    assert c.get("/variants?edit=nope").status_code == 200  # no editor, no error


def test_editor_lists_only_types_not_in_the_schedule(client):
    from frame_art import variants
    from frame_art.schedules import Schedule

    c, svc = client
    cat = svc.catalogue()
    variants.seed_types(cat)
    cat.add_variant_type("fall", "Fall", "make it autumn")
    s = Schedule("abc", "Halloween", "10-01", "10-31", {"halloween": 50})
    svc.save_settings(Settings(schedules=[s]))
    page = c.get("/variants?edit=abc").text
    assert 'name="share-halloween"' in page
    assert '<option value="fall">' in page
    assert '<option value="halloween">' not in page
    assert "+ Add a type" in page and "Delete schedule" in page
    assert "This schedule takes <b" in page
    new = c.get("/variants?edit=new").text
    assert '<option value="halloween">' in new and "Delete schedule" not in new


def test_today_strip_and_deleted_type(client, monkeypatch):
    import datetime

    from frame_art import variants
    from frame_art.schedules import Schedule

    c, svc = client
    variants.seed_types(svc.catalogue())
    monkeypatch.setattr(svc, "today", lambda: datetime.date(2026, 10, 28))
    s = Schedule("abc", "Spooky", "10-01", "10-31", {"halloween": 50, "zzgone": 40})
    svc.save_settings(Settings(schedules=[s]))
    page = c.get("/variants").text
    assert "Today’s screen" in page and "target 50%" in page
    assert "Spooky · 50%" in page and "zzgone" not in page
    assert "zzgone" not in c.get("/variants?edit=abc").text
    # Not running today: N of M counts against its next start (Dec 1).
    svc.catalogue().record_publish("nga-1-monet-haystacks.jpg", "full", "nga:1", 0)
    s = Schedule("xyz", "Xmas", "12-01", "12-26", {"halloween": 100})
    svc.save_settings(Settings(schedules=[s]))
    assert "0 of 1 ready" in c.get("/variants?edit=xyz").text


def test_wrapping_schedule_draws_two_bars(client):
    from frame_art.schedules import Schedule

    c, svc = client
    s = Schedule("w", "Holidays", "12-20", "01-05", {})
    svc.save_settings(Settings(schedules=[s]))
    page = c.get("/variants").text
    assert page.count('class="bar c') == 2
    # Two bars, the side label and the Edit button.
    assert page.count('href="/variants?edit=w#editor"') == 4


def test_schedule_list_has_an_edit_button_per_schedule(client):
    from frame_art import variants
    from frame_art.schedules import Schedule

    c, svc = client
    variants.seed_types(svc.catalogue())
    a = Schedule("a", "Halloween", "10-01", "10-31", {"halloween": 100})
    b = Schedule("b", "Holidays", "12-20", "01-05", {})
    svc.save_settings(Settings(schedules=[a, b]))
    page = c.get("/variants").text
    assert "Click a schedule to edit it." in page
    items = page.split('<ul class="schedule-list">')[1].split("</ul>")[0]
    assert "Oct 1 – Oct 31" in items and "Halloween 100%" in items
    assert "Dec 20 – Jan 5" in items and "Originals only" in items
    assert '<a class="button" href="/variants?edit=a#editor">Edit</a>' in items
    assert '<a class="button" href="/variants?edit=b#editor">Edit</a>' in items


def test_n_of_m_counts_only_this_schedules_portion(client, monkeypatch):
    import datetime

    from frame_art import variants
    from frame_art.schedules import Schedule

    c, svc = client
    cat = svc.catalogue()
    variants.seed_types(cat)
    for i in range(4):
        cat.record_publish(f"nga-{i}-x.jpg", "full", f"nga:{i}", 0)
    monkeypatch.setattr(svc, "today", lambda: datetime.date(2026, 10, 28))
    every = Schedule("e", "Everyday", "01-01", "12-31", {"halloween": 50})
    spooky = Schedule("o", "Spooky", "10-01", "10-31", {"halloween": 50})
    svc.save_settings(Settings(schedules=[every, spooky]))
    # Spooky takes 2 of the 4; Everyday takes half of the other 2.
    assert "0 of 2 ready" in c.get("/variants?edit=o").text
    assert "0 of 1 ready" in c.get("/variants?edit=e").text


def _jpeg(color="red") -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return buf.getvalue()


def test_upload_lists_and_dedupes(client):
    c, svc = client
    files = [("files", ("a.jpg", _jpeg(), "image/jpeg")),
             ("files", ("b.jpg", _jpeg(), "image/jpeg"))]  # fmt: skip
    r = c.post("/photos/upload", files=files)
    assert r.status_code == 200 and "Added 1" in r.text and "already here" in r.text
    [p] = svc.photos().all()
    page = c.get("/photos").text
    assert f'src="/photos/{p.id}/thumb"' in page and "Choose a kind" in page
    thumb = c.get(f"/photos/{p.id}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"


def test_upload_refuses_non_images_and_keeps_the_rest(client):
    c, svc = client
    files = [("files", ("clip.mov", b"\x00\x00\x00 ftypqt", "video/quicktime")),
             ("files", ("a.jpg", _jpeg(), "image/jpeg"))]  # fmt: skip
    r = c.post("/photos/upload", files=files)
    assert "clip.mov" in r.text and "not a photo" in r.text
    assert len(svc.photos().all()) == 1


def test_upload_refuses_oversized_files(client, monkeypatch):
    from frame_art import web

    c, svc = client
    monkeypatch.setattr(web, "MAX_UPLOAD", 100)
    r = c.post("/photos/upload", files=[("files", ("big.jpg", _jpeg(), "image/jpeg"))])
    assert "big.jpg" in r.text and "too large" in r.text
    assert svc.photos().all() == []


def test_kind_override_and_delete(client):
    c, svc = client
    c.post("/photos/upload", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    [p] = svc.photos().all()
    c.post(f"/photos/{p.id}/kind", data={"kind": "portrait"})
    got = svc.photos().get(p.id)
    assert got.kind == "portrait" and not got.kind_auto
    assert c.post(f"/photos/{p.id}/kind", data={"kind": "nope"}).status_code == 400
    assert c.post("/photos/nope/kind", data={"kind": "scene"}).status_code == 404
    assert c.post(f"/photos/{p.id}/delete").status_code == 200
    assert svc.photos().all() == []
    assert c.get(f"/photos/{p.id}/thumb").status_code == 404


def test_photos_stay_off_public_pages(client):
    c, svc = client
    files = [("files", ("secret-name.jpg", _jpeg(), "image/jpeg"))]
    c.post("/photos/upload", files=files)
    for page in ("/", "/catalogue", "/pool", "/collection", "/variants"):
        assert "secret-name" not in c.get(page).text
    assert not svc.dest.exists() or not any(svc.dest.rglob("*"))


def _google_client(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    monkeypatch.setenv("FRAME_ART_KEYS", str(keys))
    (keys / "google-photos").mkdir(parents=True)
    (keys / "google-photos" / "client.json").write_text(
        '{"installed": {"client_id": "cid", "client_secret": "SEKRIT",'
        ' "auth_uri": "https://accounts.google.com/o/oauth2/auth",'
        ' "token_uri": "https://oauth2.googleapis.com/token"}}'
    )


def test_google_box_without_a_client_asks_for_setup(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "no-keys"))
    assert "Set up Google Photos" in c.get("/photos").text


def test_google_sign_in_never_shows_the_secret(client, tmp_path, monkeypatch):
    c, svc = client
    _google_client(tmp_path, monkeypatch)
    page = c.get("/photos").text
    assert "Sign in" in page and "SEKRIT" not in page
    r = c.get("/photos/google/signin")
    assert r.status_code == 200 and "code_challenge" in r.text
    assert "SEKRIT" not in r.text
    assert svc.catalogue().get("google_signin")["verifier"] not in r.text
    r = c.post("/photos/google/signin",
               data={"pasted": "http://127.0.0.1:8765/?state=WRONG&code=C"})  # fmt: skip
    assert r.status_code == 400 and "didn't match" in html.unescape(r.text)
    assert "SEKRIT" not in r.text


def test_google_sign_in_that_has_expired(client, tmp_path, monkeypatch):
    c, svc = client
    _google_client(tmp_path, monkeypatch)
    c.get("/photos/google/signin")
    started = svc.catalogue().get("google_signin")
    svc.catalogue().put("google_signin", {**started, "at": started["at"] - 16 * 60})
    r = c.post("/photos/google/signin",
               data={"pasted": f"http://127.0.0.1:8765/?state={started['state']}&code=C"})  # fmt: skip
    assert r.status_code == 400 and "Start again" in r.text


def test_google_sign_out_forgets_the_token(client, tmp_path, monkeypatch):
    c, svc = client
    _google_client(tmp_path, monkeypatch)
    token = svc.data / "google-photos" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"access_token": "A", "refresh_token": "R", "expires_at": 0}')
    assert "Signed in to Google Photos" in c.get("/photos").text
    r = c.post("/photos/google/signout")
    assert r.status_code == 200 and not token.exists() and "Sign in" in r.text


def _signed_in_with_pick(client, tmp_path, monkeypatch, expires_in=600):
    c, svc = client
    _google_client(tmp_path, monkeypatch)
    from frame_art import google_photos as gp

    monkeypatch.setattr(gp, "access_token", lambda http, client, path: "TOK")
    svc.google_token.parent.mkdir(parents=True, exist_ok=True)
    svc.google_token.write_text(
        '{"access_token": "A", "refresh_token": "R", "expires_at": 0}'
    )
    svc.catalogue().put("google_pick", {"id": "s1", "uri": "https://photos.google.com/picker/x",
                                        "expires": time.time() + expires_in})  # fmt: skip
    submitted = []
    monkeypatch.setattr(
        svc, "submit", lambda kind, args=None: submitted.append((kind, args))
    )
    return c, svc, gp, submitted


def test_pending_pick_offers_the_picker_and_refreshes(client, tmp_path, monkeypatch):
    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch)
    monkeypatch.setattr(gp, "session_done", lambda http, token, id: False)
    page = c.get("/photos").text
    assert "Open Google Photos" in page and "https://photos.google.com/picker/x" in page
    assert 'http-equiv="refresh"' in page and "Cancel" in page
    assert submitted == [] and svc.catalogue().get("google_pick")


def test_finished_pick_queues_the_import(client, tmp_path, monkeypatch):
    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch)
    monkeypatch.setattr(gp, "session_done", lambda http, token, id: True)
    c.get("/photos").text
    assert submitted == [("import-google", {"session": "s1"})]
    assert not svc.catalogue().get("google_pick")


def test_page_keeps_refreshing_until_picked_photos_arrive(
    client, tmp_path, monkeypatch
):
    from frame_art.service import Job

    c, svc = client
    page = c.get("/photos").text
    assert 'http-equiv="refresh"' not in page
    for kind in ("import-google", "classify-photos"):
        monkeypatch.setattr(svc, "queued", lambda kind=kind: [Job(kind, None)])
        page = c.get("/photos").text
        assert 'http-equiv="refresh"' in page and "Adding your photos" in page


def test_expired_pick_is_dropped_without_an_import(client, tmp_path, monkeypatch):
    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch, -1)
    deleted = []
    monkeypatch.setattr(gp, "session_done", lambda http, token, id: True)
    monkeypatch.setattr(
        gp, "delete_session", lambda http, token, id: deleted.append(id)
    )
    c.get("/photos")
    assert (
        submitted == [] and deleted == ["s1"] and not svc.catalogue().get("google_pick")
    )


def test_cancelled_pick_is_dropped(client, tmp_path, monkeypatch):
    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch)
    deleted = []
    monkeypatch.setattr(
        gp, "delete_session", lambda http, token, id: deleted.append(id)
    )
    r = c.post("/photos/google/cancel")
    assert r.status_code == 200 and deleted == ["s1"] and submitted == []
    assert not svc.catalogue().get("google_pick")


def test_ended_sign_in_during_a_pick_asks_to_sign_in_again(
    client, tmp_path, monkeypatch
):
    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch)

    def ended(http, client, path):
        path.unlink()  # as the real one does on invalid_grant
        raise gp.SignInNeeded("invalid_grant")

    monkeypatch.setattr(gp, "access_token", ended)
    r = c.get("/photos")
    assert r.status_code == 200 and "Sign in again" in r.text
    assert submitted == [] and not svc.catalogue().get("google_pick")


def test_uploads_with_the_same_name_are_different_photos(client):
    c, svc = client
    c.post(
        "/photos/upload",
        files=[("files", ("IMG_0001.jpg", _jpeg("red"), "image/jpeg"))],
    )
    r = c.post("/photos/upload",
               files=[("files", ("IMG_0001.jpg", _jpeg("blue"), "image/jpeg"))])  # fmt: skip
    assert "Added 1." in r.text and len(svc.photos().all()) == 2


def test_upload_refusals_stay_on_screen(client, monkeypatch):
    from frame_art.service import Job

    c, svc = client
    monkeypatch.setattr(svc, "queued", lambda: [Job("classify-photos", None)])
    files = [("files", ("ok.jpg", _jpeg(), "image/jpeg")),
             ("files", ("clip.mov", b"not a picture", "video/quicktime"))]  # fmt: skip
    r = c.post("/photos/upload", files=files)
    assert "clip.mov: not a photo" in r.text and 'http-equiv="refresh"' not in r.text


def test_google_calls_stay_off_the_event_loop(client, tmp_path, monkeypatch):
    import asyncio

    c, svc, gp, submitted = _signed_in_with_pick(client, tmp_path, monkeypatch)
    on_loop = []

    def done(http, token, id):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return False

    monkeypatch.setattr(gp, "session_done", done)
    monkeypatch.setattr(gp, "create_session", lambda http, token: done(http, token, "")
                        or gp.PickSession("s2", "https://x", time.time() + 60))  # fmt: skip
    monkeypatch.setattr(
        gp, "delete_session", lambda http, token, id: done(http, token, id)
    )
    c.get("/photos")
    c.post("/photos/google/cancel")
    c.post("/photos/google/pick")
    assert on_loop and not any(on_loop)  # the pick redirects to /photos: 4 calls


def test_painters_page_add_edit_delete(client):
    c, svc = client
    page = c.get("/studio/painters").text
    assert "John Singer Sargent" in page and "Salvador Dal" in page
    r = c.post("/studio/painters",
               data={"name": "Mary Cassatt", "manner": "Soft pastel light.", "suits": "portrait"})  # fmt: skip
    assert r.status_code == 200 and "Mary Cassatt" in r.text
    r = c.post("/studio/painters/mary-cassatt",
               data={"name": "Mary Cassatt", "manner": "Pastel.", "suits": ["portrait", "scene"]})  # fmt: skip
    assert svc.studio().painter("mary-cassatt").suits == ("portrait", "scene")
    r = c.post("/studio/painters/mary-cassatt/delete")
    assert svc.studio().painter("mary-cassatt") is None
    r = c.post("/studio/painters", data={"name": "", "manner": "x"})
    assert r.status_code == 400 and "name" in r.text


def _photo_in_studio(svc, kind=None):
    p = svc.photos().add("upload", "studio-1", _jpeg(), "kid.jpg")
    if kind:
        svc.photos().set_kind(p.id, kind, None, False)
    return p


def test_paint_queues_one_job_and_redirects(client, monkeypatch):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    submitted, real = [], svc.submit
    monkeypatch.setattr(svc, "submit", lambda kind, args=None: submitted.append(
        (kind, args)) or real(kind, args))  # fmt: skip
    r = c.post(f"/studio/{p.id}/paint", follow_redirects=False,
               data={"painter": "hopper", "setting": "keep", "shape": "fill",
                     "nudge": ["warmer light"], "nudge_text": "less desert"})  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == f"/studio/{p.id}"
    (t,) = svc.studio().tries(p.id)
    assert submitted == [("paint", {"try": t.id})]
    assert t.nudges == ["warmer light", "less desert"] and t.painter == "Edward Hopper"
    page = c.get(f"/studio/{p.id}").text
    assert "data-poll" in page and "Edward Hopper" in page
    assert "Waiting for other jobs" in page


def test_studio_form_keeps_the_last_choices(client, monkeypatch):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    monkeypatch.setattr(svc, "submit", lambda kind, args=None: None)
    c.post(f"/studio/{p.id}/paint",
           data={"painter": "hopper", "setting": "reinvent", "shape": "keep",
                 "nudge": ["warmer light"], "nudge_text": "less desert"})  # fmt: skip
    page = c.get(f"/studio/{p.id}").text
    assert '<option value="hopper" selected>' in page
    assert 'value="reinvent" checked' in page and 'value="keep" checked' in page
    assert 'value="warmer light" checked' in page
    assert 'value="more painterly" checked' not in page
    assert 'name="nudge_text" value="less desert"' in page
    c.post(f"/studio/{p.id}/paint",
           data={"painter": "hopper", "painter_name": "Mary Cassatt",
                 "setting": "keep", "shape": "fill"})  # fmt: skip
    page = c.get(f"/studio/{p.id}").text
    assert 'name="painter_name" value="Mary Cassatt"' in page
    assert "selected>" not in page.split('name="painter"')[1].split("</select>")[0]


def test_studio_form_keeps_choices_when_refused(client):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    r = c.post(f"/studio/{p.id}/paint",
               data={"painter": "dali", "shape": "keep", "nudge_text": "beach"})  # fmt: skip
    assert r.status_code == 400
    assert '<option value="dali" selected>' in r.text
    assert 'value="keep" checked' in r.text and 'value="beach"' in r.text


def test_studio_shows_a_failed_paint_plainly(client, tmp_path, monkeypatch):
    c, svc = client
    keys = tmp_path / "keys"
    (keys / "openrouter").mkdir(parents=True)
    (keys / "openrouter" / "key").write_text("sk-SEKRIT")
    monkeypatch.setenv("FRAME_ART_KEYS", str(keys))
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    st.fail_try(t.id, "OpenRouter: balance too low")
    page = c.get(f"/studio/{p.id}").text
    assert "OpenRouter: balance too low" in page and 'http-equiv="refresh"' not in page
    assert "SEKRIT" not in page


def test_studio_defaults_follow_the_kind(client):
    c, svc = client
    p = _photo_in_studio(svc, "scene")
    page = c.get(f"/studio/{p.id}").text
    assert 'value="reinvent" checked' in page and 'value="fill" checked' in page
    assert page.index("Claude Monet") < page.index("Edward Hopper")
    assert c.get("/studio/nope").status_code == 404
    assert f"/studio/{p.id}" in c.get("/studio").text


def test_nightly_prunes_old_studio_tries(client, tmp_path, monkeypatch):
    from frame_art import jobs, variants
    from frame_art.publish import PublishResult
    from frame_art.service import Job

    _, svc = client
    monkeypatch.setenv("FRAME_ART_KEYS", str(tmp_path / "no-keys"))
    monkeypatch.setattr(jobs, "crawl", lambda *a, **k: {})
    monkeypatch.setattr(jobs, "publish", lambda *a, **k: PublishResult([], [], []))
    monkeypatch.setattr(variants, "nightly", lambda *a, **k: {
        "slots": {}, "generated": [], "gen_failed": [], "swapped": [],
        "swap_failed": [], "cost": 0.0})  # fmt: skip
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    st.db.execute("update tries set made_at = 0 where id = ?", (t.id,))
    st.db.commit()
    svc._run(Job("nightly"))
    assert svc.studio().get_try(t.id) is None
    assert svc.catalogue().runs(1)[0]["summary"]["tries_pruned"] == 1


def test_paint_job_for_a_deleted_try_is_quiet(client, tmp_path, monkeypatch):
    c, svc = client
    keys = tmp_path / "keys"
    (keys / "openrouter").mkdir(parents=True)
    (keys / "openrouter" / "key").write_text("sk-test")
    monkeypatch.setenv("FRAME_ART_KEYS", str(keys))
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    st.delete_try(t.id)
    assert svc._paint(t.id) == {"try": t.id, "status": "deleted"}


def _done_try(svc, p, painter="monet"):
    st = svc.studio()
    t = st.start_try(p.id, st.painter(painter), "keep", "fill", [], None)
    st.finish_try(t.id, Image.new("RGB", (64, 36), "orange"), 0.12)
    return st.get_try(t.id)


def test_save_a_try_to_portraits(client):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    t = _done_try(svc, p)
    page = c.get(f"/studio/{p.id}").text
    assert f'action="/studio/tries/{t.id}/save"' in page
    r = c.post(f"/studio/tries/{t.id}/save", follow_redirects=False)
    assert (
        r.status_code == 303 and r.headers["location"] == f"/studio/{p.id}?done=saved"
    )
    page = c.get(r.headers["location"]).text
    assert "Saved to Portraits." in page
    assert f'action="/studio/tries/{t.id}/save"' not in page
    assert f'action="/studio/tries/{t.id}/delete"' not in page
    (painting,) = svc.studio().paintings()
    assert (
        "Portraits from this photo" in page
        and f"/portraits/{painting.id}/thumb" in page
    )
    assert c.post("/studio/tries/999/save").status_code == 404


def test_delete_a_try(client):
    c, svc = client
    p = _photo_in_studio(svc)
    t = _done_try(svc, p)
    page = c.get(f"/studio/{p.id}").text
    assert f'action="/studio/tries/{t.id}/delete"' in page
    r = c.post(f"/studio/tries/{t.id}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/studio/{p.id}?done=deleted"
    assert "Try deleted." in c.get(r.headers["location"]).text
    assert svc.studio().get_try(t.id) is None and not Path(t.path).exists()
    kept = _done_try(svc, p)
    svc.studio().save(kept.id)
    assert c.post(f"/studio/tries/{kept.id}/delete").status_code == 400


def test_a_try_still_painting_has_no_delete(client):
    c, svc = client
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    st.begin_try(t.id)
    assert f"/studio/tries/{t.id}/delete" not in c.get(f"/studio/{p.id}").text
    assert c.post(f"/studio/tries/{t.id}/delete").status_code == 400


def test_deleting_a_photo_keeps_its_portraits(client):
    c, svc = client
    p = _photo_in_studio(svc)
    t, other = _done_try(svc, p), _done_try(svc, p)
    painting = svc.studio().save(t.id)
    c.post(f"/photos/{p.id}/delete")
    assert svc.studio().tries(p.id) == [] and not Path(other.path).exists()
    assert svc.studio().get_painting(painting.id).photo_id is None
    assert c.get(f"/studio/{p.id}").status_code == 404
    page = c.get("/portraits").text
    assert f"/portraits/{painting.id}/thumb" in page and "photo deleted" in page


def test_portraits_page_links_and_removes(client):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    painting = svc.studio().save(_done_try(svc, p, "hopper").id)
    page = c.get("/portraits").text
    assert "Edward Hopper" in page and "$0.12" in page
    assert f'href="/studio/{p.id}"' in page and f"/photos/{p.id}/thumb" in page
    assert 'href="/portraits"' in c.get("/").text
    assert (
        c.get(f"/portraits/{painting.id}.jpg").content
        == Path(painting.path).read_bytes()
    )
    assert c.get(f"/portraits/{painting.id}/thumb").status_code == 200
    r = c.post(f"/portraits/{painting.id}/remove")
    assert r.status_code == 200 and "Removed" in r.text
    assert svc.studio().get_painting(painting.id) is None
    assert not Path(painting.path).exists()
    assert c.get(f"/portraits/{painting.id}.jpg").status_code == 404
    assert c.post(f"/portraits/{painting.id}/remove").status_code == 404


def test_photos_page_counts_tries_and_portraits(client):
    c, svc = client
    p = _photo_in_studio(svc)
    svc.studio().save(_done_try(svc, p).id)
    _done_try(svc, p)
    assert "2 tries · 1 portrait" in c.get("/photos").text


def test_paintings_stay_off_the_museum_pages(client):
    c, svc = client
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, studio.Painter("", "Mary Cassatt", "", ()), "keep",
                     "fill", [], None)  # fmt: skip
    st.finish_try(t.id, Image.new("RGB", (64, 36), "orange"), 0.12)
    painting = st.save(t.id)
    for page in ("/", "/catalogue", "/pool", "/collection", "/variants", "/history"):
        text = c.get(page).text
        assert "Mary Cassatt" not in text and painting.path not in text, page


def test_schedule_with_a_portraits_share(client):
    from frame_art import variants

    c, svc = client
    variants.seed_types(svc.catalogue())
    editor = c.get("/variants?edit=new").text
    assert '<option value="portraits">Portraits</option>' in editor
    r = c.post(
        "/variants/schedules", data=_schedule_form(halloween="50", portraits="20")
    )
    assert r.status_code == 200 and "Saved" in r.text
    [s] = svc.settings().schedules
    assert s.shares == {"halloween": 50, "portraits": 20}
    page = c.get("/variants").text
    items = page.split('<ul class="schedule-list">')[1].split("</ul>")[0]
    assert "Portraits 20%" in items
    editor = c.get(f"/variants?edit={s.id}").text
    assert "Portraits (saved Studio paintings)" in editor
    assert 'name="share-portraits" min="0" max="100" step="1" value="20"' in editor


def _live_portrait(svc, content_id="MY_F0042"):
    from frame_art import portraits

    p = _photo_in_studio(svc)
    painting = svc.studio().save(_done_try(svc, p, "hopper").id)
    cat = svc.catalogue()
    pub = publish.Publisher(cat, None, svc.dest, "blur")
    portraits.sync(cat, svc.studio(), pub, svc.dest, want=1, seed=1)
    name = f"portrait-{painting.id}.jpg"
    (svc.dest / "full" / publish.SIDECAR).write_text(
        json.dumps({name: {"content_id": content_id, "modified": 1.0}})
    )
    return painting, name


def test_now_showing_a_portrait_says_only_portrait(client):
    c, svc = client
    painting, name = _live_portrait(svc)
    cat = svc.catalogue()
    cat.put("tv_current", {"content_id": "MY_F0042", "at": time.time()})
    info = svc.now_showing(cat)
    assert info == {**info, "ours": True, "kind": "portrait", "title": "Portrait",
                    "file": f"full/{name}"}  # fmt: skip
    assert "Edward Hopper" not in json.dumps(info) and "kid.jpg" not in json.dumps(info)


def test_history_lists_a_portrait_as_portrait(client):
    c, svc = client
    _live_portrait(svc)
    cat = svc.catalogue()
    cat.note_shown("MY_F0042", None, True)
    page = c.get("/history").text
    assert (
        '<div class="title">Portrait</div>' in page and "Not from frame-art" not in page
    )
    assert "Edward Hopper" not in page


def test_removing_a_portrait_updates_the_tv(client, monkeypatch):
    c, svc = client
    painting, _ = _live_portrait(svc)
    submitted = []
    monkeypatch.setattr(
        svc, "submit", lambda kind, args=None: submitted.append((kind, args))
    )
    c.post(f"/portraits/{painting.id}/remove")
    assert submitted == [("portraits", None)]


def test_studio_polls_the_tries_without_reloading_the_form(client, monkeypatch):
    c, svc = client
    p = _photo_in_studio(svc, "portrait")
    c.post(f"/studio/{p.id}/paint",
           data={"painter": "hopper", "setting": "keep", "shape": "fill"})  # fmt: skip
    page = c.get(f"/studio/{p.id}").text
    assert 'http-equiv="refresh"' not in page
    results = page.split('id="studio-results"')[1]
    assert 'id="paint"' not in results and "data-poll" in page
    # A refused paint while another paints: no reload onto the POST-only URL.
    r = c.post(f"/studio/{p.id}/paint", data={"painter": "hopper"})
    assert r.status_code == 400 and 'http-equiv="refresh"' not in r.text


def test_a_queued_try_is_not_stuck_however_long_it_waits(client):
    c, svc = client
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    svc.submit("paint", {"try": t.id})
    st.db.execute(
        "update tries set made_at = ? where id = ?", (time.time() - 3600, t.id)
    )
    st.db.commit()
    page = c.get(f"/studio/{p.id}").text
    assert "never finished" not in page and "Waiting for other jobs" in page
    # Deleting a queued try cancels it: the job then finds nothing to paint.
    assert f"/studio/tries/{t.id}/delete" in page
    painting = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    st.begin_try(painting.id)
    st.db.execute("update tries set started_at = ? where id = ?",
                  (time.time() - 3600, painting.id))  # fmt: skip
    st.db.commit()
    assert "This try never finished." in c.get(f"/studio/{p.id}").text


def test_a_queued_try_whose_job_was_lost_never_started(client):
    c, svc = client
    p = _photo_in_studio(svc)
    st = svc.studio()
    t = st.start_try(p.id, st.painter("monet"), "keep", "fill", [], None)
    page = c.get(f"/studio/{p.id}").text  # no job: the service restarted
    assert "This try never started." in page and "data-poll" not in page
    assert f"/studio/tries/{t.id}/delete" in page
