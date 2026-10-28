# Mix Schedules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Seasonal page's single mix with yearly, overlapping schedules that set the mix by date, combined "off the top", with a one-week lookahead for making versions.

**Architecture:** A new pure module `schedules.py` holds the `Schedule` type and all date and share arithmetic (which schedules cover a day, their order, the cascade into per-type work counts). `Settings` stores the schedules in place of `mix` and migrates an old mix to an "Everyday" schedule. `variants.nightly` uses the cascade twice: once without limits, to decide what to generate (today plus upcoming schedules), and once capped by the versions that are ready, to decide what the TV shows (the `slots` table). The Seasonal page gets a timeline, a strip for today's screen, and a schedule editor.

**Tech Stack:** Python 3.12, Starlette + Jinja2, SQLite, pytest, ruff 0.16.1 (`uvx ruff@0.16.1 check .` and `uvx ruff@0.16.1 format --check .`).

**Spec:** `docs/superpowers/specs/2026-10-28-mix-schedules-design.md`

## Global Constraints

- Schedules repeat every year; dates are month and day, stored as `"MM-DD"`, inclusive, and may wrap the new year. From = to is one day.
- A schedule's shares are whole numbers 0–100 per variant type, summing to at most 100.
- Overlaps combine "off the top": narrowest range first; ties go to the later start in the year, then to the later-created schedule (later in the list).
- Lookahead is fixed at 7 days. The nightly limit (`variant_limit`, 0–50, default 10) stays one shared budget, spent narrowest schedule first across running and upcoming schedules.
- Feb 29 runs on Feb 28 in non-leap years.
- Shares for unknown (deleted) variant types are ignored.
- "Today" is the nightly run's date in the service's timezone (`Service.tz`).
- The web UI stays LAN-only, holds no HA token and never talks to the TV. No new JS framework: plain HTML/CSS plus a small inline script.
- Copy on the page uses the wording agreed in the spec: "every year", "+ Add a type", "+ Add schedule", "New versions made per night", "N of M ready", "This schedule takes X% of the screen; the rest follows whatever wider schedule is running".

## Review Focus

1. A `settings.json` with a malformed schedule (bad date, share 150, not a list): load must drop the `schedules` field with a warning and keep every other setting (Task 2 test).
2. Saving the main Settings page must not wipe the schedules or the nightly limit (Task 2 test).
3. A schedule whose type was later deleted: the type is ignored everywhere, with no crash on the Seasonal page or in the nightly run (Task 1 and Task 4 tests).
4. Editor posts with junk (share "x", month 13, Feb 30, total over 100): rejected with errors, and the form comes back showing what was typed (Task 4 test).
5. An edit or delete for an unknown schedule id returns 404, not a 500 (Task 4 test).

---

### Task 1: The schedule model and arithmetic

**Files:**
- Create: `src/frame_art/schedules.py`
- Test: `tests/test_schedules.py`

**Interfaces:**
- Produces:
  - `@dataclass class Schedule: id: str; name: str; start: str; end: str; shares: dict[str, int]` (start and end are `"MM-DD"`).
  - `Schedule.total() -> int`: the sum of the shares.
  - `covers(s: Schedule, day: date) -> bool`
  - `length(s: Schedule) -> int`: days in the range, counted in a non-leap year (Feb 29 counts as Feb 28).
  - `known(schedules: list[Schedule], types: set[str]) -> list[Schedule]`: copies with shares limited to `types`, in the same order.
  - `layers(schedules: list[Schedule], day: date) -> list[Schedule]`: the schedules covering `day`, narrowest first.
  - `order(schedules: list[Schedule]) -> list[Schedule]`: the same sort that `layers` uses, without the date filter. Task 3 uses it for the budget.
  - `allocate(layers: list[Schedule], n: int, ready: dict[str, int] | None = None) -> dict[str, int]`: works per type, omitting zeros.
  - `shares_for(schedules: list[Schedule], day: date) -> dict[str, float]`: today's target percent per type (the cascade with n=100 and no rounding), omitting zeros.
  - `upcoming(schedules: list[Schedule], day: date, days: int = 7) -> list[tuple[date, Schedule]]`: schedules not covering `day` whose next first day falls within `day+1 .. day+days`, with that first day.
  - `next_start(s: Schedule, day: date) -> date`: the first day of the schedule's next run on or after `day` (Task 4 uses it for "N of M").
  - `bars(s: Schedule) -> list[tuple[float, float]]`: `(left%, width%)` pieces on a 365-day year; two pieces when the range wraps.
  - `validate(s: Schedule) -> list[str]`
  - `new_id() -> str`: `secrets.token_hex(4)`.
  - `everyday(mix: dict[str, int]) -> Schedule`: name "Everyday", `"01-01"` to `"12-31"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_schedules.py`. Use the helper `S(start, end, idx=..., **shares)` to build schedules with `id=f"s{idx}"`.

