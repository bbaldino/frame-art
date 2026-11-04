import pytest

from frame_art import studio


def store(tmp_path) -> studio.StudioStore:
    return studio.StudioStore(tmp_path / "frame-art.db", tmp_path / "studio")


def test_prompt_is_base_manner_setting_people_restraint_shape_nudges():
    p = studio.build_prompt(
        "Edward Hopper", "Flat planes.", "keep", "fill", ["warmer light"]
    )
    assert p.startswith("Paint this photograph as Edward Hopper would")
    assert "Flat planes." in p and studio.SETTING_TEXT["keep"] in p
    assert (
        studio.PEOPLE in p and studio.RESTRAINT in p and studio.SHAPE_TEXT["fill"] in p
    )
    assert p.endswith("Also: warmer light.")
    assert (
        p.index("Flat planes.")
        < p.index(studio.SETTING_TEXT["keep"])
        < p.index(studio.RESTRAINT)
    )
    r = studio.build_prompt("Edward Hopper", "", "reinvent", "keep", [])
    assert studio.SETTING_TEXT["reinvent"] in r and studio.SETTING_TEXT["keep"] not in r
    assert studio.SHAPE_TEXT["fill"] not in r and "Also:" not in r and "  " not in r


def test_retouch_prompt_keeps_the_restraint_rules():
    p = studio.retouch_prompt(["simpler background", "less desert, more beach"])
    assert p.startswith(studio.RETOUCH)
    assert "simpler background; less desert, more beach" in p and studio.RESTRAINT in p


def test_default_painters_and_suggestions(tmp_path):
    st = store(tmp_path)
    names = [p.name for p in st.painters()]
    assert names == ["Claude Monet", "Edward Hopper", "John Singer Sargent",
                     "Salvador Dalí", "Winslow Homer"]  # fmt: skip
    scene = [p.name for p in studio.suggested(st.painters(), "scene")]
    assert scene[:3] == ["Claude Monet", "Salvador Dalí", "Winslow Homer"]
    assert [p.name for p in studio.suggested(st.painters(), None)] == names
    assert "melting clocks" in st.painter("dali").manner
    for p in st.painters():
        st.remove_painter(p.slug)
    assert store(tmp_path).painters() == []  # never re-seeded


def test_painter_edits_are_checked(tmp_path):
    st = store(tmp_path)
    p = st.add_painter("Mary Cassatt", "Soft pastel light.", ("portrait",))
    assert p.slug == "mary-cassatt" and st.painter("mary-cassatt") == p
    st.update_painter("mary-cassatt", "Mary Cassatt", "Pastel.", ("portrait", "scene"))
    assert st.painter("mary-cassatt").suits == ("portrait", "scene")
    for bad in [("", "x", ("portrait",)), ("Mary Cassatt", "x", ("portrait",)),
                ("Someone", "x", ("landscape",))]:  # fmt: skip
        with pytest.raises(ValueError):
            st.add_painter(*bad)


# -- tries -----------------------------------------------------------------------

import io  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from PIL import Image  # noqa: E402

from frame_art.photos import PhotoStore  # noqa: E402
from frame_art.providers import Painted, PaintFailed  # noqa: E402


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def paint(self, im, prompt, aspect=None, size="4K"):
        self.calls.append(
            {"size": size, "aspect": aspect, "prompt": prompt, "im": im.size}
        )
        if self.fail:
            raise PaintFailed(self.fail)
        return Painted(Image.new("RGB", (64, 36), "orange"), 0.12, "gen")


def photo(tmp_path, size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "JPEG")
    ps = PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")
    return ps, ps.add("upload", f"x{size}", buf.getvalue(), "p.jpg")


HOPPER = studio.Painter("hopper", "Edward Hopper", "Flat planes.", ("portrait",))


def test_paint_fill_and_keep_shapes(tmp_path):
    ps, p = photo(tmp_path, (30, 40))
    st, prov = store(tmp_path), FakeProvider()
    t = st.start_try(p.id, HOPPER, "keep", "fill", ["warmer light"], None)
    assert t.status == "queued" and t.prompt.endswith("Also: warmer light.")
    done = studio.paint(st, ps, prov, t.id)
    assert prov.calls[0]["size"] == "4K" and prov.calls[0]["aspect"] == "16:9"
    assert done.status == "done" and done.cost == 0.12 and Path(done.path).is_file()
    k = st.start_try(p.id, HOPPER, "keep", "keep", [], None)
    studio.paint(st, ps, prov, k.id)
    assert prov.calls[1]["aspect"] == "3:4"
    assert st.spent(p.id) == pytest.approx(0.24)
    assert [x.id for x in st.tries(p.id)] == [k.id, t.id]


def test_paint_again_from_a_try(tmp_path):
    ps, p = photo(tmp_path)
    st, prov = store(tmp_path), FakeProvider()
    first = studio.paint(
        st, ps, prov, st.start_try(p.id, HOPPER, "keep", "fill", [], None).id
    )
    again = st.start_try(p.id, HOPPER, "keep", "fill", ["simpler background"], first.id)
    studio.paint(st, ps, prov, again.id)
    assert prov.calls[1]["im"] == (64, 36)  # the earlier try, not the photo
    assert prov.calls[1]["prompt"].startswith(studio.RETOUCH)


