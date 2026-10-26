import pytest

from frame_art import classify, pipeline
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
    assert (
        pipeline.slug("Skovinteriør med kroget træ") == "skovinterior-med-kroget-trae"
    )


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


def test_full_screen_only_drops_padded_works():
    works = [work(source_id="1", width=4000, height=2300), work(source_id="2", title="P",
             width=3000, height=4000)]  # fmt: skip
    cands, rep = pipeline.filter_works(
        works, None, pipeline.Prefs(full_screen_only=True)
    )
    assert [c.work.source_id for c in cands] == ["1"]
    assert rep.rejected["not full screen at this crop level"] == 1


def test_nudes_are_filtered_unless_allowed():
    works = [
        work(source_id="1", title="Venus and Adonis"),
        work(source_id="2", title="Woman with a Parrot", tags=["nude"]),
        work(source_id="3", title="Liggende nøgen kvinde"),
        work(source_id="4", title="The Harvesters"),
    ]
    cands, rep = pipeline.filter_works(works, None)
    assert [c.work.source_id for c in cands] == ["4"]
    assert rep.rejected["nude"] == 3
    cands, _ = pipeline.filter_works(works, None, pipeline.Prefs(allow_nudes=True))
    assert len(cands) == 4


def test_nude_title_needs_whole_words():
    assert not classify.nude_title("Venusberg Road")  # not the goddess
    assert not classify.nude_title("Nuns at prayer")
    assert classify.nude_title("Akt im Atelier")


def test_commons_thumb_from_original_and_thumb():
    from frame_art.pipeline import commons_thumb

    orig = "https://upload.wikimedia.org/wikipedia/commons/9/96/A_b.jpg?utm_source=x"
    want = "https://upload.wikimedia.org/wikipedia/commons/thumb/9/96/A_b.jpg/500px-A_b.jpg"
    assert commons_thumb(orig, 500) == want
    thumb = "https://thumb.wikimedia.org/wikipedia/commons/thumb/9/96/A_b.jpg/3840px-A_b.jpg"
    assert commons_thumb(thumb, 500) == want


def test_commons_artist_and_date_cleanup():
    from frame_art.sources.commons import _artist, _date

    def meta(**kv):
        return {k: {"value": v} for k, v in kv.items()}

    long = ("This Photo was taken by Wolfgang Moroder. Feel free to use my photos, "
            "but please mention me as the author.")  # fmt: skip
    assert _artist(meta(Artist=long)) == "Wolfgang Moroder"
    assert _artist(meta(Artist='<a href="x">Steffen Schmitz</a> (more photos)')) == (
        "Steffen Schmitz"
    )
    assert _date(meta(DateTimeOriginal="Taken on 16 August 2014")) == "2014"
    assert _date(meta(DateTimeOriginal="2015-12-20 11:25:28")) == "2015-12-20"
    assert _date(meta(DateTimeOriginal="Taken on 1")) is None


def test_commons_hidden_labels_and_artworks():
    from frame_art.sources.commons import _date, _is_artwork, _text

    def meta(**kv):
        return {k: {"value": v} for k, v in kv.items()}

    name = (
        '<div class="fn"><i>The Siege of Osaka Castle</i>'
        '<div style="display: none;">label QS:Lja,"大坂"\nlabel QS:Len,"x"</div></div>'
    )
    assert _text(meta(ObjectName=name), "ObjectName") == "The Siege of Osaka Castle"
    date = '17<sup>th</sup> century<div style="display: none;">date QS:P571,+1650</div>'
    assert _date(meta(DateTimeOriginal=date)) is None
    assert _is_artwork(
        meta(Categories="Osaka|Featured pictures of paintings from Japan")
    )
    assert _is_artwork(meta(Categories="PD-Art (PD-old-100)"))
    assert not _is_artwork(meta(Categories="Featured pictures of Japan|Lakes"))


def test_loc_image_urls():
    from frame_art.sources.loc import iiif_base, master_tiff, rid_from

    svc = "https://tile.loc.gov/storage-services/service/pnp/highsm/55000/55058v.jpg#h=683&w=1024"
    assert master_tiff(["x", svc]) == (
        "https://tile.loc.gov/storage-services/master/pnp/highsm/55000/55058u.tif"
    )
    iiif = "https://tile.loc.gov/image-services/iiif/service:pnp:highsm:16300:16378/full/pct:25/0/default.jpg"
    assert iiif_base([iiif]) == (
        "https://tile.loc.gov/image-services/iiif/service:pnp:highsm:16300:16378"
    )
    assert rid_from("https://www.loc.gov/item/2018703046/") == "2018703046"