```python
FALL = S("09-01", "12-31", fall=30)
HW = S("10-01", "10-31", halloween=50)
BDAY = S("10-28", "10-28", birthday=30)

def test_cascade_worked_examples():
    assert shares_for([FALL, HW], date(2026, 10, 15)) == {"halloween": 50, "fall": 15}
    got = shares_for([FALL, HW, BDAY], date(2026, 10, 28))
    assert got == pytest.approx({"birthday": 30, "halloween": 35, "fall": 10.5})
    assert allocate(layers([FALL, HW, BDAY], date(2026, 10, 28)), 200) == {
        "birthday": 60, "halloween": 70, "fall": 21}

def test_no_schedule_is_all_originals():
    assert shares_for([HW], date(2026, 3, 1)) == {}

def test_full_share_replaces_everything_below():
    full = S("10-28", "10-28", birthday=100)
    assert allocate(layers([FALL, HW, full], date(2026, 10, 28)), 150) == {"birthday": 150}

def test_shortfall_falls_to_the_next_layer():
    # Birthday wants 60 of 200 but only 10 are ready: Halloween gets 50% of 190.
    ls = layers([FALL, HW, BDAY], date(2026, 10, 28))
    assert allocate(ls, 200, {"birthday": 10, "halloween": 999, "fall": 999}) == {
        "birthday": 10, "halloween": 95, "fall": 28}

def test_same_type_in_two_layers_adds_up():
    a = S("09-01", "12-31", fall=20); b = S("10-01", "10-31", fall=50)
    assert shares_for([a, b], date(2026, 10, 5)) == {"fall": 60}

def test_wrap_around_new_year():
    xmas = S("12-20", "01-05", christmas=40)
    assert covers(xmas, date(2026, 12, 31)) and covers(xmas, date(2027, 1, 1))
    assert not covers(xmas, date(2027, 1, 6))
    assert length(xmas) == 17
    assert bars(xmas) == [pytest.approx((distance to Dec 20 as %, ...)), ...]  # two pieces: Dec 20–31 and Jan 1–5

def test_ties_later_start_then_later_created():
    a = S("10-01", "10-10", idx=0, x=50); b = S("10-05", "10-14", idx=1, y=50)
    assert [s.id for s in layers([a, b], date(2026, 10, 7))] == ["s1", "s0"]
    c = S("10-01", "10-10", idx=2, z=50)
    assert [s.id for s in layers([a, c], date(2026, 10, 7))] == ["s2", "s0"]

def test_leap_day_runs_on_feb_28_in_other_years():
    leap = S("02-29", "02-29", birthday=30)
    assert covers(leap, date(2027, 2, 28)) and covers(leap, date(2028, 2, 29))
    assert not covers(leap, date(2028, 2, 28))

def test_upcoming_within_a_week():
    xmas = S("12-01", "12-26", christmas=60)
    assert upcoming([xmas, HW], date(2026, 11, 24)) == [(date(2026, 12, 1), xmas)]
    assert upcoming([xmas], date(2026, 11, 23)) == []
    assert upcoming([xmas], date(2026, 12, 2)) == []  # running, not upcoming

def test_unknown_types_are_ignored():
    s = S("01-01", "12-31", halloween=30, gone=40)
    assert shares_for(known([s], {"halloween"}), date(2026, 5, 1)) == {"halloween": 30}

def test_validate():
    assert validate(S("10-01", "10-31", x=50)) == []
    assert validate(S("13-01", "10-31", x=50))      # no month 13
    assert validate(S("02-30", "03-01", x=50))      # no Feb 30
    assert validate(S("02-29", "02-29", x=50)) == []
    assert validate(S("10-01", "10-31", x=60, y=50))  # over 100
    assert validate(S("10-01", "10-31", x=-1))
    assert validate(dataclasses.replace(S("10-01", "10-31"), name=" "))  # needs a name
```

