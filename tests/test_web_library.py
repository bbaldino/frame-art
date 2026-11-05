import datetime
import io
import re
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.service import Service
from frame_art.studio import Painter
from frame_art.web import create_app

HOPPER = Painter("hopper", "Edward Hopper", "Flat planes.", ("scene",))
DAY = 86400


def work(source, sid, title, kind="painting", **kw) -> Work:
    return Work(
        source=source, source_id=sid, title=title, artist="Someone", date="1900",
        kind=kind, license="CC0", iiif=f"https://example.org/iiif/{sid}",
        width=4000, height=2250, **kw,
    )  # fmt: skip


def jpeg(color="red", size=(64, 36)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def lib(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "out"
    cat = Catalogue(data / "frame-art.db")
    cat.upsert(
        [
            work("nga", "1", "Haystacks", subjects=["field"]),
            work("nga", "2", "Adoration", subjects=["christmas"]),
            work("met", "3", "Bridge", kind="photograph"),
        ]
    )
    svc = Service(data, dest, "/config/frame_art_pool", None, 1883, "test")
    photo = svc.photos().add("upload", "a", jpeg(), "IMG_0001.jpg")
    svc.photos().set_kind(photo.id, "scene", "test", False)
    st = svc.studio()
    t = st.start_try(photo.id, HOPPER, "keep", "fill", [], None)
    st.finish_try(t.id, Image.new("RGB", (320, 180), "orange"), 0.1)
    painting = st.save(t.id)
    cat.add_variant_type("christmas", "Christmas", "make it festive")
    vpath = data / "variants" / "christmas" / "nga-1" / "1.jpg"
    vpath.parent.mkdir(parents=True)
    vpath.write_bytes(jpeg("green"))
    v = cat.add_variant("nga:1", "christmas", 1, "p", "m", 0.12, str(vpath))
    return TestClient(create_app(svc)), svc, photo, painting, v


def card(key: str) -> str:
    return 'id="w-' + re.sub(r"[^A-Za-z0-9_-]", "-", key) + '"'


def cards(text: str) -> list[str]:
    return re.findall(r'<article class="card[^"]*" id="(w-[^"]+)"', text)


def test_library_lists_only_library_by_default(lib):
    c, svc, *_ = lib
    svc.catalogue().set_library(["nga:1"], "in")
    page = c.get("/library").text
    assert card("nga:1") in page
    assert card("nga:2") not in page and card("met:3") not in page


def test_all_works_and_views(lib):
    c, svc, photo, painting, v = lib
    cat = svc.catalogue()
    cat.set_library(["nga:1"], "in")
    cat.set_library(["nga:2"], "removed")
    cat.collect("met:3")
    every = cards(c.get("/library?scope=all").text)
    assert set(every) == {
        "w-nga-1", "w-nga-2", "w-met-3", f"w-photo-{photo.id}", f"w-portrait-{painting.id}",
    }  # fmt: skip
    with_versions = cards(c.get("/library?scope=all&versions=1").text)
    assert f"w-variant-{v.id}" in with_versions
    out = cards(c.get("/library?scope=all&view=out").text)
    assert "w-nga-1" not in out and "w-nga-2" not in out and "w-met-3" in out
    assert cards(c.get("/library?scope=all&view=removed").text) == ["w-nga-2"]
    assert cards(c.get("/library?scope=all&view=favorites").text) == ["w-met-3"]
    # The photo's own name shows on its card (a LAN page).
    assert "IMG_0001.jpg" in c.get("/library?scope=all&source=photos").text


def test_tag_filter_is_group_qualified(lib):
    c, _, _, _, v = lib
    plain = cards(c.get("/library?scope=all&versions=1&tag=subject:christmas").text)
    assert plain == ["w-nga-2"]
    ai = cards(c.get("/library?scope=all&versions=1&tag=ai-variant:christmas").text)
    assert ai == [f"w-variant-{v.id}"]


def test_sort_newest_and_added_since(lib):
    c, svc, *_ = lib
    cat = svc.catalogue()
    now = datetime.datetime.now().timestamp()
    for key, days in (("nga:1", 30), ("nga:2", 2), ("met:3", 10)):
        cat.db.execute(
            "update work_added set added_at = ? where key = ?", (now - days * DAY, key)
        )
    cat.db.commit()
    q = "/library?scope=all&source={}"
    newest = [
        x
        for x in cards(c.get("/library?scope=all").text)
        if "w-nga" in x or "w-met" in x
    ]
    assert newest == ["w-nga-2", "w-met-3", "w-nga-1"]
    by_title = cards(c.get("/library?scope=all&sort=title&source=nga").text)
    assert by_title == ["w-nga-2", "w-nga-1"]
    since = (datetime.date.today() - datetime.timedelta(days=15)).isoformat()
    recent = cards(c.get(q.format("nga") + f"&added_since={since}").text)
    assert recent == ["w-nga-2"]
    # In the Library, the date that counts is when it joined.
    cat.set_library(["nga:1"], "in")
    page = c.get("/library").text
    assert "in Library since" in page


def test_add_and_remove_and_put_back(lib):
    c, svc, _, _, v = lib
    r = c.post(
        "/library/add?next=/library%3Fscope%3Dall",
        data={"key": "nga:1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/library?scope=all"
    lib_state = svc.catalogue().library
    assert lib_state()["nga:1"][0] == "in"
    c.post("/library/remove", data={"key": "nga:1"})
    assert lib_state()["nga:1"][0] == "removed"
    assert lib_state()[f"variant:{v.id}"][0] == "removed"  # its versions too
    c.post("/library/put-back", data={"key": "nga:1"})
    assert lib_state()["nga:1"][0] == "in"
    assert lib_state()[f"variant:{v.id}"][0] == "removed"  # versions stay out
    # Unticking "with its versions" leaves them alone.
    c.post("/library/put-back", data={"key": f"variant:{v.id}"})
    c.post("/library/remove", data={"key": "nga:1", "with_versions": ["0"]})
    assert f"variant:{v.id}" not in lib_state()


def test_remove_live_work_submits_sync(lib):
    c, svc, *_ = lib
    cat = svc.catalogue()
    cat.set_library(["nga:1", "nga:2"], "in")
    cat.record_publish("haystacks.jpg", "full", "nga:1", 0)
    c.post("/library/remove", data={"key": "nga:2"})
    assert not [j for j in svc.queued() if j.kind == "sync"]
    c.post("/library/remove", data={"key": "nga:1"})
    assert [j.kind for j in svc.queued()] == ["sync"]


def test_bulk_add_all_on_filtered_view(lib):
    c, svc, *_ = lib
    r = c.post(
        "/library/add?scope=all&source=nga", data={"all": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert set(svc.catalogue().library()) == {"nga:1", "nga:2"}


def test_bulk_all_on_all_works_needs_a_filter(lib):
    c, svc, *_ = lib
    r = c.post("/library/add?scope=all", data={"all": "1"}, follow_redirects=False)
    assert r.status_code == 400 and "filter" in r.text
    assert svc.catalogue().library() == {}
    # Within the Library, "all" is already a choice of works.
    svc.catalogue().set_library(["nga:1"], "in")
    r = c.post("/library/remove", data={"all": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.catalogue().library()["nga:1"][0] == "removed"


def test_star_does_not_add(lib):
    c, svc, *_ = lib
    c.post("/library/star", data={"key": "nga:2", "on": "1"})
    cat = svc.catalogue()
    assert "nga:2" in cat.collection() and "nga:2" not in cat.library()
    c.post("/library/star", data={"key": "nga:2", "on": "0"})
    assert "nga:2" not in cat.collection()


def test_tag_and_blank_tag(lib):
    c, svc, photo, *_ = lib
    key = f"photo:{photo.id}"
    c.post("/library/tag", data={"key": key, "tag": "Christmas", "on": "1"})
    assert svc.catalogue().user_tags() == {key: {"christmas"}}
    assert card(key) in c.get("/library?scope=all&tag=yours:christmas").text
    r = c.post("/library/tag", data={"key": key, "tag": "  ", "on": "1"})
    assert r.status_code == 400
    assert svc.catalogue().user_tags() == {key: {"christmas"}}
    c.post("/library/tag", data={"key": key, "tag": "christmas", "on": "0"})
    assert svc.catalogue().user_tags() == {}


def test_make_versions_confirms_cost_first(lib):
    c, svc, *_ = lib
    url = "/library/make-versions?scope=all&source=nga"
    r = c.post(url, data={"all": "1", "type": "christmas"})
    assert r.status_code == 200
    assert "2 works" in r.text and "Christmas" in r.text and "$0.24" in r.text
    assert 'name="confirm" value="1"' in r.text
    assert svc.queued() == []
    r = c.post(
        url,
        data={"all": "1", "type": "christmas", "confirm": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    (job,) = svc.queued()
    assert job.kind == "generate"
    assert job.args == {"keys": ["nga:1", "nga:2"], "type": "christmas"}
    assert c.post(url, data={"all": "1", "type": "nope"}).status_code == 404


def test_tags_json_groups(lib):
    c, *_ = lib
    got = c.get("/library/tags.json?q=christ").json()
    assert {"tag": "subject:christmas", "label": "christmas",
            "group": "Subject and style", "count": 1} in got  # fmt: skip
    assert {"tag": "ai-variant:christmas", "label": "ai-variant-christmas",
            "group": "AI variant", "count": 1} in got  # fmt: skip
    kinds = c.get("/library/tags.json?q=photo").json()
    assert {"tag": "kind:photograph", "label": "photograph",
            "group": "Kind", "count": 1} in kinds  # fmt: skip
    # Tags already in the filter, and tags no match carries, aren't offered.
    narrowed = c.get("/library/tags.json?tag=kind:painting").json()
    tags = {x["tag"] for x in narrowed}
    assert "kind:painting" not in tags and "kind:photograph" not in tags
    assert "subject:christmas" in tags


def test_version_actions(lib):
    c, svc, _, _, v = lib
    key = f"variant:{v.id}"
    c.post(f"/versions/{v.id}/remove")
    assert svc.catalogue().library()[key][0] == "removed"
    c.post(f"/versions/{v.id}/put-back")
    assert key not in svc.catalogue().library()
    c.post(f"/versions/{v.id}/regenerate")
    assert [(j.kind, j.args) for j in svc.queued()] == [
        ("regenerate", {"variant": v.id})
    ]
    assert c.post("/versions/999/remove").status_code == 404


def test_card_warns_but_adds(lib):
    c, svc, *_ = lib
    cat = svc.catalogue()
    cat.record_reject("nga:2", "too small")
    page = c.get("/library?scope=all&kind=painting").text
    assert "too small" in page
    c.post("/library/add", data={"key": "nga:2"})
    assert cat.library()["nga:2"][0] == "in"


def test_header_says_when_library_is_smaller_than_pool(lib):
    c, svc, *_ = lib
    svc.catalogue().set_library(["nga:1"], "in")
    assert "smaller than the" in c.get("/library").text


def test_work_page_for_photo_portrait_version(lib):
    c, svc, photo, painting, v = lib
    page = c.get(f"/works/photo:{photo.id}")
    assert page.status_code == 200 and "IMG_0001.jpg" in page.text
    assert "kind:scene" in page.text and "Not in the Library" in page.text
    c.post("/library/tag", data={"key": f"photo:{photo.id}", "tag": "family"})
    assert "yours:family" in c.get(f"/works/photo:{photo.id}").text
    assert c.get("/works/nga:1").status_code == 200

    page = c.get(f"/works/portrait:{painting.id}").text
    assert "Delete" in page and "Edward Hopper" in page
    page = c.get(f"/works/variant:{v.id}").text
    assert "Delete" in page and "Haystacks" in page and "/works/nga:1" in page

    # Deleting a version keeps its row, removes its file and takes it out.
    c.post(f"/works/variant:{v.id}/delete")
    cat = svc.catalogue()
    assert cat.variant(v.id) is not None and not Path(v.path).exists()
    assert cat.library()[f"variant:{v.id}"][0] == "removed"
    c.post(f"/works/portrait:{painting.id}/delete")
    assert svc.studio().get_painting(painting.id) is None
    assert c.get(f"/works/portrait:{painting.id}").status_code == 404
    assert c.post("/works/nga:1/delete").status_code == 400
