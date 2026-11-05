import dataclasses
import datetime

import pytest
from starlette.testclient import TestClient

from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.schedules import Schedule, Slice
from frame_art.service import Service
from frame_art.web import create_app

DEC_10 = datetime.date(2026, 12, 10)


def work(sid, title, **kw) -> Work:
    return Work(
        source="nga", source_id=sid, title=title, artist="Someone", date="1900",
        kind="painting", license="CC0", iiif=f"https://example.org/iiif/{sid}",
        width=4000, height=2250, **kw,
    )  # fmt: skip


@pytest.fixture
def sched(tmp_path, monkeypatch):
    data, dest = tmp_path / "data", tmp_path / "out"
    cat = Catalogue(data / "frame-art.db")
    cat.upsert([work("1", "Haystacks"), work("2", "Adoration")])
    cat.set_library(["nga:1", "nga:2"], "in")
    cat.add_variant_type("christmas", "Christmas", "make it festive")
    v = cat.add_variant("nga:1", "christmas", 1, "p", "m", 0.12, "/nowhere.jpg")
    svc = Service(data, dest, "/config/frame_art_pool", None, 1883, "test")
    monkeypatch.setattr(svc, "today", lambda: DEC_10)
    svc.save_settings(
        dataclasses.replace(
            svc.settings(),
            schedules=[
                Schedule(
                    "e", "Everyday", "01-01", "12-31", [Slice(["kind:painting"], 100)]
                ),
                Schedule(
                    "x",
                    "Christmas",
                    "12-01",
                    "12-31",
                    [Slice(["ai-variant:christmas"], 50)],
                ),
            ],
        )
    )
    return TestClient(create_app(svc)), svc, v


def wants_all(svc):
    """Christmas takes the whole screen, so nga:2 needs a version too."""
    s = svc.settings()
    x = dataclasses.replace(
        s.schedules[1], slices=[Slice(["ai-variant:christmas"], 100)]
    )
    svc.save_settings(dataclasses.replace(s, schedules=[s.schedules[0], x]))


def form(name="Spring", **extra):
    return {
        "name": name, "start_month": "3", "start_day": "1",
        "end_month": "5", "end_day": "31", **extra,
    }  # fmt: skip


def test_schedule_page_renders_slices_and_counts(sched):
    c, svc, v = sched
    cat = svc.catalogue()
    cat.record_publish("a.jpg", "full", "nga:2", 0)
    cat.record_publish("b.jpg", "full", "zzz:9", 0)  # not an item at all
    page = c.get("/schedule").text
    assert "Today on the TV" in page and "Christmas" in page and "Everyday" in page
    assert "1 ready of 1 wanted" in page and "made up to a week before" in page
    assert "2 in Library, 1 wanted" in page
    assert "/library?tag=kind%3Apainting" in page
    assert "/library?tag=ai-variant%3Achristmas&amp;versions=1" in page
    assert "not in today" in page
    assert 'class="tl-today"' in page
    # The Library (2 works) is smaller than the pool.
    assert "Library has 2 works" in page and "pool" in page
    # Rest of the Library comes last, and counts nothing here.
    assert page.index("Everyday") < page.index("Rest of the Library")


def test_schedule_page_for_a_schedule_not_running_today(sched):
    c, svc, _ = sched
    svc.save_settings(
        dataclasses.replace(
            svc.settings(),
            schedules=[
                Schedule(
                    "h", "Halloween", "10-01", "10-31", [Slice(["kind:painting"], 40)]
                )
            ],
        )
    )
    page = c.get("/schedule?edit=h").text
    assert "Halloween" in page and "2 in Library" in page
    assert 'name="slice_tags_0"' in page and 'name="slice_tags_1"' in page
    assert 'name="slice_tags_2"' not in page  # the saved slice plus one blank row


def test_add_save_and_delete_schedule(sched):
    c, svc, _ = sched
    r = c.post("/schedule/schedules", data=form("Spring"))
    assert r.status_code == 200 and "Saved Spring" in r.text
    spring = next(s for s in svc.settings().schedules if s.name == "Spring")
    assert (spring.start, spring.end, spring.slices) == ("03-01", "05-31", [])
    r = c.post(
        f"/schedule/schedules/{spring.id}",
        data=form(
            "Spring",
            slice_tags_0="Kind:Painting, style:impressionism", slice_share_0="60",
            slice_tags_1="", slice_share_1="",
        ),
    )  # fmt: skip
    assert r.status_code == 200
    saved = next(s for s in svc.settings().schedules if s.id == spring.id)
    assert saved.slices == [Slice(["kind:painting", "style:impressionism"], 60)]
    assert c.post("/schedule/schedules/nope", data=form()).status_code == 404
    assert c.post("/schedule/schedules/nope/delete").status_code == 404
    r = c.post(f"/schedule/schedules/{spring.id}/delete")
    assert r.status_code == 200 and "Deleted Spring" in r.text
    assert all(s.id != spring.id for s in svc.settings().schedules)


