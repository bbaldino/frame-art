import json

from frame_art import imaging, pipeline
from frame_art.settings import Settings

DROPPED = {
    "choose_from": "collection",
    "photo_share": 0,
    "photo_crop": "loose",
    "allow_nudes": True,
    "full_screen_only": True,
    "repeat_days": 30,
}


def load(tmp_path, raw):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(raw))
    return Settings.load(p)


def test_photo_share_moves_into_everyday(tmp_path):
    s = load(tmp_path, {"photo_share": 20, "choose_from": "collection"})
    assert [(sl.tags, sl.share) for sl in s.schedules[0].slices] == [
        (["kind:photograph"], 20)
    ]
    assert s.schedules[0].id == "everyday"
    assert not hasattr(s, "photo_share")


def test_photo_share_joins_an_existing_everyday(tmp_path):
    sched = {
        "id": "e",
        "name": "Everyday",
        "start": "01-01",
        "end": "12-31",
        "shares": {"halloween": 30},
    }
    s = load(tmp_path, {"photo_share": 20, "schedules": [sched]})
    assert len(s.schedules) == 1
    assert [(sl.tags, sl.share) for sl in s.schedules[0].slices] == [
        (["ai-variant:halloween"], 30),
        (["kind:photograph"], 20),
    ]


def test_photo_share_survives_a_renamed_everyday_keeping_its_id(tmp_path):
    # The first Everyday, renamed and re-dated, still carries the fixed id.
    sched = {
        "id": "everyday",
        "name": "Halloween",
        "start": "10-01",
        "end": "10-31",
        "shares": {"halloween": 30},
    }
    s = load(tmp_path, {"photo_share": 75, "schedules": [sched]})
    assert [x.name for x in s.schedules] == ["Halloween", "Everyday"]
    every = s.schedules[1]
    assert every.id == "everyday-photos"
    assert [(sl.tags, sl.share) for sl in every.slices] == [(["kind:photograph"], 75)]
    assert (
        load(tmp_path, {"photo_share": 75, "schedules": [sched]}).schedules[1].id
        == every.id
    )


def test_photo_share_not_added_again_once_slices_exist(tmp_path):
    sched = {
        "id": "everyday",
        "name": "Everyday",
        "start": "01-01",
        "end": "12-31",
        "slices": [],
    }
    s = load(tmp_path, {"photo_share": 20, "schedules": [sched]})
    assert s.schedules[0].slices == []


def test_zero_photo_share_adds_nothing(tmp_path):
    assert load(tmp_path, {"photo_share": 0}).schedules == []


def test_old_mix_becomes_slices(tmp_path):
    s = load(tmp_path, {"mix": {"halloween": 40, "portraits": 10}})
    assert s.schedules[0].id == "everyday"
    assert [(sl.tags, sl.share) for sl in s.schedules[0].slices] == [
        (["ai-variant:halloween"], 40),
        (["source:studio"], 10),
    ]


def test_dropped_settings_are_ignored(tmp_path):
    s = load(tmp_path, {**DROPPED, "count": 9, "crop": "loose"})
    assert (s.count, s.crop) == (9, "loose")
    assert s.schedules == []
    for k in DROPPED:
        assert not hasattr(s, k)


def test_prefs_are_fixed():
    assert Settings(crop="loose").prefs() == pipeline.Prefs(
        imaging.CROP_LEVELS["loose"], full_screen_only=False
    )


def test_save_and_load_round_trip(tmp_path):
    s = load(tmp_path, {"photo_share": 20})
    p = tmp_path / "out.json"
    s.save(p)
    assert Settings.load(p).schedules == s.schedules
