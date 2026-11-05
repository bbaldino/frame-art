import io
import json

import numpy as np
import pytest
from PIL import Image

from frame_art import imaging, library, pipeline, publish
from frame_art.catalogue import Catalogue
from frame_art.model import Work
from frame_art.photos import PhotoStore
from frame_art.schedules import Slice, everyday
from frame_art.studio import Painter, StudioStore

HOPPER = Painter("hopper", "Edward Hopper", "Flat planes.", ("portrait",))
DAY = 86400.0


def make_work(i: int, width=4000, height=2250) -> Work:
    return Work(
        source=f"s{i % 3}", source_id=str(i), title=f"Work {i}", artist=f"Artist {i}",
        date="1900", kind="painting", license="CC0", image_url=f"http://x/{i}.jpg",
        width=width, height=height,
    )  # fmt: skip


def noise(seed: int) -> Image.Image:
    # Distinct noise per work so perceptual dedupe sees different pictures.
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 255, (36, 64, 3), dtype=np.uint8))


def jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


class Env:
    def __init__(self, tmp_path, monkeypatch):
        db = tmp_path / "db.sqlite"
        self.tmp = tmp_path
        self.cat = Catalogue(db)
        self.works = [make_work(i) for i in range(10)] + [
            make_work(i, 2000, 3000) for i in range(10, 14)
        ]
        self.cat.upsert(self.works)
        self.photos = PhotoStore(db, tmp_path / "photos")
        self.studio = StudioStore(db, tmp_path / "studio")
        self.data = tmp_path / "data"
        self.dest = tmp_path / "frame_upload"
        self.styles: dict[str, str] = {}  # work key -> pad style render_image got

        def fake_render(http, cand, pad_style="blur"):
            im = noise(int(cand.work.source_id))
            return pipeline.Output(cand.work, cand.plan, imaging.phash(im), im)

        def fake_render_image(work, im, prefs, pad_style="blur"):
            self.styles[work.key] = pad_style
            plan = imaging.plan_for(*im.size, prefs.max_crop)
            small = im.convert("RGB").resize((64, 36))
            return pipeline.Output(work, plan, imaging.phash(im), small)

        monkeypatch.setattr(pipeline, "render", fake_render)
        monkeypatch.setattr(pipeline, "render_image", fake_render_image)

    def add(self, *keys: str) -> None:
        self.cat.set_library(keys, "in")

    def pub(self, pad_style="blur") -> publish.Publisher:
        return publish.Publisher(
            self.cat, None, self.dest, pad_style, "strict", self.photos, self.studio,
            self.data,
        )  # fmt: skip

    def run(self, layers=(), pool=50, count=7, seed=1, now=None, pad_style="blur"):
        return self.pub(pad_style).run(list(layers), pool, count, seed, now=now)

    def live(self) -> set[str]:
        return {p.key for p in self.cat.published(active=True)}

    def photo(self, size=(640, 480), color="red") -> str:
        p = self.photos.add(
            "upload", color, jpeg(Image.new("RGB", size, color)), "a.jpg"
        )
        self.add(library.PHOTO + p.id)
        return p.id

    def painting(self, size=(320, 180), color="orange") -> int:
        t = self.studio.start_try("ph1", HOPPER, "keep", "fill", [], None)
        self.studio.finish_try(t.id, Image.new("RGB", size, color), 0.12)
        p = self.studio.save(t.id)
        self.add(library.PORTRAIT + str(p.id))
        return p.id

    def version(self, key: str, type: str, n: int = 1, size=(640, 360)):
        if type not in {t.slug for t in self.cat.variant_types()}:
            self.cat.add_variant_type(type, type.title(), "p")
        path = self.tmp / "v" / f"{key}-{type}-{n}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg(noise(1000 + n)))
        return self.cat.add_variant(key, type, n, "p", "m", None, str(path))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def everything(env: Env) -> None:
    env.add(*(w.key for w in env.works))


def test_run_fills_the_pool_from_the_library(setup):
    env = setup
    picked = [w.key for w in env.works[:5]]
    env.add(*picked)
    res = env.run(pool=50)
    assert len(res.added) == 5 and not res.retired and not res.failed
    assert env.live() == set(picked)
    index = json.loads((env.dest / "_meta" / "index.json").read_text())
    assert set(index) == set(res.added)
    assert not list((env.dest / ".staging").glob("*"))  # staging left empty


def test_nothing_outside_the_library(setup):
    env = setup
    res = env.run(pool=50)
    assert res.added == [] and env.live() == set()


