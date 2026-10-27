import json

import numpy as np
import pytest
from PIL import Image

from frame_art import imaging, pipeline, publish
from frame_art.catalogue import Catalogue
from frame_art.model import Work


def make_work(i: int, width=4000, height=2250) -> Work:
    return Work(
        source=f"s{i % 3}", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=width, height=height,
    )  # fmt: skip


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    works = [make_work(i) for i in range(10)] + [
        make_work(i, 2000, 3000) for i in range(10, 14)
    ]
    cat.upsert(works)

    def fake_render(http, cand, pad_style="blur"):
        # Distinct noise per work so perceptual dedupe sees different pictures.
        rng = np.random.default_rng(int(cand.work.source_id))
        im = Image.fromarray(rng.integers(0, 255, (36, 64, 3), dtype=np.uint8))
        return pipeline.Output(cand.work, cand.plan, imaging.phash(im), im)

    monkeypatch.setattr(pipeline, "render", fake_render)
    dest = tmp_path / "frame_upload"
    return cat, dest


def test_publish_adds_count_and_writes_index(setup):
    cat, dest = setup
    res = publish.Publisher(cat, None, dest, "blur").run(
        count=4, pool=50, season=None, seed=1
    )
    assert len(res.added) == 4
    assert len(list((dest / "full").glob("*.jpg"))) == 4
    index = json.loads((dest / "_meta" / "index.json").read_text())
    assert set(index) == set(res.added)
    assert not list((dest / ".staging").glob("*"))  # staging left empty


def test_second_run_publishes_different_works(setup):
    cat, dest = setup
    first = publish.Publisher(cat, None, dest, "blur").run(4, 50, None, seed=1).added
    second = publish.Publisher(cat, None, dest, "blur").run(4, 50, None, seed=2).added
    assert not set(first) & set(second)


def test_retire_oldest_reads_sidecar_and_lists_content_ids(setup):
    cat, dest = setup
    first = publish.Publisher(cat, None, dest, "blur").run(3, 50, None, seed=1).added
    # Pretend HA uploaded those three.
    sidecar = {f.split("/")[1]: {"content_id": f"MY_F{i}", "modified": 1.0}
               for i, f in enumerate(first)}  # fmt: skip
    (dest / "full" / publish.SIDECAR).write_text(json.dumps(sidecar))
    sidecar_before = (dest / "full" / publish.SIDECAR).read_text()

    res = publish.Publisher(cat, None, dest, "blur").run(3, pool=4, season=None, seed=2)
    assert len(res.retired) == 2
    assert set(res.retired) <= set(first)
    for f in res.retired:
        assert not (dest / f).exists()
    todo = publish.pending(cat, dest)
    assert sorted(todo["retire"]) == sorted(sidecar[f.split("/")[1]]["content_id"]
                                            for f in res.retired)  # fmt: skip
    # Three live works were never uploaded (not in the sidecar) ...
    assert len(todo["to_upload"]) == 3
    # ... and once HA confirms a delete, it drops off the list.
    cat.record_deleted(todo["retire"][0])
    assert len(publish.pending(cat, dest)["retire"]) == 1
    assert (
        dest / "full" / publish.SIDECAR
    ).read_text() == sidecar_before  # never written
    assert len(cat.published(active=True)) == 4


def test_matte_style_puts_padded_works_in_matted(setup):
    cat, dest = setup
    res = publish.Publisher(cat, None, dest, "matte").run(14, 50, None, seed=1)
    folders = {f.split("/")[0] for f in res.added}
    assert folders == {"full", "matted"}
    for f in res.added:
        key = f.split("/")[1]
        portrait = any(f"-{i}-" in key for i in range(10, 14))
        assert f.startswith("matted/") == portrait


def test_rejected_work_is_recorded_and_not_retried(setup, monkeypatch):
    cat, dest = setup
    real = pipeline.render

    def render(http, cand, pad_style="blur"):
        if cand.work.source_id == "0":
            raise pipeline.NotRectangular("round")
        return real(http, cand, pad_style)

    monkeypatch.setattr(pipeline, "render", render)
    res = publish.Publisher(cat, None, dest, "blur").run(14, 50, None, seed=1)
    assert len(res.added) == 13 and not res.failed
    assert cat.rejected_keys() == {"s0:0"}


def test_diversify_interleaves_sources():
    cands = [
        pipeline.Candidate(make_work(i), imaging.plan_for(4000, 2250), score=10 - i)
        for i in range(9)
    ]
    order = [c.work.source for c in publish.diversify(cands, seed=0)]
    assert len(set(order[:3])) == 3


def test_ban_retires_live_work_and_keeps_it_out(setup):
    cat, dest = setup
    added = publish.Publisher(cat, None, dest, "blur").run(3, 50, None, seed=1).added
    key = next(p.key for p in cat.published(active=True) if p.filename in added[0])
    cat.set_override(key, "banned")
    retired = publish.Publisher(cat, None, dest, "blur").sync()
    assert retired == [added[0]]
    res = publish.Publisher(cat, None, dest, "blur").run(20, 50, None, seed=2)
    assert key not in {p.key for p in cat.published(active=True)}
    assert len(res.added) == 11  # 14 works less the 3 published earlier (1 banned)


def test_pinned_work_publishes_first_and_is_never_retired(setup):
    cat, dest = setup
    cat.set_override("s1:13", "pinned")
    res = publish.Publisher(cat, None, dest, "blur").run(1, 50, None, seed=1)
    assert "-13-" in res.added[0]
    publish.Publisher(cat, None, dest, "blur").run(5, pool=2, season=None, seed=2)
    live = {p.key for p in cat.published(active=True)}
    assert "s1:13" in live and len(live) == 2


