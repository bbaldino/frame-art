from frame_art.library import Item, Live
from frame_art.mix import REST, member, plan, spread
from frame_art.schedules import Slice, everyday

DAY = 86400.0
NOW = 1_000 * DAY


def it(key, *tags, kind="museum", root=None, vtype=None):
    return Item(key, kind, key, 0.0, frozenset(tags), root or key, vtype=vtype)


def ver(vid, root, vtype, *tags):
    key, own = f"variant:{vid}", f"ai-variant:{vtype}"
    return it(key, own, *tags, kind="version", root=root, vtype=vtype)


def lv(key, at, root=None):
    return Live(f"{key}.jpg", "matted", key, root or key, at)


def run(layers, items, live=(), *, pool=10, count=7, pinned=(), favorites=(), last_shown=None):  # fmt: skip
    return plan(
        layers, items, list(live), pool=pool, count=count, pinned=set(pinned),
        favorites=set(favorites), last_shown=last_shown or {}, now=NOW, seed=1,
    )  # fmt: skip


paintings = [it(f"m:p{i}", "kind:painting", "source:m") for i in range(10)]
photos = [it(f"photo:{i}", "kind:photograph", "source:photos") for i in range(5)]
PHOTO_SLICE = everyday([Slice(["kind:photograph"], 20)])


def test_rest_takes_what_slices_leave():
    p = run([PHOTO_SLICE], paintings + photos)
    assert p.slices == [frozenset({"kind:photograph"}), REST]
    assert p.targets == [2, 8]


def test_shortfall_goes_to_rest():
    p = run([everyday([Slice(["kind:photograph"], 50)])], paintings + photos[:2])
    assert p.targets == [2, 8]


def test_pool_shrinks_to_library():
    p = run([PHOTO_SLICE], paintings[:2] + photos[:2], pool=150)
    assert sum(p.targets) == 4
    assert p.targets == [2, 2]  # Rest has only two, so the photos take up the slack


def test_versions_only_via_their_slice():
    version_xmas = ver(1, "m:p0", "christmas", "kind:painting", "source:m")
    assert member(version_xmas, [frozenset({"kind:painting"})]) is None
    assert member(version_xmas, [frozenset({"ai-variant:christmas"})]) == 0
    assert member(paintings[0], [frozenset({"ai-variant:christmas"})]) == 1


def test_first_matching_slice_counts():
    both = it("m:x", "kind:painting", "subject:snow")
    slices = [frozenset({"subject:snow"}), frozenset({"kind:painting"})]
    assert member(both, slices) == 0


def test_removed_live_work_retires_even_if_pinned():
    p = run([], paintings[:3], [lv("m:gone", 1.0), lv("m:p0", 2.0)], pinned={"m:gone"})
    assert p.retire == ["m:gone.jpg"]


def test_rotation_retires_about_count_oldest():
    items = [it(f"m:{i:02}", "kind:painting") for i in range(40)]
    live = [lv(f"m:{i:02}", float(100 - i)) for i in range(20)]  # m:19 oldest
    p = run([], items, live, pool=20, count=7)
    assert p.targets == [20]
    # Held back: the publisher retires them only as replacements go up.
    assert p.retire == []
    assert p.rotate == [[f"m:{i:02}.jpg" for i in range(19, 12, -1)]]


def test_rotation_keeps_pins():
    items = [it(f"m:{i:02}", "kind:painting") for i in range(40)]
    live = [lv(f"m:{i:02}", float(100 - i)) for i in range(20)]
    p = run([], items, live, pool=20, count=7, pinned={"m:19", "m:18"})
    assert p.rotate == [[f"m:{i:02}.jpg" for i in range(17, 10, -1)]]


def test_no_waiting_no_rotation():
    live = [lv(k.key, float(i)) for i, k in enumerate(paintings)]
    p = run([], paintings, live, pool=10, count=7)
    assert p.retire == [] and p.rotate == [[]]
    assert p.fill == [[]]


