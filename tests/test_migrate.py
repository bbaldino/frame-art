import pytest
from test_publish import Env

from frame_art import library, migrate
from frame_art.schedules import Slice, everyday


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def old_db(env: Env) -> dict:
    """A 0.11 database: Works, a collection, a ban, a retired work, live files."""
    cat, w = env.cat, [w.key for w in env.works]
    cat.db.execute("delete from work_added")  # 0.11 had no such table
    cat.db.execute("update works set crawled_at = 9000")
    cat.db.commit()
    cat.collect(w[0])
    cat.db.execute("update collection set added_at = 5000 where key = ?", (w[0],))
    cat.collect(w[1])
    cat.db.execute("update collection set added_at = 6000 where key = ?", (w[1],))
    cat.set_override(w[2], "banned")
    cat.db.execute("update overrides set set_at = 7000")
    cat.record_publish("a.jpg", "full", w[1], 1, published_at=3000)  # collected, shown
    cat.record_publish(
        "b.jpg", "full", w[2], 2, published_at=2000
    )  # shown, then banned
    cat.record_publish("c.jpg", "full", w[3], 3, published_at=1500)  # retired
    cat.record_publish("c2.jpg", "full", w[3], 3, published_at=1800)
    cat.record_retire("b.jpg", None)
    cat.record_retire("c.jpg", None)
    cat.record_retire("c2.jpg", None)
    cat.record_publish("d.jpg", "full", w[4], 4, published_at=1000)  # live
    cat.db.commit()
    return {"w": w}


def test_library_starts_with_collection_published_and_portraits(setup):
    env = setup
    w = old_db(env)["w"]
    pid = env.painting()
    env.cat.set_library([library.PORTRAIT + str(pid)], None)  # saved, never in
    v = env.version(w[5], "christmas")
    env.cat.record_publish("v.jpg", "full", w[5], 5, variant=v.id, published_at=500)
    assert migrate.upgrade(env.cat, env.studio) is True
    lib = env.cat.library()
    ins = {k for k, (s, _) in lib.items() if s == "in"}
    assert ins == {w[0], w[1], w[3], w[4], w[5], library.PORTRAIT + str(pid)}
    assert env.cat.get("library_0_12") is True


def test_banned_become_removed_and_stay_out(setup):
    env = setup
    w = old_db(env)["w"]
    env.cat.set_override(w[6], "banned")  # banned, never shown
    migrate.upgrade(env.cat, env.studio)
    lib = env.cat.library()
    assert lib[w[2]] == ("removed", 7000)  # it was published, yet not "in"
    assert lib[w[6]][0] == "removed"
    assert env.cat.overrides()[w[2]] == "banned"  # the old table is untouched


def test_dont_use_and_old_versions_are_removed(setup):
    env = setup
    w = old_db(env)["w"]
    old = env.version(w[5], "christmas", n=1)
    new = env.version(w[5], "christmas", n=2)  # current
    vetoed = env.version(w[6], "winter")
    env.cat.set_dont_use(w[6], "winter", True)
    live = env.version(w[7], "halloween", n=1)
    newer = env.version(w[7], "halloween", n=2)  # current; live file is the old one
    env.cat.record_publish("lv.jpg", "full", w[7], 7, variant=live.id, published_at=100)
    vlive = env.version(w[8], "winter")  # vetoed, and its file is live right now
    env.cat.set_dont_use(w[8], "winter", True)
    env.cat.record_publish("vl.jpg", "full", w[8], 8, variant=vlive.id, published_at=90)
    migrate.upgrade(env.cat, env.studio)
    lib = env.cat.library()
    assert lib[f"variant:{vlive.id}"][0] == "removed"
    k = lambda v: f"variant:{v.id}"  # noqa: E731
    assert k(new) not in lib and k(newer) not in lib  # current: eligible
    assert lib[k(old)][0] == "removed"
    assert lib[k(vetoed)][0] == "removed"
    assert k(live) not in lib  # live on the TV right now: stays eligible


