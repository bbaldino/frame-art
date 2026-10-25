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
    retire = json.loads((dest / "_meta" / "retire.json").read_text())
    assert sorted(retire["content_ids"]) == sorted(sidecar[f.split("/")[1]]["content_id"]
                                                   for f in res.retired)  # fmt: skip
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