def test_photo_and_portrait_publish_under_their_keys(setup):
    env = setup
    pid = env.photo()
    paint = env.painting(size=(180, 240))  # photo-shaped: padded, as tall museum works
    res = env.run(pool=50, pad_style="matte")
    assert sorted(res.added) == [f"full/photo-{pid}.jpg", f"full/portrait-{paint}.jpg"]
    assert env.live() == {f"photo:{pid}", f"portrait:{paint}"}
    # A matte pad style still pads: HA uploads these from full/ (R5).
    assert set(env.styles.values()) == {"blur"}
    assert not (env.dest / "matted").exists()
    # Neither goes into the public index.
    assert json.loads((env.dest / "_meta" / "index.json").read_text()) == {}


def test_pending_lists_all_kinds(setup):
    env = setup
    env.add("s0:0")
    pid, paint = env.photo(), env.painting()
    res = env.run(pool=50)
    todo = publish.pending(env.cat, env.dest)
    assert sorted(todo["to_upload"]) == sorted(res.added)
    assert f"full/photo-{pid}.jpg" in todo["to_upload"]
    assert f"full/portrait-{paint}.jpg" in todo["to_upload"]
    assert todo["live"] == 3


def christmas(share=100):
    return [everyday([Slice(["ai-variant:christmas"], share)])]


def test_version_replaces_its_live_original(setup):
    env = setup
    env.add("s0:0")
    env.run(pool=1)
    assert env.live() == {"s0:0"}
    v = env.version("s0:0", "christmas")
    res = env.run(christmas(), pool=1, count=0)
    stem = pipeline.filename(env.works[0])[: -len(".jpg")]
    name = f"{stem}--christmas-1.jpg"
    assert res.retired == [f"full/{pipeline.filename(env.works[0])}"]
    assert res.added == [f"full/{name}"]
    (live,) = env.cat.published(active=True)
    assert (live.key, live.filename) == ("s0:0", name)
    assert env.cat.published_variants() == {name: v.id}
    files = library.live(env.cat, library.items(env.cat, env.photos, env.studio))
    assert [f.key for f in files] == [f"variant:{v.id}"]


def test_version_retires_its_original_from_another_slice(setup):
    # The original is live in Rest; the Christmas slice picks its version, so
    # the publisher retires the original itself, and that is Rest's excess:
    # s1:1 stays even though it is the oldest.
    env = setup
    env.add("s0:0", "s1:1")
    env.run(pool=2, now=1000)
    env.cat.db.execute("update published set published_at = 1 where key = 's1:1'")
    env.cat.db.commit()
    v = env.version("s0:0", "christmas")
    res = env.run(christmas(50), pool=2, count=0)
    assert res.retired == [f"full/{pipeline.filename(env.works[0])}"]
    assert env.live() == {"s0:0", "s1:1"}
    files = library.live(env.cat, library.items(env.cat, env.photos, env.studio))
    assert {f.key for f in files} == {f"variant:{v.id}", "s1:1"}


def test_original_waits_while_its_version_is_live(setup):
    env = setup
    env.add("s0:0", "s1:1", "s2:2")
    env.cat.set_override("s0:0", "pinned")  # first in line for Rest, even so
    v = env.version("s0:0", "christmas")
    env.run(christmas(50), pool=3, count=0)
    files = library.live(env.cat, library.items(env.cat, env.photos, env.studio))
    keys = {f.key for f in files}
    assert keys == {f"variant:{v.id}", "s1:1", "s2:2"}


def test_two_versions_of_one_original_never_live_together(setup):
    env = setup
    env.add("s0:0", "s1:1", "s2:2")
    a = env.version("s0:0", "christmas")
    b = env.version("s0:0", "winter")
    layers = [
        everyday(
            [Slice(["ai-variant:christmas"], 30), Slice(["ai-variant:winter"], 30)]
        )
    ]
    env.run(layers, pool=3, count=0)
    files = library.live(env.cat, library.items(env.cat, env.photos, env.studio))
    roots = [f.root for f in files]
    assert len(roots) == len(set(roots))
    assert f"variant:{a.id}" in {f.key for f in files}
    assert f"variant:{b.id}" not in {f.key for f in files}


def test_version_of_removed_original_retires(setup):
    env = setup
    env.add("s0:0", "s1:1")
    env.version("s0:0", "christmas")
    env.run(christmas(50), pool=2, count=0)
    assert len(env.cat.published_variants()) == 1
    all_items = library.items(env.cat, env.photos, env.studio)
    library.remove(env.cat, all_items, ["s0:0"])
    res = env.run(christmas(50), pool=2, count=0)
    assert len(res.retired) == 1 and "christmas" in res.retired[0]
    assert env.live() == {"s1:1"}


