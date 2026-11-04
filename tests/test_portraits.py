import json

import numpy as np
import pytest
from PIL import Image

from frame_art import imaging, pipeline, portraits, publish, studio, variants
from frame_art.catalogue import Catalogue
from frame_art.model import Work

HOPPER = studio.Painter("hopper", "Edward Hopper", "Flat planes.", ("portrait",))


def test_target():
    assert portraits.target(20, 150, 100) == 38
    assert portraits.target(20, 150, 10) == 10
    assert portraits.target(0, 150, 10) == 0
    assert portraits.target(100, 150, 7) == 7
    assert portraits.target(20, 150, 0) == 0


def test_keys():
    assert portraits.key_for(7) == "portrait:7"
    assert portraits.is_portrait("portrait:7") and not portraits.is_portrait("met:7")


def make_work(i: int) -> Work:
    return Work(
        source=f"s{i % 3}", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=4000, height=2250,
    )  # fmt: skip


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.upsert([make_work(i) for i in range(10)])

    def fake_render(http, cand, pad_style="blur"):
        rng = np.random.default_rng(int(cand.work.source_id))
        im = Image.fromarray(rng.integers(0, 255, (36, 64, 3), dtype=np.uint8))
        return pipeline.Output(cand.work, cand.plan, imaging.phash(im), im)

    monkeypatch.setattr(pipeline, "render", fake_render)
    st = studio.StudioStore(tmp_path / "db.sqlite", tmp_path / "studio")
    dest = tmp_path / "frame_upload"
    pub = publish.Publisher(cat, None, dest, "blur")
    return cat, st, pub, dest


def saved(st, size=(320, 180), color="orange"):
    t = st.start_try("ph1", HOPPER, "keep", "fill", [], None)
    st.finish_try(t.id, Image.new("RGB", size, color), 0.12)
    return st.save(t.id)


def live_portraits(cat):
    return [p for p in cat.published(active=True, portraits=True)
            if portraits.is_portrait(p.key)]  # fmt: skip


def test_sync_publishes_up_to_want(setup):
    cat, st, pub, dest = setup
    for c in ("red", "green", "blue"):
        saved(st, color=c)
    out = portraits.sync(cat, st, pub, dest, want=2, seed=1)
    assert out["live"] == 2 and len(out["added"]) == 2 and out["retired"] == []
    names = sorted(p.filename for p in live_portraits(cat))
    assert all(n.startswith("portrait-") and n.endswith(".jpg") for n in names)
    assert sorted(f.name for f in (dest / "full").glob("portrait-*.jpg")) == names
    assert portraits.sync(cat, st, pub, dest, want=2, seed=1)["added"] == []


def test_a_photo_shaped_painting_is_padded_into_full(setup):
    # HA uploads only full/, so a painting in the photo's shape is padded to the
    # panel like a tall museum work, never left in matted/.
    cat, st, pub, dest = setup
    p = saved(st, size=(180, 240))
    out = portraits.sync(cat, st, pub, dest, want=1, seed=1)
    assert out["added"] == [f"full/portrait-{p.id}.jpg"]
    with Image.open(dest / "full" / f"portrait-{p.id}.jpg") as im:
        assert im.size == imaging.PANEL
    assert not (dest / "matted").exists()


def test_nightly_rotation_swaps_some_portraits(setup):
    cat, st, pub, dest = setup
    for c in ("red", "green", "blue", "white"):
        saved(st, color=c)
    portraits.sync(cat, st, pub, dest, want=2, seed=1)
    before = {p.key for p in live_portraits(cat)}
    # A hand sync (removing a painting, say) leaves the rest alone...
    assert portraits.sync(cat, st, pub, dest, want=2, seed=1)["retired"] == []
    # ...the nightly one rotates the longest-live out for one waiting.
    out = portraits.sync(cat, st, pub, dest, want=2, seed=2, rotate=True)
    after = {p.key for p in live_portraits(cat)}
    assert len(out["retired"]) == 1 and len(after) == 2 and after != before
    assert len(after - before) == 1


def test_rotation_with_nothing_waiting_keeps_everything(setup):
    cat, st, pub, dest = setup
    saved(st, color="red"), saved(st, color="blue")
    portraits.sync(cat, st, pub, dest, want=2, seed=1)
    assert (
        portraits.sync(cat, st, pub, dest, want=2, seed=2, rotate=True)["retired"] == []
    )