def test_dates_backfilled_from_earliest_known(setup):
    env = setup
    w = old_db(env)["w"]
    env.cat.record_render(w[3], 3, "full", 1.0, env.tmp / "x.jpg")
    env.cat.db.execute("update rendered set rendered_at = 800 where key = ?", (w[3],))
    env.cat.record_reject(w[8], "too small")
    env.cat.db.execute("update rejected set rejected_at = 400 where key = ?", (w[8],))
    env.cat.db.commit()
    pid = env.painting()
    env.cat.set_library([library.PORTRAIT + str(pid)], None)
    env.studio.db.execute("update paintings set saved_at = 250")
    env.studio.db.commit()
    env.cat.record_crawl("zz", 1.0)  # an existing row is kept
    migrate.upgrade(env.cat, env.studio)
    added = env.cat.work_added()
    assert added[w[0]] == 9000  # only the crawl
    assert added[w[3]] == 800  # render beats crawl; publish at 1500 is later
    assert added[w[4]] == 1000
    assert added[w[8]] == 400
    assert set(added) == {x.key for x in env.works}
    crawls = env.cat.source_crawls()
    assert crawls["s0"] == crawls["s1"] == 9000 and crawls["zz"] == 1.0
    lib = env.cat.library()
    assert lib[w[0]][1] == 5000  # collection date
    assert lib[w[1]][1] == 6000  # collection date wins over publish
    assert lib[w[3]][1] == 1500  # first publish
    assert lib[library.PORTRAIT + str(pid)][1] == 250  # saved_at


def test_runs_once(setup):
    env = setup
    w = old_db(env)["w"]
    assert migrate.upgrade(env.cat, env.studio) is True
    env.cat.set_library([w[0]], "removed")  # Brian's later choice
    assert migrate.upgrade(env.cat, env.studio) is False
    assert env.cat.library()[w[0]][0] == "removed"


def test_first_run_after_upgrade_does_not_churn(setup):
    env = setup
    w = old_db(env)["w"]
    env.cat.db.execute("delete from published where key = ?", (w[3],))  # keep it simple
    env.cat.record_publish(
        "e.jpg", "full", w[0], 6, published_at=900
    )  # a collected work, live
    pid = env.painting()
    env.cat.set_library([library.PORTRAIT + str(pid)], None)
    env.cat.record_publish(f"portrait-{pid}.jpg", "full", library.PORTRAIT + str(pid), 7,
                           published_at=800)  # fmt: skip
    old = env.version(w[5], "christmas", n=1)
    v = env.version(w[5], "christmas", n=2)
    env.cat.record_publish(
        "w5.jpg", "full", w[5], 8, published_at=700
    )  # the original, retired
    env.cat.record_retire("w5.jpg", None)
    env.cat.record_publish("w5-v.jpg", "full", w[5], 9, variant=v.id, published_at=600)
    env.cat.record_publish(
        "g.jpg", "full", w[9], 11, published_at=300
    )  # shown, then retired
    env.cat.record_retire("g.jpg", None)
    live_before = {p.filename for p in env.cat.published(active=True)}
    assert old.id != v.id
    assert migrate.upgrade(env.cat, env.studio) is True
    res = env.run(
        layers=[everyday([Slice(["ai-variant:christmas"], 20)])],
        pool=150,
        count=0,
        seed=1,
    )
    assert res.retired == [] and res.failed == []
    assert live_before <= {p.filename for p in env.cat.published(active=True)}
    assert res.added == [
        "full/s0-9-artist-9-work-9.jpg"
    ]  # only the retired one fills the pool


def test_start_migrates_before_any_job_can_run(tmp_path, monkeypatch):
    import threading

    from frame_art.service import Service

    svc = Service(tmp_path / "data", tmp_path / "out", "/config/x", None, 1883, "test")
    started = []
    monkeypatch.setattr(
        threading.Thread, "start", lambda self: started.append(self.name)
    )

    def boom(cat, studio):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(migrate, "upgrade", boom)
    with pytest.raises(RuntimeError):
        svc.start()
    assert started == []  # no worker or scheduler ran against an un-migrated Library