Spell the `bars` assertion out with real numbers: Dec 20 is day 354 of 365, so the pieces are `(353/365*100, 12/365*100)` and `(0, 5/365*100)`.

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/test_schedules.py -v`
Expected: FAIL with `ModuleNotFoundError: frame_art.schedules`

- [ ] **Step 3: Implement `src/frame_art/schedules.py`**

The order key is `(length(s), -start_doy(s), -index_in_list)`, ascending, where `start_doy` is the day of the year in a non-leap year. Covering: map both ends to `(month, day)` tuples, turning `(2, 29)` into `(2, 28)` when `day` isn't in a leap year, then compare tuples; wrap when start > end. `allocate` is the one algorithm the tests don't pin down:

```python
rem, out = n, Counter()
for s in layers:
    base = rem
    for t, p in sorted(s.shares.items()):
        if p <= 0:
            continue
        want = min(round(p * base / 100), rem)
        if ready is not None:
            want = min(want, max(0, ready.get(t, 0) - out[t]))
        out[t] += want
        rem -= want
```

`shares_for` runs the same cascade with `n=100` and floats, without rounding. Module docstring: one paragraph naming the "off the top" rule.

- [ ] **Step 4: Run the tests and check they pass**

Run: `uv run pytest tests/test_schedules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/frame_art/schedules.py tests/test_schedules.py
git commit -m "feat: schedules: yearly date ranges combined off the top"
```

---

### Task 2: Settings hold schedules instead of a mix

**Files:**
- Modify: `src/frame_art/settings.py` (remove `mix` and `originals_share`; add `schedules`; update `validate` and `load`)
- Modify: `src/frame_art/web.py` (`settings_page` carry-over at line ~523; replace `save_variant_mix` and its route with `save_variant_limit` at `POST /variants/limit`; the hand-picked-version rule at line ~824)
- Modify: `src/frame_art/service.py` (add `today()`; the hand-generated rule at line ~289; pass today into `jobs.nightly`)
- Modify: `src/frame_art/jobs.py` (`nightly(..., today: date)`), `src/frame_art/cli.py` (`cmd_nightly` passes `date.today()`)
- Modify: `src/frame_art/variants.py` (`nightly` and `progress` take `today: date`; temporarily turn `shares_for` into `assign_slots`' percent input, so behaviour is unchanged for a single year-round schedule until Task 3)
- Modify: `src/frame_art/templates/variants.html` (the Mix section becomes a form holding only "New versions made per night", posting to `/variants/limit`)
- Test: `tests/test_web.py`, `tests/test_variants.py`

**Interfaces:**
- Consumes: Task 1's `Schedule`, `validate`, `everyday`, `known`, `shares_for`.
- Produces:
  - `Settings.schedules: list[Schedule]` (default empty).
  - `Settings.today_shares(day: date, types: set[str]) -> dict[str, float]`, which is `shares_for(known(self.schedules, types), day)`.
  - `Service.today() -> date`, which is `datetime.datetime.now(self.tz).date()`.
  - `variants.nightly(cat, http, data, pub, settings, api_key, today: date) -> dict`
  - `variants.progress(cat, settings, today: date) -> list[dict]`; the row key `"share"` becomes today's target percent (float).
  - `jobs.nightly(cat, http, data, dest, settings, today: date) -> dict`

- [ ] **Step 1: Write the failing tests** (replace `test_mix_validates`, `test_saving_settings_keeps_the_mix` and `test_variants_mix_saves_and_rejects`, and update every other `Settings(mix=...)` in `tests/` to a year-round schedule built with `everyday({...})`):

```python
def test_schedules_validate():
    assert Settings(schedules=[everyday({"halloween": 50})]).validate() == []
    assert Settings(schedules=[everyday({"halloween": 60, "autumn": 50})]).validate()
    assert Settings(variant_limit=51).validate()
    dup = everyday({"halloween": 5})
    assert Settings(schedules=[dup, dup]).validate()  # ids must be unique

