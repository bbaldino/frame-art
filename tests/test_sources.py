from frame_art.settings import SOURCE_NAMES, SOURCES
from frame_art.sources import KINDS, SOURCE_INFO, all_sources, info


def test_every_crawler_is_crawled_and_public(tmp_path):
    for src in all_sources(tmp_path, KINDS):
        i = info(src.name)
        assert (i.mode, i.private) == ("crawled", False)
    assert set(SOURCES) <= set(SOURCE_INFO)
    assert info("met").label == SOURCE_NAMES["met"]


def test_personal_sources_are_picked_and_private():
    up = info("upload")
    assert (up.mode, up.private, up.needs_login) == ("picked", True, False)
    g = info("google-photos")
    assert (g.mode, g.private, g.needs_login, g.label) == (
        "picked", True, True, "Google Photos"
    )  # fmt: skip