def test_shrinking_want_retires_the_longest_live(setup):
    cat, st, pub, dest = setup
    a, b = saved(st, color="red"), saved(st, color="blue")
    portraits.sync(cat, st, pub, dest, want=2, seed=1)
    cat.db.execute("update published set published_at = 1 where key = ?",
                   (portraits.key_for(b.id),))  # fmt: skip
    cat.db.commit()
    out = portraits.sync(cat, st, pub, dest, want=1, seed=1)
    assert out["retired"] == [f"full/portrait-{b.id}.jpg"]
    assert [p.key for p in live_portraits(cat)] == [portraits.key_for(a.id)]


def test_removed_painting_is_retired_and_its_tv_id_deleted(setup):
    cat, st, pub, dest = setup
    p = saved(st)
    portraits.sync(cat, st, pub, dest, want=1, seed=1)
    name = f"portrait-{p.id}.jpg"
    (dest / "full" / publish.SIDECAR).write_text(
        json.dumps({name: {"content_id": "MY_F0099", "modified": 1.0}})
    )
    st.remove_painting(p.id)
    out = portraits.sync(cat, st, pub, dest, want=1, seed=1)
    assert out["retired"] == [f"full/{name}"] and out["live"] == 0
    assert not (dest / "full" / name).exists()
    assert "MY_F0099" in publish.pending(cat, dest)["retire"]


def test_least_recently_shown_goes_up_first(setup):
    cat, st, pub, dest = setup
    a, b = saved(st, color="red"), saved(st, color="blue")
    portraits.sync(cat, st, pub, dest, want=1, seed=1)
    first = live_portraits(cat)[0].key
    portraits.sync(cat, st, pub, dest, want=0, seed=1)
    portraits.sync(cat, st, pub, dest, want=1, seed=1)
    other = {portraits.key_for(a.id), portraits.key_for(b.id)} - {first}
    assert [p.key for p in live_portraits(cat)] == list(other)


def test_museum_pool_ignores_portraits(setup):
    cat, st, pub, dest = setup
    pub.run(count=4, pool=4, season=None, seed=1)
    saved(st, color="red"), saved(st, color="blue")
    portraits.sync(cat, st, pub, dest, want=2, seed=1)
    res = pub.run(count=0, pool=4, season=None, seed=2)
    assert res.retired == []
    assert len(cat.published(active=True)) == 4
    assert len(cat.published(active=True, portraits=True)) == 6
    assert len(publish.pending(cat, dest)["to_upload"]) == 6


def test_portraits_stay_out_of_the_index_and_versions(setup):
    cat, st, pub, dest = setup
    pub.run(count=2, pool=4, season=None, seed=1)
    p = saved(st)
    portraits.sync(cat, st, pub, dest, want=1, seed=1)
    pub._write_meta()
    index = json.loads((dest / "_meta" / "index.json").read_text())
    assert len(index) == 2 and not any("portrait" in k for k in index)
    out = variants.apply_versions(cat, None, dest.parent / "data", pub)
    assert out == {"swapped": [], "failed": []}
    assert (dest / "full" / f"portrait-{p.id}.jpg").is_file()


def test_no_saved_paintings_publishes_nothing(setup):
    cat, st, pub, dest = setup
    want = portraits.target(20, 150, len(st.paintings()))
    assert portraits.sync(cat, st, pub, dest, want, seed=1) == {
        "live": 0, "added": [], "retired": []}  # fmt: skip


def test_sync_portraits_uses_todays_share(setup):
    from datetime import date

    from frame_art import jobs
    from frame_art.schedules import everyday
    from frame_art.settings import Settings

    cat, st, pub, dest = setup
    pub.run(count=2, pool=4, season=None, seed=1)
    for c in ("red", "green", "blue"):
        saved(st, color=c)
    s = Settings(schedules=[everyday({"portraits": 50})])
    out = jobs.sync_portraits(cat, st, dest, s, date(2026, 11, 4))
    assert out["live"] == 2  # half the screen: 2 museum works, 2 portraits
    before = {p.key for p in live_portraits(cat)}
    out = jobs.sync_portraits(cat, st, dest, s, date(2026, 11, 5), rotate=True)
    assert len(out["retired"]) == 1 and {p.key for p in live_portraits(cat)} != before
    s = Settings(schedules=[])
    assert jobs.sync_portraits(cat, st, dest, s, date(2026, 11, 4))["live"] == 0


def test_rotation_does_not_count_paintings_retired_for_excess(setup):
    cat, st, pub, dest = setup
    for c in ("red", "green", "blue"):
        saved(st, color=c)
    portraits.sync(cat, st, pub, dest, want=3, seed=1)
    out = portraits.sync(cat, st, pub, dest, want=2, seed=2, rotate=True)
    assert len(out["retired"]) == 1 and out["added"] == [] and out["live"] == 2