def test_save_schedule_with_slices(sched):
    c, svc, _ = sched
    data = form(
        "Christmas", slice_tags_0="ai-variant:christmas", slice_share_0="40",
        slice_tags_1="source:nga", slice_share_1="25", slice_tags_2="", slice_share_2="",
    )  # fmt: skip
    data.update(start_month="12", start_day="1", end_month="12", end_day="31")
    assert c.post("/schedule/schedules/x", data=data).status_code == 200
    x = next(s for s in svc.settings().schedules if s.id == "x")
    assert x.slices == [Slice(["ai-variant:christmas"], 40), Slice(["source:nga"], 25)]
    # The same dates may come as one MM-DD each.
    r = c.post(
        "/schedule/schedules",
        data={"name": "Pi day", "start": "03-14", "end": "03-14"},
    )
    assert r.status_code == 200
    assert any(
        s.name == "Pi day" and s.start == "03-14" for s in svc.settings().schedules
    )


def test_bad_slice_tag_is_rejected(sched):
    c, svc, _ = sched
    before = svc.settings().schedules
    data = form("Spring", slice_tags_0="painting", slice_share_0="10")
    r = c.post("/schedule/schedules", data=data)
    assert r.status_code == 400 and "group:value" in r.text
    assert 'value="painting"' in r.text  # what was typed comes back
    r = c.post(
        "/schedule/schedules/e",
        data=form("Everyday", slice_tags_0="kind:x", slice_share_0="lots"),
    )
    assert (
        r.status_code == 400 and "whole number" in r.text and 'value="lots"' in r.text
    )
    r = c.post(
        "/schedule/schedules/e",
        data=form("Everyday", slice_tags_0="kind:x", slice_share_0="101"),
    )
    assert r.status_code == 400
    r = c.post(
        "/schedule/schedules/e",
        data=form(
            "Everyday",
            slice_tags_0="kind:x",
            slice_share_0="70",
            slice_tags_1="kind:y",
            slice_share_1="40",
        ),
    )
    assert r.status_code == 400 and "add up" in r.text
    r = c.post(
        "/schedule/schedules/e",
        data=form(" ", slice_tags_0="kind:x", slice_share_0="5"),
    )
    assert r.status_code == 400 and "needs a name" in r.text
    assert svc.settings().schedules == before


def test_version_styles_lists_types_and_cost(sched):
    c, svc, v = sched
    cat = svc.catalogue()
    wants_all(svc)
    old = cat.add_variant("nga:1", "christmas", 2, "p", "m", 0.30, "/nowhere2.jpg")
    cat.set_library([f"variant:{old.id}"], "removed")  # not counted as a version
    page = c.get("/studio/versions").text
    assert "Christmas" in page and "make it festive" in page
    assert "$0.42" in page  # cost counts every version, removed or not
    assert '<td class="versions num">1</td>' in page  # the removed one is not counted
    assert "Make 1 now" in page  # nga:2 has no version and Christmas wants both
    assert 'name="variant_limit"' in page


def test_make_now_confirms_then_submits(sched):
    c, svc, v = sched
    wants_all(svc)
    url = "/studio/versions/christmas/make-now"
    page = c.post(url).text
    assert "Make Christmas versions?" in page and "$0.12" in page
    assert svc.queued() == []
    r = c.post(url, data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs"
    [job] = svc.queued()
    assert (job.kind, job.args) == ("make-now", {"type": "christmas"})
    assert c.post("/studio/versions/nope/make-now").status_code == 404


def test_variant_type_add_and_edit(sched):
    c, svc, _ = sched
    r = c.post("/studio/versions/types", data={"name": "Christmas", "prompt": "boo"})
    assert "already a type" in r.text
    r = c.post("/studio/versions/types", data={"name": "", "prompt": "boo"})
    assert r.status_code == 400 and "give the type a name" in r.text
    r = c.post("/studio/versions/types", data={"name": "Halloween", "prompt": "spooky"})
    assert r.status_code == 200 and "Added Halloween" in r.text
    assert [t.slug for t in svc.catalogue().variant_types()] == [
        "christmas",
        "halloween",
    ]
    c.post("/studio/versions/types/halloween", data={"name": "Boo", "prompt": "more"})
    t = svc.catalogue().variant_types()[1]
    assert (t.name, t.prompt) == ("Boo", "more")
    r = c.post("/studio/versions/types/halloween", data={"name": "", "prompt": "x"})
    assert r.status_code == 400
    r = c.post("/studio/versions/types/nope", data={"name": "a", "prompt": "b"})
    assert r.status_code == 404


def test_variant_limit_saves_and_rejects(sched):
    c, svc, _ = sched
    r = c.post("/studio/versions/limit", data={"variant_limit": "5"})
    assert r.status_code == 200 and "Saved" in r.text
    assert svc.settings().variant_limit == 5
    r = c.post("/studio/versions/limit", data={"variant_limit": "x"})
    assert r.status_code == 400 and "whole number" in r.text
    r = c.post("/studio/versions/limit", data={"variant_limit": "99"})
    assert r.status_code == 400 and "0-50" in r.text
    assert svc.settings().variant_limit == 5
    assert svc.settings().schedules  # the schedules came through untouched


def test_old_variants_page_goes_to_schedule(sched):
    c, *_ = sched
    r = c.get("/variants", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/schedule"
    assert c.post("/variants/limit").status_code in (404, 405)
