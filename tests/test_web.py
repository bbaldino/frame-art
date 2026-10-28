import dataclasses
import html
import time

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


def test_mix_validates(tmp_path):
    assert Settings(mix={"halloween": 50}).validate() == []
    assert Settings(mix={"halloween": 60, "autumn": 50}).validate()
    assert Settings(mix={"halloween": -1}).validate()
    assert Settings(variant_limit=51).validate()
    assert Settings(mix={"halloween": 30}).originals_share() == 70


def test_saving_settings_keeps_the_mix(client):
    c, svc = client
    svc.save_settings(
        dataclasses.replace(svc.settings(), mix={"halloween": 50}, variant_limit=4)
    )
    form = {"crop": "medium", "pad_style": "blur", "kinds": "painting",
            "sources": "nga", "count": "5", "pool": "100", "run_at": "03:30",
            "recrawl_days": "7", "crawl_limit": "300"}  # fmt: skip
    assert "Saved" in c.post("/settings", data=form).text
    assert svc.settings().mix == {"halloween": 50}
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
    svc.save_settings(Settings(mix={"halloween": 10}))
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

    def fake_nightly(cat, http, data, pub, settings, api_key):
        calls.append(api_key)
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


def test_variants_mix_saves_and_rejects(client):
    from frame_art import variants

    c, svc = client
    variants.seed_types(svc.catalogue())
    r = c.post("/variants/mix", data={"share-halloween": "50", "variant_limit": "5"})
    assert r.status_code == 200 and "Saved" in r.text
    assert svc.settings().mix == {"halloween": 50}
    assert svc.settings().variant_limit == 5
    r = c.post("/variants/mix", data={"share-halloween": "150", "variant_limit": "5"})
    assert "0-100" in r.text
    assert svc.settings().mix == {"halloween": 50}
    r = c.post("/variants/mix", data={"share-halloween": "x", "variant_limit": "5"})
    assert "not a whole number" in r.text
    assert svc.settings().mix == {"halloween": 50}


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
    svc.save_settings(Settings(mix={"halloween": 10}))
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
