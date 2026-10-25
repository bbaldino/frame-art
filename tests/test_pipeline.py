import pytest

from frame_art import pipeline
from frame_art.classify import seasons_for
from frame_art.model import Work


def work(**kw) -> Work:
    base = {
        "source": "t", "source_id": "1", "title": "T", "artist": "A", "date": None, "kind": "painting",
        "license": "CC0", "image_url": "http://x/1.jpg", "width": 4000, "height": 2400,
    }  # fmt: skip
    base.update(kw)
    return Work(**base)


def test_seasons():
    assert seasons_for("Winter Landscape with Skaters") == ["winter"]
    assert seasons_for("The Waterfall") == []
    assert seasons_for("Orchard in Bloom, Spring") == ["spring"]
    assert seasons_for("May Belfort") == []
    assert seasons_for("Fra det gamle Christiansborg. Sent efterår") == ["autumn"]


def test_slug_transliterates():
    assert pipeline.slug("Vilhelm Hammershøi") == "vilhelm-hammershoi"
    assert pipeline.slug("Skovinteriør med kroget træ") == "skovinterior-med-kroget-trae"


def test_filter_dedupes_by_identity_keeping_larger():
    small = work(source="a", source_id="1", width=3200, height=1900)
    big = work(source="b", source_id="2", width=6000, height=3500)
    cands, rep = pipeline.filter_works([small, big], None)
    assert [c.work.source for c in cands] == ["b"]
    assert rep.rejected["duplicate (same artist+title or QID)"] == 1


def test_filter_rejects_small_and_extreme_shapes():
    tiny = work(source_id="1", title="a", width=1200, height=800)
    sliver = work(source_id="2", title="b", width=1000, height=4000)
    ok = work(source_id="3", title="c")
    cands, rep = pipeline.filter_works([tiny, sliver, ok], None)
    assert [c.work.source_id for c in cands] == ["3"]
    assert rep.passed == 1


def test_iiif_request_asks_only_for_needed_width():
    w = work(iiif="https://iiif/x", image_url=None, width=10000, height=6000)
    plan = pipeline.imaging.plan_for(w.width, w.height)
    assert plan.needed_width(w.width) < 4000
    url = pipeline.image_request(w, plan.needed_width(w.width))
    assert url == f"https://iiif/x/full/{plan.needed_width(w.width)},/0/default.jpg"


def test_filter_skips_tondi_by_title():
    cands, rep = pipeline.filter_works([work(title="The Alba Madonna (tondo)")], None)
    assert not cands
    assert rep.rejected["round (tondo)"] == 1


def test_render_rejects_round_work_on_card(monkeypatch):
    from io import BytesIO

    from test_imaging import _tondo

    buf = BytesIO()
    _tondo().save(buf, "JPEG")
    monkeypatch.setattr(pipeline, "fetch", lambda http, w, plan: buf.getvalue())
    w = work(width=1200, height=1200)
    cand = pipeline.Candidate(w, pipeline.imaging.plan_for(3000, 3000), 0)
    with pytest.raises(pipeline.NotRectangular):
        pipeline.render(None, cand)
