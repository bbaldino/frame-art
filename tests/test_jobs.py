import datetime
import time

from frame_art import jobs, keys, notability, variants
from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.publish import PublishResult
from frame_art.service import Service
from frame_art.settings import Settings


def make_work(i: int) -> Work:
    return Work(
        source="nga", source_id=str(i), title=f"Work {i}", artist="A", date="1900",
        kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=4000, height=2250,
    )  # fmt: skip


def test_nightly_makes_versions_before_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "read_key", lambda name: None)
    cat = Catalogue(tmp_path / "frame-art.db")
    cat.record_crawl("nga")  # not due for a recrawl
    calls = []

    def fake_variants(*a, **k):
        calls.append("variants")
        return {"generated": [], "gen_failed": [], "cost": 0.0}

    def fake_publish(*a, **k):
        calls.append("publish")
        return PublishResult(added=[], retired=[], failed=[])

    monkeypatch.setattr(variants, "nightly", fake_variants)
    monkeypatch.setattr(jobs, "publish", fake_publish)
    summary = jobs.nightly(
        cat, None, tmp_path, tmp_path / "out", Settings(), datetime.date(2026, 11, 5)
    )
    assert calls == ["variants", "publish"]
    assert {"added", "retired", "failed", "live", "variants", "tries_pruned"} <= set(
        summary
    )


def test_nightly_variant_failure_marks_run_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "read_key", lambda name: None)
    cat = Catalogue(tmp_path / "frame-art.db")
    cat.record_crawl("nga")
    monkeypatch.setattr(
        variants, "nightly", lambda *a, **k: {"generated": [], "gen_failed": ["k"]}
    )
    monkeypatch.setattr(
        jobs, "publish", lambda *a, **k: PublishResult(added=[], retired=[], failed=[])
    )
    summary = jobs.nightly(
        cat, None, tmp_path, tmp_path / "out", Settings(), datetime.date(2026, 11, 5)
    )
    assert summary["failed"] == ["k"]


def test_crawl_reports_new_works(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "frame-art.db")
    cat.upsert([make_work(1)])

    class Src:
        name, kind = "nga", "painting"

        def iter_works(self, http, limit):
            return [make_work(1), make_work(2), make_work(3)]

    monkeypatch.setattr(jobs, "all_sources", lambda cache, kinds: [Src()])
    monkeypatch.setattr(notability, "annotate", lambda *a, **k: None)
    before = time.time()
    out = jobs.crawl(cat, None, tmp_path / "cache", ("painting",), 10)
    assert out["new"] == 2
    assert out["since"] >= before


def test_now_showing_a_photo_shows_no_filename(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "out"
    svc = Service(data, dest, "/config/x", None, 1883, "test")
    cat = svc.catalogue()
    cat.record_publish("IMG_1234_beach.jpg", "full", "photo:7", 1)
    cat.put("tv_current", {"content_id": "MY_F1", "at": 1.0, "on": True})
    cat.db.execute(
        "update published set content_id = 'MY_F1' where filename = 'IMG_1234_beach.jpg'"
    )
    cat.db.commit()
    info = svc.now_showing(cat)
    assert info["ours"] is True
    assert info["kind"] == "photo"
    assert info["title"] == "Your photo"
    assert "IMG_1234" not in str(info.get("title"))
    assert "key" not in info