def test_a_failed_paint_says_why_and_costs_nothing(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    prov = FakeProvider("OpenRouter returned 402: requires at least $1.00")
    t = studio.paint(
        st, ps, prov, st.start_try(p.id, HOPPER, "keep", "fill", [], None).id
    )
    assert (t.status, t.error, t.cost, t.path) == (
        "failed", "OpenRouter: balance too low", None, None)  # fmt: skip
    assert st.spent(p.id) == 0


def test_old_unsaved_tries_are_pruned(tmp_path):
    ps, p = photo(tmp_path)
    st, prov = store(tmp_path), FakeProvider()
    old = studio.paint(
        st, ps, prov, st.start_try(p.id, HOPPER, "keep", "fill", [], None).id
    )
    new = studio.paint(
        st, ps, prov, st.start_try(p.id, HOPPER, "keep", "fill", [], None).id
    )
    st.db.execute(
        "update tries set made_at = ? where id = ?", (time.time() - 15 * 86400, old.id)
    )
    st.db.execute(
        "update tries set made_at = ? where id = ?", (time.time() - 13 * 86400, new.id)
    )
    st.db.commit()
    assert st.prune(time.time()) == 1
    assert st.get_try(old.id) is None and not Path(old.path).exists()
    assert st.get_try(new.id) is not None


# -- saving -----------------------------------------------------------------------


def done_try(st, ps, p, prov=None):
    t = st.start_try(p.id, HOPPER, "keep", "fill", [], None)
    return studio.paint(st, ps, prov or FakeProvider(), t.id)


def test_save_copies_the_try_without_painting(tmp_path):
    ps, p = photo(tmp_path)
    st, prov = store(tmp_path), FakeProvider()
    t = done_try(st, ps, p, prov)
    calls = len(prov.calls)
    saved = st.save(t.id)
    assert len(prov.calls) == calls
    assert Path(saved.path).read_bytes() == Path(t.path).read_bytes()
    assert Path(saved.path).parent.name == "paintings"
    assert (saved.try_id, saved.photo_id, saved.painter, saved.cost) == (
        t.id, p.id, "Edward Hopper", 0.12)  # fmt: skip
    assert st.save(t.id).id == saved.id and len(st.paintings()) == 1
    assert st.get_painting(saved.id) == saved and st.saved_try_ids() == {t.id}
    failed = st.start_try(p.id, HOPPER, "keep", "fill", [], None)
    st.fail_try(failed.id, "nope")
    with pytest.raises(ValueError):
        st.save(failed.id)


def test_saved_tries_survive_pruning(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    t = done_try(st, ps, p)
    saved = st.save(t.id)
    st.db.execute("update tries set made_at = ? where id = ?",
                  (time.time() - 15 * 86400, t.id))  # fmt: skip
    st.db.commit()
    assert st.prune(time.time()) == 0
    assert Path(t.path).exists() and Path(saved.path).exists()


def test_remove_painting_deletes_row_and_file(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    saved = st.save(done_try(st, ps, p).id)
    assert st.remove_painting(saved.id) is True
    assert st.get_painting(saved.id) is None and not Path(saved.path).exists()
    assert st.remove_painting(saved.id) is False


def test_delete_try_refuses_a_saved_one(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    keep, drop = done_try(st, ps, p), done_try(st, ps, p)
    st.save(keep.id)
    assert st.delete_try(drop.id) is True
    assert st.get_try(drop.id) is None and not Path(drop.path).exists()
    with pytest.raises(ValueError):
        st.delete_try(keep.id)
    assert st.delete_try(drop.id) is False


def test_paint_of_a_deleted_try_does_nothing(tmp_path):
    ps, p = photo(tmp_path)
    st, prov = store(tmp_path), FakeProvider()
    t = st.start_try(p.id, HOPPER, "keep", "fill", [], None)
    st.delete_try(t.id)
    assert studio.paint(st, ps, prov, t.id) is None and prov.calls == []


def test_forget_photo_keeps_its_paintings(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    t, other = done_try(st, ps, p), done_try(st, ps, p)
    saved = st.save(t.id)
    st.forget_photo(p.id)
    assert st.tries(p.id) == [] and not Path(other.path).exists()
    kept = st.get_painting(saved.id)
    assert kept.photo_id is None and Path(kept.path).exists()


def test_counts_per_photo(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    t = done_try(st, ps, p)
    done_try(st, ps, p)
    st.save(t.id)
    assert st.counts() == {p.id: (2, 1)}
    assert [x.id for x in st.paintings(p.id)] == [st.paintings()[0].id]


def test_a_try_waits_in_the_queue_before_painting(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    t = st.start_try(p.id, HOPPER, "keep", "fill", [], None)
    assert t.status == "queued" and t.started_at is None

    class Watching(FakeProvider):
        def paint(self, im, prompt, aspect=None, size="4K"):
            mid = st.get_try(t.id)
            self.seen = (mid.status, mid.started_at is not None)
            return super().paint(im, prompt, aspect, size)

    prov = Watching()
    studio.paint(st, ps, prov, t.id)
    assert prov.seen == ("painting", True)


def test_a_try_deleted_mid_paint_leaves_no_file(tmp_path):
    ps, p = photo(tmp_path)
    st = store(tmp_path)
    t = st.start_try(p.id, HOPPER, "keep", "fill", [], None)

    class Deleting(FakeProvider):
        def paint(self, im, prompt, aspect=None, size="4K"):
            st.delete_try(t.id)
            return super().paint(im, prompt, aspect, size)

    assert studio.paint(st, ps, Deleting(), t.id) is None
    assert list((tmp_path / "studio" / "tries").glob("*")) == []