def test_sync_retires_removed_and_deleted(setup):
    env = setup
    env.add("s0:0", "s1:1")
    pid = env.photo()
    paint = env.painting()
    env.run(pool=50)
    name = f"portrait-{paint}.jpg"
    (env.dest / "full" / publish.SIDECAR).write_text(
        json.dumps({name: {"content_id": "MY_F0099", "modified": 1.0}})
    )
    library.remove(env.cat, library.items(env.cat, env.photos, env.studio), ["s0:0"])
    env.photos.remove(pid)
    env.studio.remove_painting(paint)
    retired = env.pub().sync()
    assert sorted(retired) == sorted(
        [
            f"full/{pipeline.filename(env.works[0])}",
            f"full/photo-{pid}.jpg",
            f"full/{name}",
        ]
    )
    assert env.live() == {"s1:1"}
    assert not (env.dest / "full" / name).exists()
    assert "MY_F0099" in publish.pending(env.cat, env.dest)["retire"]
    # A removed work never comes back on its own.
    env.run(pool=50, seed=2)
    assert env.live() == {"s1:1"}


def test_rejected_work_is_recorded_and_skipped(setup, monkeypatch):
    env = setup
    everything(env)
    real = pipeline.render

    def render(http, cand, pad_style="blur"):
        if cand.work.source_id == "0":
            raise pipeline.NotRectangular("round")
        return real(http, cand, pad_style)

    monkeypatch.setattr(pipeline, "render", render)
    res = env.run(pool=50)
    assert len(res.added) == 13 and not res.failed
    assert env.cat.rejected_keys() == {"s0:0"}


def test_render_failure_is_failed_and_skipped(setup, monkeypatch):
    env = setup
    env.add("s0:0", "s1:1")

    def render(http, cand, pad_style="blur"):
        raise OSError("museum down")

    monkeypatch.setattr(pipeline, "render", render)
    res = env.run(pool=50)
    assert res.added == [] and sorted(res.failed) == ["s0:0", "s1:1"]
    assert env.cat.rejected_keys() == set()


def test_excluded_work_is_skipped_not_rejected(setup):
    # Exclusions depend on settings (crop, upscale): skipped this run only.
    env = setup
    w = make_work(20)
    w.title = "Madonna (tondo)"
    env.cat.upsert([w])
    env.add(w.key, "s0:0")
    items = library.items(env.cat, env.photos, env.studio)
    with pytest.raises(publish.Skipped):
        env.pub().publish_item(items[w.key])
    res = env.run(pool=50)
    assert len(res.added) == 1 and not res.failed
    assert env.cat.rejected() == {}


def test_own_pictures_and_versions_are_never_rejected(setup, monkeypatch):
    env = setup
    env.add("s0:0")
    pid, paint = env.photo(), env.painting()
    env.version("s0:0", "christmas")

    def render_image(work, im, prefs, pad_style="blur"):
        raise pipeline.TooSmall("needs 3x upscaling")

    monkeypatch.setattr(pipeline, "render_image", render_image)
    res = env.run(christmas(50), pool=50)
    assert len(res.added) == 1 and not res.failed  # just the museum original
    assert env.cat.rejected() == {}
    assert {f"photo:{pid}", f"portrait:{paint}"} <= {
        it.key for it in library.eligible(env.cat, library.items(env.cat, env.photos, env.studio))
    }  # fmt: skip


def test_duplicate_picture_is_skipped(setup, monkeypatch):
    env = setup
    env.add("s0:0", "s1:1")

    def render(http, cand, pad_style="blur"):
        im = noise(7)  # the same picture from two museums
        return pipeline.Output(cand.work, cand.plan, imaging.phash(im), im)

    monkeypatch.setattr(pipeline, "render", render)
    res = env.run(pool=50)
    assert len(res.added) == 1 and not res.failed
    assert env.cat.rejected_keys() == set()


def test_rotation_retires_only_what_it_replaced(setup, monkeypatch):
    env = setup
    everything(env)
    env.run(pool=10, count=0, now=1000)
    first = sorted(p.filename for p in env.cat.published(active=True))
    calls = []

    def render(http, cand, pad_style="blur"):
        calls.append(cand.work.key)
        raise OSError("museum down")

    monkeypatch.setattr(pipeline, "render", render)
    res = env.run(pool=10, count=4, seed=2, now=1000 + DAY)
    assert res.retired == [] and len(res.failed) == 4
    assert sorted(p.filename for p in env.cat.published(active=True)) == first

    # One of four renders works: one retires, the oldest.
    oldest = env.cat.published(active=True)[0]

    def render_one(http, cand, pad_style="blur"):
        if calls:
            raise OSError("museum down")
        calls.append(cand.work.key)
        im = noise(int(cand.work.source_id))
        return pipeline.Output(cand.work, cand.plan, imaging.phash(im), im)

    calls.clear()
    monkeypatch.setattr(pipeline, "render", render_one)
    res = env.run(pool=10, count=4, seed=3, now=1000 + 2 * DAY)
    assert len(res.added) == 1
    assert res.retired == [f"{oldest.folder}/{oldest.filename}"]
    assert len(env.live()) == 10