def test_never_shown_first_then_least_recent():
    items = [it("m:a"), it("m:b"), it("m:new")]
    shown = {"m:a": NOW - 5 * DAY, "m:b": NOW - 20 * DAY}
    p = run([], items, last_shown=shown)
    assert p.fill == [["m:new", "m:b", "m:a"]]


def test_favorite_comes_round_twice_as_often():
    items = [it("m:fav"), it("m:plain")]
    shown = {"m:fav": NOW - 10 * DAY, "m:plain": NOW - 15 * DAY}
    p = run([], items, favorites={"m:fav"}, last_shown=shown)
    assert p.fill == [["m:fav", "m:plain"]]


def test_one_root_live_once_across_slices():
    orig = it("m:p0", "kind:painting")
    xmas = ver(1, "m:p0", "christmas", "kind:painting")
    layers = [everyday([Slice(["ai-variant:christmas"], 50)])]
    p = run(
        layers, [orig, xmas, it("m:p1", "kind:painting")], [lv("m:p0", 1.0)], pool=2
    )
    assert "variant:1" in p.fill[0]
    assert "m:p0" not in p.fill[1]


def test_spread():
    assert spread(7, [50, 50]) == [4, 3]
    assert sum(spread(7, [3, 140, 7])) == 7
    assert spread(7, [0, 10, 0]) == [0, 7, 0]
    assert spread(5, [0, 0]) == [0, 0]
    assert spread(1, [1, 1, 1]) == [1, 0, 0]  # ties go to the lower index


# Rulings


def test_live_version_without_a_slice_retires_first():
    # Christmas is over: the version is still eligible but no slice names it.
    xmas = ver(1, "m:p0", "christmas", "kind:painting")
    live = [lv("m:p1", 1.0), lv("variant:1", 2.0, root="m:p0")]
    p = run([], [paintings[0], paintings[1], xmas], live, pinned={"variant:1"})
    assert p.retire[0] == "variant:1.jpg"
    assert "variant:1.jpg" not in p.retire[1:]


def test_pins_never_retire_even_over_target():
    items = [it(f"m:{i}", "kind:photograph") for i in range(6)] + paintings
    live = [lv(f"m:{i}", float(i)) for i in range(4)]
    pins = {f"m:{i}" for i in range(4)}
    p = run([PHOTO_SLICE], items, live, pool=10, count=7, pinned=pins)
    assert p.targets[0] == 2
    assert p.retire == []


def test_version_waits_when_root_live_elsewhere_not_same_slice():
    orig = it("m:p0", "kind:painting")
    xmas = ver(1, "m:p0", "christmas", "kind:painting")
    xmas2 = ver(2, "m:p0", "christmas", "kind:painting")
    layers = [everyday([Slice(["ai-variant:christmas"], 50)])]
    # root live as an original in Rest: its versions may wait in slice 0
    p = run(layers, [orig, xmas, xmas2], [lv("m:p0", 1.0)], pool=2)
    assert set(p.fill[0]) == {"variant:1", "variant:2"}
    # root live as a version in slice 0: the other version never waits there
    p = run(layers, [orig, xmas, xmas2], [lv("variant:1", 1.0, root="m:p0")], pool=2)
    assert p.fill[0] == []
    assert "m:p0" in p.fill[1]


def test_never_shown_round_robin_by_artist_then_source():
    items = [
        it("m:a1", "artist:a"), it("m:a2", "artist:a"), it("m:a3", "artist:a"),
        it("m:b1", "artist:b"), it("m:s1", "source:s"),
    ]  # fmt: skip
    p = run([], items)
    fill = p.fill[0]
    assert sorted(fill) == sorted(i.key for i in items)
    # the first three come from three different groups, a's stay in key order
    assert len({k[2] for k in fill[:3]}) == 3
    assert [k for k in fill if k.startswith("m:a")] == ["m:a1", "m:a2", "m:a3"]
    assert fill[-2:] == ["m:a2", "m:a3"]


