import io
from pathlib import Path

import pytest
from PIL import Image

from frame_art.catalogue import Catalogue
from frame_art.photos import PhotoStore, open_image


def jpeg(color="red", size=(40, 30), orientation=None) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if orientation:
        exif = im.getexif()
        exif[0x0112] = orientation
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def store(tmp_path) -> PhotoStore:
    return PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")


def test_add_get_and_dedupe(tmp_path):
    st = store(tmp_path)
    p = st.add("upload", "a.jpg", jpeg(), "a.jpg")
    assert p and (p.width, p.height, p.kind) == (40, 30, None)
    assert Path(p.path).is_file() and Path(p.path).parent == tmp_path / "photos"
    assert Path(p.path).name == f"{p.id}.jpg"  # the user's name is never a path
    assert st.add("upload", "a.jpg", jpeg("blue"), "a.jpg") is None  # same source id
    assert st.add("google-photos", "g1", jpeg(), "IMG_1.jpg") is None  # same bytes
    assert [x.id for x in st.all()] == [p.id]
    assert st.get(p.id) == p and st.get("nope") is None


def test_newest_first(tmp_path):
    st = store(tmp_path)
    a = st.add("upload", "a", jpeg("red"), "a.jpg")
    b = st.add("upload", "b", jpeg("blue"), "b.jpg")
    assert [x.id for x in st.all()] == [b.id, a.id]


def test_kind_and_remove(tmp_path):
    st = store(tmp_path)
    p = st.add("upload", "a", jpeg(), "a.jpg")
    st.set_kind(p.id, "scene", "2 people, water", True)
    got = st.get(p.id)
    assert (got.kind, got.kind_reason, got.kind_auto) == (
        "scene",
        "2 people, water",
        True,
    )
    with pytest.raises(ValueError):
        st.set_kind(p.id, "landscape", None, False)
    assert st.remove(p.id) and not Path(p.path).exists() and st.get(p.id) is None
    assert not st.remove(p.id)


def test_photos_never_reach_the_catalogue(tmp_path):
    st = store(tmp_path)
    st.add("upload", "a", jpeg(), "a.jpg")
    assert Catalogue(tmp_path / "frame-art.db").works() == []


def test_exif_orientation_is_applied(tmp_path):
    st = store(tmp_path)
    p = st.add("upload", "r", jpeg(orientation=6), "r.jpg")
    assert (p.width, p.height) == (30, 40)
    assert open_image(p).size == (30, 40)


def test_not_an_image_is_refused(tmp_path):
    with pytest.raises(ValueError):
        store(tmp_path).add("upload", "x", b"not a picture", "x.jpg")


def test_multi_picture_jpeg_is_a_photo(tmp_path):
    # Samsung portrait shots and many cameras save MPO (a JPEG with a second image).
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(
        buf, "MPO", save_all=True, append_images=[Image.new("RGB", (40, 30), "blue")]
    )
    p = store(tmp_path).add("upload", "m", buf.getvalue(), "m.jpg")
    assert p is not None and p.path.endswith(".jpg")
