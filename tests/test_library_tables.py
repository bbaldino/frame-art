import time

from frame_art import jobs
from frame_art.catalogue import Catalogue
from frame_art.model import Work


def make_work(i: int) -> Work:
    return Work(
        source=f"s{i}", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=4000, height=2250,
    )  # fmt: skip


def test_added_is_set_once(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    monkeypatch.setattr(time, "time", lambda: 100.0)
    cat.upsert([make_work(1)])
    monkeypatch.setattr(time, "time", lambda: 200.0)
    cat.upsert([make_work(1), make_work(2)])
    assert cat.work_added() == {"s1:1": 100.0, "s2:2": 200.0}


def test_crawl_times_are_per_source(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.record_crawl("met", 10.0)
    cat.record_crawl("nga", 20.0)
    cat.record_crawl("met", 30.0)
    assert cat.source_crawls() == {"met": 30.0, "nga": 20.0}
    assert jobs.last_crawl(cat) == 30.0


def test_last_crawl_falls_back_to_works(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    monkeypatch.setattr(time, "time", lambda: 50.0)
    cat.upsert([make_work(1)])
    assert jobs.last_crawl(cat) == 50.0


def test_library_states(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    monkeypatch.setattr(time, "time", lambda: 1.0)
    cat.set_library(["a:1", "photo:x"], "in")
    monkeypatch.setattr(time, "time", lambda: 2.0)
    cat.set_library(["a:1"], "in")  # already in: date kept
    cat.set_library(["photo:x"], "removed")
    assert cat.library() == {"a:1": ("in", 1.0), "photo:x": ("removed", 2.0)}
    monkeypatch.setattr(time, "time", lambda: 3.0)
    cat.set_library(["photo:x"], "in")  # put back: date reset
    cat.set_library(["a:1"], None)
    assert cat.library() == {"photo:x": ("in", 3.0)}


def test_removing_a_museum_work_also_bans_it_for_older_releases(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.collect("a:1")
    cat.set_library(["a:1", "photo:x"], "removed")
    assert cat.overrides() == {"a:1": "banned"}  # photo keys are not mirrored
    assert "a:1" in cat.collection()  # the star survives
    cat.set_library(["a:1"], "in")
    assert cat.overrides() == {}


def test_star_does_not_change_membership(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.set_library(["a:1"], "removed")
    cat.collect("a:1")
    assert cat.library()["a:1"][0] == "removed"


def test_user_tags(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.set_tag(["a:1", "photo:x"], "Christmas", True)
    cat.set_tag(["a:1"], "christmas", False)
    assert cat.user_tags() == {"photo:x": {"christmas"}}


def test_old_schema_reads_still_work(tmp_path):
    # The statements 0.11.0 runs against these tables must still succeed.
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.db.execute("insert into works values (?, ?, ?, ?)", ("a:1", "a", "{}", 1.0))
    cat.db.execute(
        "insert into published values ('photo-x.jpg','full','photo:x','00',1,null,null)"
    )
    assert cat.db.execute("select count(*) from works").fetchone() == (1,)