def test_fill_lists_every_waiting_key_pinned_first():
    items = [it(f"m:{i}") for i in range(30)]
    shown = {f"m:{i}": NOW - i * DAY for i in range(30)}
    p = run([], items, pool=5, pinned={"m:3"}, last_shown=shown)
    assert len(p.fill[0]) == 30
    assert p.fill[0][:3] == ["m:3", "m:29", "m:28"]


def test_versions_never_rest_and_count_by_root():
    xmas = [ver(i, "m:p0", "christmas") for i in range(5)]
    layers = [everyday([Slice(["ai-variant:christmas"], 50)])]
    p = run(layers, [paintings[0], *xmas, paintings[1]], pool=10)
    # two roots: the pool is 2, the slice has one root available
    assert p.targets == [1, 1]
    assert not any(k.startswith("variant:") for k in p.fill[1])


def test_rotation_never_retires_what_the_waiting_cannot_replace():
    # 11 works, pool 10: 8 live, 3 waiting. Two fill the gaps, so only one rotates.
    live = [lv(k.key, float(i)) for i, k in enumerate(paintings[:8])]
    extra = [it(f"m:x{i}", "kind:painting") for i in range(3)]
    p = run([], paintings[:8] + extra, live, pool=10, count=7)
    assert p.targets == [10]
    assert p.retire == [] and p.rotate == [["m:p0.jpg"]]


def test_rotation_cap_counts_waiting_roots_not_keys():
    # Roots a and b, three Christmas versions each; only one of b's can go live.
    vs = [ver(i, "m:a" if i <= 3 else "m:b", "xmas") for i in range(1, 7)]
    layers = [everyday([Slice(["ai-variant:xmas"], 100)])]
    items = [it("m:a"), it("m:b"), *vs]
    p = run(layers, items, [lv("variant:1", 1.0, root="m:a")], pool=2)
    assert p.targets == [2, 0]
    assert p.retire == []
    assert p.fill[0] == ["variant:4", "variant:5", "variant:6"]


def test_excess_retires_now_rotation_waits():
    # Rest shrinks from 4 to 2: two retire now, and one more rotates out later.
    items = [it(f"m:{i}") for i in range(6)]
    live = [lv(f"m:{i}", float(i)) for i in range(4)]
    p = run([], items, live, pool=2, count=1)
    assert p.retire == ["m:0.jpg", "m:1.jpg"]
    assert p.rotate == [["m:2.jpg"]]


XMAS = [everyday([Slice(["ai-variant:christmas"], 25)])]


def test_versions_of_live_originals_go_first():
    rest = [it(k, "kind:painting") for k in ("m:a", "m:b", "m:c", "m:d", "m:x")]
    va = ver(1, "m:a", "christmas", "kind:painting")
    vx = ver(2, "m:x", "christmas", "kind:painting")
    live = [lv("m:d", 1.0), lv("m:c", 2.0), lv("m:b", 3.0), lv("m:a", 4.0)]
    # vx was never shown, so it would otherwise come first.
    p = run(XMAS, [*rest, va, vx], live, pool=4, count=0,
            last_shown={"variant:1": NOW - DAY})  # fmt: skip
    assert p.targets == [1, 3]
    assert p.fill[0] == ["variant:1", "variant:2"]


def test_schedule_start_swap_stands_in_for_the_excess():
    # Christmas starts: a's version replaces a, so Rest's excess is a itself
    # and the oldest (d) stays.
    rest = [it(k, "kind:painting") for k in ("m:a", "m:b", "m:c", "m:d", "m:x")]
    va = ver(1, "m:a", "christmas", "kind:painting")
    vx = ver(2, "m:x", "christmas", "kind:painting")
    live = [lv("m:d", 1.0), lv("m:c", 2.0), lv("m:b", 3.0), lv("m:a", 4.0)]
    p = run(XMAS, [*rest, va, vx], live, pool=4, count=0,
            last_shown={"variant:2": NOW - DAY})  # fmt: skip
    assert p.fill[0][0] == "variant:1"
    assert p.retire == [] and p.rotate == [[], []]
