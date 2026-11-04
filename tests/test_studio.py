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