def test_old_mix_migrates_to_everyday(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mix": {"halloween": 75}, "count": 5}))
    s = Settings.load(p)
    assert [(x.name, x.start, x.end, x.shares) for x in s.schedules] == [
        ("Everyday", "01-01", "12-31", {"halloween": 75})]
    assert s.count == 5
    p.write_text(json.dumps({"mix": {}}))
    assert Settings.load(p).schedules == []

def test_schedules_round_trip_and_bad_ones_are_dropped(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(schedules=[everyday({"halloween": 50})], count=4)
    s.save(p)
    assert Settings.load(p) == s
    raw = json.loads(p.read_text()); raw["schedules"][0]["start"] = "13-40"
    p.write_text(json.dumps(raw))
    back = Settings.load(p)
    assert back.schedules == [] and back.count == 4

def test_saving_settings_keeps_schedules_and_limit(client):  # as the old test, asserting schedules and variant_limit survive

def test_variant_limit_saves_and_rejects(client):
    # POST /variants/limit {"variant_limit": "5"} -> "Saved", and the limit is 5
    # "x" -> "whole number"; "99" -> "0-50"; the limit is still 5
```

Change `test_nightly_*` and the progress tests to pass `today=date(2026, 10, 28)`. Their expected values stay the same in this task.

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/ -q`
Expected: FAIL (no `schedules` field; `nightly()` missing `today`)

- [ ] **Step 3: Implement**

- **`Settings.load`.** Before the per-field loop, if `"schedules"` isn't in `raw` and `raw.get("mix")` is a non-empty dict of ints, use `[everyday(raw["mix"])]`. Turn a `schedules` value into `Schedule(**d)` for each dict. A `TypeError` or a non-list value is caught by the existing "wrong type" path, so only that field is dropped.
- **`Settings.validate`.** Validate every schedule, then require unique ids.
- **Hand-picked and hand-generated versions** (`service.py:289`, `web.py:824`). Each replaces `s.mix.get(type, 0) > 0` with `type in s.today_shares(svc.today(), known_types)`.
- **The `variants.nightly` stopgap.** Build `mix = settings.today_shares(today, known)` and pass it to the existing percent-based `assign_slots`. Task 3 replaces this.

- [ ] **Step 4: Run all checks**

Run: `uv run pytest -q && uvx ruff@0.16.1 check . && uvx ruff@0.16.1 format --check .`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git commit -am "feat: settings keep schedules; an old mix becomes Everyday"
```

---

### Task 3: The nightly run follows schedules: shortfall and lookahead

**Files:**
- Modify: `src/frame_art/variants.py` (`assign_slots`, `nightly`, `progress`)
- Test: `tests/test_variants.py`

**Interfaces:**
- Consumes: `schedules.layers`, `order`, `allocate`, `upcoming`, `known`; `Settings.schedules`.
- Produces:
  - `assign_slots(live: list[str], target: dict[str, int], current: dict[str, Slot], usable: set[tuple[str, str]], *, ready_only: bool = False) -> dict[str, Slot]`. `target` is a count of works per type. With `ready_only`, non-manual current slots with no usable variant are dropped, and free works are filled only from works with a usable variant of the type. The other rules are unchanged: manual slots survive a shrinking count but not their type dropping out of `target`; droppable slots go variantless first, then oldest; free works are taken with-variant first, then newest.
  - `nightly(...)` returns the same keys as before. `"slots"` now counts the shown slots.

- [ ] **Step 1: Write the failing tests**

- Convert the existing `assign_slots` tests to counts: `{"hw": 50}` on 4 works becomes `{"hw": 2}`, `{"hw": 100}` on 1 work becomes `{"hw": 1}`, and `{}` stays `{}`.
- Add:

```python
def test_ready_only_shows_only_made_versions():
    cur = {"a": Slot("hw", False), "b": Slot("hw", True)}
    got = assign_slots(LIVE, {"hw": 3}, cur, {("c", "hw"), ("b", "hw")}, ready_only=True)
    assert got == {"b": Slot("hw", True), "c": Slot("hw", False)}

def test_nightly_shows_only_ready_versions(vsetup4):
    # year-round halloween 100%, limit 2: two made tonight, both shown, the rest stay originals
    out = variants.nightly(cat, None, data, pub, settings, "k", date(2026, 10, 28))
    assert out["generated"] == ["s1:4", "s0:3"] and out["slots"] == {"halloween": 2}
    assert set(cat.slots()) == {"s1:4", "s0:3"}
    out = variants.nightly(cat, None, data, pub, settings, "k", date(2026, 10, 29))
    assert out["slots"] == {"halloween": 4}  # same works kept, two more made

def test_shortfall_gives_the_rest_to_the_wider_schedule(vsetup4):
    # Two types: a "birthday" type added to the fixture; Halloween versions already made for all four works.
    # Schedules: Everyday halloween 100%, Oct 28 birthday 50%. No birthday versions, limit 0.
    # On Oct 28 all four still show Halloween; nothing reverts to the original.

def test_lookahead_makes_but_does_not_show(vsetup4):
    # Only a birthday schedule on Nov 2 at 50%, limit 2, today Oct 28.
    out = variants.nightly(cat, None, data, pub, settings, "k", date(2026, 10, 28))
    assert len(out["generated"]) == 2 and out["slots"] == {} and cat.slots() == {}
    out = variants.nightly(cat, None, data, pub, settings, "k", date(2026, 11, 2))
    assert out["slots"] == {"birthday": 2} and out["generated"] == []

def test_lookahead_keeps_making_for_the_same_works(vsetup4):
    # Same setup as above (birthday Nov 2 at 50% of 4 works = 2, limit 2).
    # Oct 28 makes s1:4 and s0:3; on Oct 29 the plan keeps those two (they have versions),
    # so Oct 29 makes nothing: out["generated"] == [] and no new variant rows exist.

def test_budget_goes_to_the_narrowest_schedule_first(vsetup4):
    # Running: Everyday halloween 100%. Upcoming within 7 days: a one-day birthday at 50%. Limit 2, nothing made yet.
    # Both nights' versions are birthday ones (2 works), not Halloween.
```

Give the commented tests full bodies in the same style as `test_nightly_generates_up_to_the_limit_and_swaps`. The fixture gains `cat.add_variant_type("birthday", "Birthday", "make it a birthday")` where needed, and its settings become `Settings(schedules=[everyday({"halloween": 100})], variant_limit=2)`. Delete or update the tests that expected uncapped slots (`test_nightly_generates_up_to_the_limit_and_swaps`, which now expects 2 shown).

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/test_variants.py -v`
Expected: FAIL (`ready_only` unknown; slots not capped)

- [ ] **Step 3: Implement `nightly` in this order**

1. Set `types = {t.slug for t in cat.variant_types()}`, `sch = known(settings.schedules, types)`, `live = _live(cat)` and `usable = set(cat.usable_variants())`.
2. **Plans (not stored).** `today_plan = assign_slots(live, allocate(layers(sch, today), len(live)), cat.slots(), usable)`. For each `(d, s)` in `upcoming(sch, today)`, `plan = assign_slots(live, allocate(layers(sch, d), len(live)), {}, usable)`.
3. **Budget.** `pairs` = `needed(plan, states, live)` for each plan, deduplicated in order. Then stable-sort them by the rank of their type: the index of the first schedule in `order(running + upcoming schedules)` whose shares include the type. Take `[: settings.variant_limit]`.
4. Generate, as today.
5. **Shown.** Recompute `usable` and `ready = Counter(t for k, t in usable if k in live)`. Then `shown = assign_slots(live, allocate(layers(sch, today), len(live), ready), cat.slots(), usable, ready_only=True)`, `cat.set_slots(shown)`, then `apply_versions`.

In `progress`:
- `"slotted"` and `"ready"` count against today's uncapped plan, computed the same way as step 2 without storing it.
- `"needed"` is the uncapped plan's `needed` count.

- [ ] **Step 4: Run all checks**

Run: `uv run pytest -q && uvx ruff@0.16.1 check . && uvx ruff@0.16.1 format --check .`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git commit -am "feat: nightly follows schedules: shows only made versions, looks a week ahead"
```

---

### Task 4: The Seasonal page: timeline, today's screen, schedule editor

**Files:**
- Modify: `src/frame_art/web.py` (`variants_view` and new handlers `add_schedule`, `save_schedule`, `delete_schedule`; routes)
- Modify: `src/frame_art/templates/variants.html`
- Modify: `src/frame_art/static/style.css` (a "/* Schedules */" block)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `schedules.bars`, `length`, `layers`, `shares_for`, `allocate`, `next_start`, `covers`, `validate`, `new_id`; `Settings.schedules`; `variants.progress(cat, s, today)`.
- Produces routes:
  - `GET /variants?edit=<id>` opens that schedule's editor, and `?edit=new` opens a blank one.
  - `POST /variants/schedules` adds a schedule.
  - `POST /variants/schedules/{id}` saves one.
  - `POST /variants/schedules/{id}/delete` deletes one.
  - An unknown id returns 404.
- Form fields:
  - `name`, `start_month` (1–12), `start_day`, `end_month`, `end_day`;
  - one `share-<slug>` per type row in the editor. Only the rows present are saved, so a removed row means the type leaves the schedule.

- [ ] **Step 1: Write the failing tests**

```python
def test_schedule_add_edit_delete(client):
    # add "Halloween", Oct 1–31, share-halloween=50 -> 200 with "Saved"; settings hold it
    # GET /variants: the timeline has a bar labelled "Halloween · 50%" linking to ?edit=<id>
    # edit to share-halloween=60 and name "Spooky" -> saved
    # delete -> gone

def test_schedule_editor_rejects_and_shows_what_was_typed(client):
    # share-halloween=x -> 400, "not a whole number", the page still shows "x" and the typed name
    # start_month=2, start_day=30 -> 400, and nothing is saved
    # two shares summing to 110 -> 400

def test_schedule_unknown_id_is_404(client):
    # POST /variants/schedules/nope and /nope/delete -> 404

def test_editor_lists_only_types_not_in_the_schedule(client):
    # types halloween and fall; schedule has halloween only: the editor has a row for halloween
    # and the "+ Add a type" list offers fall, not halloween

def test_today_strip_and_deleted_type(client):
    # a schedule covering today with halloween 50 and a share for a deleted type "gone"
    # GET /variants -> 200; the today strip names Halloween with a 50% target; "gone" appears nowhere

def test_wrapping_schedule_draws_two_bars(client):
    # Dec 20 – Jan 5: two bar pieces with the same edit link
```

Give each one a full body in the style of the existing `test_variant_types_add_and_edit`. To test "today", monkeypatch `svc.today` to return `date(2026, 10, 28)`.

- [ ] **Step 2: Run the tests and check they fail**

Run: `uv run pytest tests/test_web.py -k schedule -v`
Expected: FAIL (routes missing)

- [ ] **Step 3: Implement**

Page order and copy as in the spec's "Web UI" section:
- **Timeline.** One row per schedule, widest (longest) at the top. Each bar is an `<a>` to `?edit=<id>`, absolutely positioned using the `bars()` percentages. A one-day bar puts its label beside it. A marker shows today.
- **Today's screen strip.** One segment per type plus originals, using the `live_share` figures from `progress`, with today's target from `shares_for` next to each.
- **"+ Add schedule".** A link to `?edit=new`.
- **The limit form** from Task 2.
- **The editor,** when `edit` is set:
  - name, the month selects and day inputs, and "every year";
  - one row per type in the schedule: share input, progress bar, "N of M ready", and a × button that removes the row;
  - a `<select>` "+ Add a type" listing the remaining types. Its inline script clones a hidden `<template>` row for the chosen type, removes the type from the select, and updates the readout;
  - the readout, Save, and Delete schedule (a separate form posting to `/delete`).
- **N of M.**
  - Use the schedule's day `D`: today if `covers(s, today)`, otherwise `next_start(s, today)`.
  - M is `allocate(layers(sch, D), n_live)[type]`.
  - N is `min(M, ready works of that type)`.
- **Progress, Types and Generate for…** stay. The Progress "Wanted" column shows today's target from Task 2.

On a rejected form, re-render with the typed values and keep the editor open. This is the same pattern as `variants_view(mix=...)` today; replace that parameter with `draft: Schedule | None`.

- [ ] **Step 4: Check the page in a browser, then run all checks**

1. Run the scratch demo server bound to 192.168.1.21 (not localhost), with schedules for Everyday, Fall, Halloween, a birthday and Christmas (Dec 1–26). Check the page at desktop and phone width (390px): nothing overflows, and the one-day bar and wrapped bars are readable.
2. Run: `uv run pytest -q && uvx ruff@0.16.1 check . && uvx ruff@0.16.1 format --check .`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git commit -am "feat: Seasonal page: schedule timeline, today's screen and editor"
```