def _photos(cat: Catalogue, n: int) -> None:
    cat.upsert(
        [
            Work(
                source="commons", source_id=str(100 + i), title=f"Photo {i}",
                artist="P", date="2020", kind="photograph", license="CC BY-SA 4.0",
                image_url=f"http://x/p{i}.jpg", width=6000, height=4000,
            )
            for i in range(n)
        ]
    )  # fmt: skip


def _kinds(cat: Catalogue) -> list[str]:
    works = {w.key: w for w in cat.works()}
    return [works[p.key].kind for p in cat.published(active=True)]


def test_style_mix_splits_new_works(setup):
    cat, dest = setup
    _photos(cat, 10)
    medium = pipeline.Prefs(imaging.CROP_LEVELS["medium"])
    publish.Publisher(cat, None, dest, "blur", photo_share=40, photo_prefs=medium).run(
        5, 50, None, seed=1
    )
    assert sorted(_kinds(cat)) == ["painting"] * 3 + ["photograph"] * 2


def test_style_mix_fills_photo_shortfall_with_art(setup):
    cat, dest = setup
    _photos(cat, 1)
    medium = pipeline.Prefs(imaging.CROP_LEVELS["medium"])
    publish.Publisher(cat, None, dest, "blur", photo_share=100, photo_prefs=medium).run(
        4, 50, None, seed=1
    )
    assert sorted(_kinds(cat)) == ["painting"] * 3 + ["photograph"]


def test_no_photos_without_a_share(setup):
    cat, dest = setup
    _photos(cat, 10)
    publish.Publisher(cat, None, dest, "blur").run(5, 50, None, seed=1)
    assert set(_kinds(cat)) == {"painting"}


def _live(cat: Catalogue) -> set[str]:
    return {p.key for p in cat.published(active=True)}


def test_collection_goes_first_then_automatic(setup):
    cat, dest = setup
    cat.collect("s1:4")
    cat.collect("s2:8")
    pub = publish.Publisher(cat, None, dest, "blur", choose_from="collection_first")
    pub.run(4, 50, None, seed=1)
    live = _live(cat)
    assert {"s1:4", "s2:8"} <= live and len(live) == 4


def test_collection_only_adds_nothing_else(setup):
    cat, dest = setup
    cat.collect("s1:4")
    pub = publish.Publisher(cat, None, dest, "blur", choose_from="collection")
    pub.run(4, 50, None, seed=1)
    assert _live(cat) == {"s1:4"}
    pub.run(4, 50, None, seed=2)  # already live: nothing left to add
    assert len(cat.published()) == 1


def test_collection_only_lets_photos_fill_in_for_missing_art(setup):
    cat, dest = setup
    _photos(cat, 3)
    for i in range(3):
        cat.collect(f"commons:{100 + i}")
    cat.collect("s1:4")
    medium = pipeline.Prefs(imaging.CROP_LEVELS["medium"])
    publish.Publisher(
        cat, None, dest, "blur", photo_share=25, photo_prefs=medium,
        choose_from="collection",
    ).run(4, 50, None, seed=1)  # fmt: skip
    assert sorted(_kinds(cat)) == ["painting"] + ["photograph"] * 3


def test_collection_works_come_back_sooner(setup, monkeypatch):
    cat, dest = setup
    cat.collect("s1:4")
    pub = publish.Publisher(
        cat, None, dest, "blur", choose_from="collection", repeat_days=30
    )
    pub.run(1, 1, None, seed=1)
    cat.set_override("s1:4", "banned")
    pub.sync()  # retired now
    cat.set_override("s1:4", None)
    cat.collect("s1:4")
    pub.run(1, 1, None, seed=2)
    assert _live(cat) == set()  # still cooling down
    later = publish.time.time() + 31 * publish.DAY
    monkeypatch.setattr(publish.time, "time", lambda: later)
    pub.run(1, 1, None, seed=3)
    assert _live(cat) == {"s1:4"}


def test_ban_and_collect_exclude_each_other(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.upsert([make_work(1)])
    cat.collect("s1:1")
    cat.set_override("s1:1", "banned")
    assert cat.collection() == {}
    cat.collect("s1:1")
    assert "s1:1" in cat.collection() and cat.overrides() == {}


def test_metadata_kept_out_of_the_works_row(tmp_path):
    """0.2.0 builds Work(**data) from works.data, so new fields must live elsewhere."""
    cat = Catalogue(tmp_path / "db.sqlite")
    w = make_work(1)
    w.subjects, w.style, w.lat = ["Haystacks"], "Impressionism", 48.1
    cat.upsert([w])
    (data,) = cat.db.execute("select data from works").fetchone()
    assert not set(json.loads(data)) & {"subjects", "style", "lat"}
    back = cat.works()[0]
    assert (back.subjects, back.style, back.lat) == (
        ["Haystacks"],
        "Impressionism",
        48.1,
    )


def test_content_index_knows_recorded_and_sidecar_ids(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    dest = tmp_path / "out"
    cat.record_publish("a.jpg", "full", "s:1", 0)
    cat.record_publish("b.jpg", "matted", "s:2", 0)
    cat.record_retire("a.jpg", "MY_F0001")  # the id is recorded on retiring
    (dest / "matted").mkdir(parents=True)
    sidecar = {"b.jpg": {"content_id": "MY_F0002", "modified": 1.0}}
    (dest / "matted" / publish.SIDECAR).write_text(json.dumps(sidecar))
    index = publish.content_index(cat, dest)
    assert {cid: p.filename for cid, p in index.items()} == {
        "MY_F0001": "a.jpg",
        "MY_F0002": "b.jpg",
    }
    assert publish.find_content(cat, dest, "MY_F0002").filename == "b.jpg"
    assert publish.find_content(cat, dest, "MY_F0003") is None