def test_schedule_start_version_replaces_without_extra_churn(setup):
    env = setup
    env.add("s0:0", "s1:1", "s2:2", "s0:3")
    env.run(pool=4, count=0, now=1000)
    env.cat.db.execute("update published set published_at = 1 where key != 's0:3'")
    env.cat.db.commit()  # s0:3 is the newest, so it isn't Rest's oldest
    v = env.version("s0:3", "christmas")
    res = env.run(christmas(25), pool=4, count=0)
    # Only the original goes: its version takes its place.
    assert len(res.retired) == 1 and len(res.added) == 1
    files = library.live(env.cat, library.items(env.cat, env.photos, env.studio))
    assert {f.key for f in files} == {"s0:0", "s1:1", "s2:2", f"variant:{v.id}"}


def test_pinned_stays_live_past_rotation(setup):
    env = setup
    everything(env)
    env.cat.set_override("s1:13", "pinned")
    res = env.run(pool=2, count=0, now=1000)
    assert "-13-" in res.added[0]  # pins go up first
    for seed in range(2, 6):
        env.run(pool=2, count=2, seed=seed, now=1000 + seed * DAY)
        live = env.live()
        assert "s1:13" in live and len(live) == 2


def test_least_recently_shown_goes_up_first(setup):
    env = setup
    env.add("s0:0", "s1:1")
    env.run(pool=1, now=1000)
    (first,) = env.live()
    env.run(pool=1, count=1, seed=2, now=1000 + DAY)
    assert env.live() == {"s0:0", "s1:1"} - {first}
    env.run(pool=1, count=1, seed=3, now=1000 + 2 * DAY)
    assert env.live() == {first}


def test_matte_style_puts_padded_museum_works_in_matted(setup):
    env = setup
    everything(env)
    res = env.run(pool=50, pad_style="matte")
    folders = {f.split("/")[0] for f in res.added}
    assert folders == {"full", "matted"}
    for f in res.added:
        name = f.split("/")[1]
        tall = any(f"-{i}-" in name for i in range(10, 14))
        assert f.startswith("matted/") == tall


def test_retire_reads_sidecar_and_lists_content_ids(setup):
    env = setup
    everything(env)
    first = env.run(pool=3, now=1000).added
    # Pretend HA uploaded those three.
    sidecar = {f.split("/")[1]: {"content_id": f"MY_F{i}", "modified": 1.0}
               for i, f in enumerate(first)}  # fmt: skip
    (env.dest / "full" / publish.SIDECAR).write_text(json.dumps(sidecar))
    sidecar_before = (env.dest / "full" / publish.SIDECAR).read_text()

    res = env.run(pool=4, count=2, seed=2, now=1000 + DAY)
    assert len(res.retired) == 2
    assert set(res.retired) <= set(first)
    for f in res.retired:
        assert not (env.dest / f).exists()
    todo = publish.pending(env.cat, env.dest)
    assert sorted(todo["retire"]) == sorted(sidecar[f.split("/")[1]]["content_id"]
                                            for f in res.retired)  # fmt: skip
    # Three live works were never uploaded (not in the sidecar) ...
    assert len(todo["to_upload"]) == 3
    # ... and once HA confirms a delete, it drops off the list.
    env.cat.record_deleted(todo["retire"][0])
    assert len(publish.pending(env.cat, env.dest)["retire"]) == 1
    assert (env.dest / "full" / publish.SIDECAR).read_text() == sidecar_before
    assert len(env.cat.published(active=True)) == 4


def test_republished_work_gets_a_new_filename(setup):
    env = setup
    env.add("s0:0")
    env.run(pool=1)
    library.remove(env.cat, library.items(env.cat, env.photos, env.studio), ["s0:0"])
    env.pub().sync()
    library.put_back(env.cat, ["s0:0"])
    res = env.run(pool=1, seed=2)
    stem = pipeline.filename(env.works[0])[: -len(".jpg")]
    assert res.added == [f"full/{stem}-2.jpg"]


def test_ban_unstars_but_collect_keeps_the_ban(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.upsert([make_work(1)])
    cat.collect("s1:1")
    cat.set_override("s1:1", "banned")
    assert cat.collection() == {}
    cat.collect("s1:1")  # starring is independent of a ban
    assert "s1:1" in cat.collection() and cat.overrides() == {"s1:1": "banned"}


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


def test_published_lists_every_key(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.record_publish("a.jpg", "full", "s:1", 0)
    cat.record_publish("portrait-1.jpg", "full", "portrait:1", 0)
    cat.record_publish("photo-x.jpg", "full", "photo:x", 0)
    assert {p.key for p in cat.published()} == {"s:1", "portrait:1", "photo:x"}


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
