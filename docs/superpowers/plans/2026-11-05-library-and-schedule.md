# Library and Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the collection, catalogue picks, photo share, Portraits share and seasonal slots with one model: Works → Library → On the TV, mixed by tag-based Schedule slices, published by one publisher.

**Architecture:** New side tables keep the old ones intact. `library.py` builds one read model (`Item`) over museum works, Brian's photos, Studio portraits and AI versions, with group-qualified tags. `mix.py` is a pure planner that turns schedules, eligible items and the live set into retire/fill decisions. `publish.Publisher` executes that plan for every kind of item. `variants.py` keeps nightly version making, driven by `ai-variant` slices. The web UI gets Library, Schedule and Version styles pages; old URLs redirect.

**Tech Stack:** Python 3.13, Starlette + Jinja2, SQLite, Pillow, pytest, ruff 0.16.1, uv.

**Spec:** `docs/superpowers/specs/2026-11-05-library-and-schedule-design.md`

## Global Constraints

- Work on a new branch `library` in a new worktree `.worktrees/library`, cut from `main` **after** 0.11.0 (photo sources + Studio) is merged and tagged. Do not start before `git tag --list v0.11.0` prints `v0.11.0`.
- Run tests with `uv run pytest -q`; lint with `uv run ruff check . && uv run ruff format --check .`. Both must pass at every commit.
- Never drop or rewrite an old table or column. Older releases must still start against the database: new state lives in new tables (`create table if not exists`).
- **No HA config change.** Same `full/` and `matted/` folders, same MQTT messages, same `pending()` output shape.
- The TV draws only from the Library: an item outside the Library, or Removed, is never published and is retired at the next run or `sync`.
- A removed work never comes back on its own.
- An original and any of its versions are never live together; two versions of one original are never live together.
- Tags are stored lowercased as `group:value`. Groups: `artist`, `style`, `genre`, `place`, `subject` (shown together as "Subject and style"), `kind`, `source`, `ai-variant`, `yours`. `ai-variant:christmas` is displayed as `ai-variant-christmas`. A plain `subject:christmas` never matches `ai-variant:christmas`.
- Item keys: museum `<source>:<id>` (unchanged), photo `photo:<photo id>`, portrait `portrait:<painting id>` (unchanged), version `variant:<variants.id>`.
- A version is published under its original's key with a `published_variants` row (as today), and read back as `variant:<id>` by `library.live`. Never rewrite `published` rows.
- Settings dropped (accepted and ignored on load): `choose_from`, `photo_share`, `photo_crop`, `allow_nudes`, `full_screen_only`, `repeat_days`. `variant_limit` stays. `kinds` stays **as a crawl setting only** (see Ruling R1).
- Commits use conventional-commit prefixes and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Pages are LAN-only; nothing new is served under `/config/www`. Photos never leave the LAN.

Rulings made while planning (the executor carries these; they go to Brian in the handoff):

- **R1** `kinds` stays as "Kinds to crawl". The spec drops it as a publishing filter, but the crawl uses it today (`Settings.crawl_kinds`), and dropping it would crawl drawings and prints for every source.
- **R2** A slice's shortfall cascades exactly like today's `schedules.allocate`: an unfilled share falls to the next wider schedule and then to Rest of the Library. The spec says "falls through"; this is the existing behaviour.
- **R3** Old non-current versions migrate as **Removed** (the only non-eligible state); Brian can Put back. The spec said "kept but not eligible".
- **R4** Overview stays as the first nav item (the spec's nav list omits it, and it is the home page).
- **R5** Photos, portraits and versions of either always go to `full/`, padded with `blur` when the pad style is `matte` (today's portrait rule). Museum works and versions of museum works use `pipeline.folder_for` as today.
- **R6** Brian's photos and portraits render at the `crop` setting with `full_screen_only=False`, `allow_nudes=True`.

## Review Focus

1. **Rollback:** after this release runs, an older release (0.11.0) still starts and publishes against the same DB; `photo:` keys in `published` don't crash it. Test: `test_old_schema_reads_still_work` (Task 1).
2. **Removing an original while its version is live** retires the version too (its original left the Library). Test: `test_version_of_removed_original_retires` (Task 6).
3. **A slice whose only members are already live elsewhere** (e.g. favorites slice when every favorite is live under Rest) must not double-publish one root. Test: `test_one_root_live_once_across_slices` (Task 5).
4. **Library smaller than the pool**: pool shrinks to the Library, no error, nothing outside the Library fills it. Test: `test_pool_shrinks_to_library` (Task 5).
5. **First nightly run after upgrade** retires no more than rotation would. Test: `test_first_run_after_upgrade_does_not_churn` (Task 9).

---

## File structure

| File | Responsibility |
|---|---|
| `src/frame_art/catalogue.py` (modify) | New tables: `work_added`, `source_crawls`, `library`, `user_tags`; their accessors. `published()` loses its portrait filter. |
| `src/frame_art/library.py` (create) | `Item` read model, tags, eligibility, live mapping, membership actions, source images. |
| `src/frame_art/schedules.py` (modify) | `Slice`; schedules hold slices; flattening, cascade, today's slices. |
| `src/frame_art/settings.py` (modify) | Drop settings; migrate old `shares` and `photo_share` into slices on load. |
| `src/frame_art/mix.py` (create) | Pure planner: slice membership, targets, rotation, retire/fill. |
| `src/frame_art/publish.py` (modify) | One `Publisher` that renders and publishes any `Item` and executes a `mix.Plan`. |
| `src/frame_art/variants.py` (modify) | Version making for `ai-variant` slices (nightly, make-now, by hand, regenerate). Slots and swaps removed. |
| `src/frame_art/portraits.py` (delete) | Folded into the publisher. |
| `src/frame_art/migrate.py` (create) | One-time 0.12.0 upgrade. |
| `src/frame_art/jobs.py`, `service.py`, `cli.py` (modify) | Wiring. |
| `src/frame_art/web.py` + templates (modify/create) | Library, Schedule, Version styles pages; nav; redirects. |

---

### Task 1: Dates, sources and Library tables in the catalogue

**Files:**
- Modify: `src/frame_art/catalogue.py` (SCHEMA, `upsert`, `collect`, new methods)
- Modify: `src/frame_art/jobs.py` (`crawl`, `last_crawl`)
- Test: `tests/test_library_tables.py` (create)

**Interfaces:**
- Produces (on `Catalogue`):
  - `work_added() -> dict[str, float]`
  - `record_crawl(source: str, at: float | None = None) -> None`; `source_crawls() -> dict[str, float]`
  - `library() -> dict[str, tuple[str, float]]` — key → (`"in"` | `"removed"`, added_at)
  - `set_library(keys: Iterable[str], state: str | None) -> None` — `"in"`, `"removed"`, or `None` (Not in)
  - `user_tags() -> dict[str, set[str]]`; `set_tag(keys: Iterable[str], tag: str, on: bool) -> None`
  - `collect(key, on)` keeps its signature but no longer touches overrides (starring is independent of membership).
- Produces (in `jobs`): `last_crawl(cat) -> float` reads `source_crawls`, falling back to `max(works.crawled_at)` when empty.

New tables (append to SCHEMA):

```sql
-- 0.12.0: when a work first entered Works; set once, a re-crawl never changes it.
create table if not exists work_added (key text primary key, added_at real not null);
-- 0.12.0: when each source was last crawled.
create table if not exists source_crawls (source text primary key, crawled_at real not null);
-- 0.12.0: Library membership. A missing row means "not in the Library".
create table if not exists library (
    key text primary key,
    state text not null check (state in ('in', 'removed')),
    added_at real not null
);
-- 0.12.0: Brian's own tags, stored without the "yours:" group.
create table if not exists user_tags (key text not null, tag text not null, primary key (key, tag));
```

- [ ] **Step 1: Write the failing tests**

```python
def test_added_is_set_once(tmp_path, monkeypatch):
    cat = Catalogue(tmp_path / "db.sqlite")
    monkeypatch.setattr(time, "time", lambda: 100.0)
    cat.upsert([make_work(1)])
    monkeypatch.setattr(time, "time", lambda: 200.0)
    cat.upsert([make_work(1), make_work(2)])
    assert cat.work_added() == {"s1:1": 100.0, "s2:2": 200.0}

def test_crawl_times_are_per_source(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.record_crawl("met", 10.0); cat.record_crawl("nga", 20.0); cat.record_crawl("met", 30.0)
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
    cat.set_library(["a:1"], "in")            # already in: date kept
    cat.set_library(["photo:x"], "removed")
    assert cat.library() == {"a:1": ("in", 1.0), "photo:x": ("removed", 2.0)}
    monkeypatch.setattr(time, "time", lambda: 3.0)
    cat.set_library(["photo:x"], "in")        # put back: date reset
    cat.set_library(["a:1"], None)
    assert cat.library() == {"photo:x": ("in", 3.0)}

def test_removing_a_museum_work_also_bans_it_for_older_releases(tmp_path):
    cat = Catalogue(tmp_path / "db.sqlite")
    cat.collect("a:1")
    cat.set_library(["a:1", "photo:x"], "removed")
    assert cat.overrides() == {"a:1": "banned"}   # photo keys are not mirrored
    assert "a:1" in cat.collection()              # the star survives
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
    cat.db.execute("insert into published values ('photo-x.jpg','full','photo:x','00',1,null,null)")
    assert cat.db.execute("select count(*) from works").fetchone() == (1,)
```

(`make_work(i)` as in `tests/test_portraits.py`, with `source=f"s{i}"`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_library_tables.py -q`
Expected: FAIL (`AttributeError: 'Catalogue' object has no attribute 'work_added'`).

- [ ] **Step 3: Implement**

`upsert` adds `insert or ignore into work_added values (?, ?)` per row inside the same transaction. `set_library(..., "removed")` mirrors museum keys (keys whose prefix is not `photo:`, `portrait:` or `variant:`) into `overrides` as `banned` with a direct insert (not `set_override`, which unstars); `"in"`/`None` deletes a `banned` override for those keys. `set_tag` lowercases and strips the tag. `jobs.crawl` calls `cat.record_crawl(src.name)` after each adapter's upsert.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: all pass (existing `test_ban_and_collect_exclude_each_other` changes: collecting no longer clears a ban; update that test to assert the new behaviour).

- [ ] **Step 5: Commit** — `feat: record when works arrive, per-source crawl times and Library membership`

---

### Task 2: The Item read model and tags

**Files:**
- Create: `src/frame_art/library.py`
- Test: `tests/test_library.py` (create)

**Interfaces:**
- Consumes: Task 1 accessors; `PhotoStore.all()`, `StudioStore.paintings()`, `Catalogue.variants()`, `Catalogue.rejected_keys()`.
- Produces:

```python
PHOTO, PORTRAIT, VARIANT = "photo:", "portrait:", "variant:"

@dataclass(frozen=True)
class Item:
    key: str
    kind: str                 # "museum" | "photo" | "portrait" | "version"
    title: str
    added_at: float
    tags: frozenset[str]      # group-qualified, lowercased
    root: str                 # own key; a version's original's key
    vtype: str | None = None  # a version's type slug
    variant_id: int | None = None
    work: Work | None = None  # museum works, and versions of museum works (the original's Work)
    path: str | None = None   # file for photos, portraits and versions

@dataclass(frozen=True)
class Live:
    filename: str
    folder: str
    key: str          # mapped: "variant:<id>" for a version file
    root: str
    published_at: float

def work_tags(w: Work) -> set[str]
def items(cat: Catalogue, photos: PhotoStore, studio: StudioStore) -> dict[str, Item]
def state(lib: dict[str, tuple[str, float]], key: str) -> str      # "in" | "removed" | "out"
def eligible(cat: Catalogue, all_items: dict[str, Item]) -> list[Item]
def live(cat: Catalogue, all_items: dict[str, Item]) -> list[Live]  # oldest published first
def matches(item: Item, tags: Iterable[str]) -> bool
def display_tag(tag: str) -> str     # "ai-variant:christmas" -> "ai-variant-christmas"; others -> value
```

Tag rules (`work_tags` and `items`):
- Museum: `artist:`, `style:`, `genre:`, `place:` from the fields, `subject:` per subject, `kind:<w.kind>`, `source:<w.source>`.
- Photo: `kind:<photo.kind>` when set, `source:photos`. Title is the photo's original filename. `added_at = photo.added`.
- Portrait: `source:studio`, `kind:painting`, `artist:<painter name>`. Title `"Portrait by <painter>"`. `added_at = saved_at`.
- Version: the original's tags minus every `yours:*`, plus `ai-variant:<type>`. Title `"<original title> (<Type name>)"`. `added_at = created_at`. Versions of a missing original are skipped. Versions are never made of versions, so `root` is the original's key.
- Every item adds `yours:<t>` for its own user tags and `yours:favorite` if starred (`cat.collection()`).
- Museum `added_at` comes from `work_added`, falling back to `0.0`.

Eligibility (`eligible`): not in `cat.rejected_keys()`, and either a non-version with state `"in"`, or a version whose own state is not `"removed"` and whose `root` has state `"in"`.

`live`: every `cat.published(active=True)` row (all key prefixes), mapped with `published_variants` to `variant:<id>`; `root` is the published key.

- [ ] **Step 1: Write the failing tests**

```python
def test_museum_tags(): assert work_tags(monet) >= {"artist:claude monet", "kind:painting", "source:nga", "subject:haystacks"}
def test_version_inherits_tags_but_not_yours(setup):  # starred original, user tag "mine"
    v = its["variant:1"]
    assert "ai-variant:halloween" in v.tags and "artist:claude monet" in v.tags
    assert not any(t.startswith("yours:") for t in v.tags) and v.root == "nga:1"
def test_photo_and_portrait_items(setup):
    assert its["photo:" + pid].tags >= {"source:photos", "kind:scene"}
    assert its["portrait:1"].tags >= {"source:studio", "kind:painting", "artist:edward hopper"}
def test_eligibility(setup):
    cat.set_library(["nga:1"], "in"); cat.set_library(["variant:2"], "removed")
    keys = {i.key for i in eligible(cat, its)}
    assert "nga:1" in keys and "variant:1" in keys and "variant:2" not in keys
    cat.set_library(["nga:1"], "removed")
    assert not {"nga:1", "variant:1"} & {i.key for i in eligible(cat, items(cat, ph, st))}
def test_rejected_is_not_eligible(setup): ...
def test_live_maps_version_files(setup):
    cat.record_publish("x--halloween-1.jpg", "full", "nga:1", 0, variant=1)
    assert [(l.key, l.root) for l in live(cat, its)] == [("variant:1", "nga:1")]
def test_display_tag(): assert display_tag("ai-variant:christmas") == "ai-variant-christmas"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_library.py -q` — Expected: FAIL (`ModuleNotFoundError: frame_art.library`).
- [ ] **Step 3: Implement `library.py`** as specified.
- [ ] **Step 4: Run** `uv run pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** — `feat: one read model for museum works, photos, portraits and versions`

---

### Task 3: Library actions

**Files:**
- Modify: `src/frame_art/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `add(cat, keys: Iterable[str]) -> None` — state `"in"`; a version key is put back instead (state cleared to eligible: delete its `removed` row).
  - `remove(cat, all_items: dict[str, Item], keys: Iterable[str], with_versions: bool = True) -> list[str]` — returns every key removed. For an original with `with_versions`, also its versions.
  - `put_back(cat, keys) -> None` — removed → `"in"` (originals) or row deleted (versions). Never brings an original's versions back.
  - `source_image(item: Item, http: Http, data: Path, crop: str) -> Image.Image` — museum: `variants.load_source` behaviour (moved here as `load_source`); photo: `photos.open_image`; portrait/version: `imaging.load(Path(item.path).read_bytes())`.
  - Moves `variants.source_path` and `variants.load_source` into `library.py` (variants imports them).

- [ ] **Step 1: Failing tests**: `test_remove_original_removes_versions_by_default`, `test_remove_original_can_keep_versions`, `test_put_back_original_leaves_versions_removed`, `test_adding_a_version_puts_it_back`, `test_source_image_for_photo_and_portrait`.
- [ ] **Step 2: Run** — FAIL (`AttributeError: module 'frame_art.library' has no attribute 'remove'`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Commit** — `feat: add, remove and put back Library works`

---

### Task 4: Schedules with tag slices; settings trimmed

**Files:**
- Modify: `src/frame_art/schedules.py`, `src/frame_art/settings.py`
- Test: `tests/test_schedules.py`, `tests/test_settings.py` (create if missing; else extend)

**Interfaces:**
- Produces in `schedules`:

```python
@dataclass
class Slice:
    tags: list[str]   # group-qualified; all must match
    share: int        # percent

@dataclass
class Schedule:
    id: str; name: str; start: str; end: str
    slices: list[Slice] = field(default_factory=list)
    def total(self) -> int

def flatten(layers: list[Schedule]) -> list[Slice]                  # layer order, then slice order
def allocate(layers: list[Schedule], n: int, available: list[int] | None = None) -> list[int]
    # one count per flattened slice; same cascade as today's allocate; with `available`,
    # a slice gets at most available[i] and the rest falls to the next layer down
def shares_for(schedules: list[Schedule], day: date) -> list[tuple[Slice, float]]  # cascade on 100
def from_dict(d: dict) -> Schedule   # accepts old {"shares": {...}} and new {"slices": [...]}
def everyday(slices: list[Slice]) -> Schedule
```

- `validate` checks each slice: non-empty tag list, every tag matches `^[a-z0-9-]+:.+$`, share an int 0–100; total ≤ 100.
- Old shares convert in `from_dict`: `{"portraits": p}` → `Slice(["source:studio"], p)`; any other `{t: p}` → `Slice([f"ai-variant:{t}"], p)`; slices ordered by type name (today's `sorted(shares)` order).
- `PORTRAITS`, `known`, and the dict-based `allocate` go away.
- Settings: remove the six dropped fields and `photo_prefs`, `shown_kinds`, `today_shares`, `CHOOSE_FROM`. `prefs()` returns `Prefs(CROP_LEVELS[crop], full_screen_only=False, allow_nudes=True)`. `kinds` stays (R1), labelled "Kinds to crawl" in the UI. `load` builds schedules with `from_dict`, and if the raw file has `photo_share > 0`, appends `Slice(["kind:photograph"], photo_share)` to the schedule spanning `01-01`–`12-31` named Everyday (creating `everyday([...])` with id `"everyday"` if none). It does this only when the raw file has no `"slices"` in any schedule (i.e. once).

- [ ] **Step 1: Failing tests** (port the existing schedule tests from `shares` dicts to slices, then add):

```python
def test_old_shares_become_slices():
    s = from_dict({"id": "x", "name": "H", "start": "10-01", "end": "10-31",
                   "shares": {"halloween": 60, "portraits": 10}})
    assert [(sl.tags, sl.share) for sl in s.slices] == [(["ai-variant:halloween"], 60), (["source:studio"], 10)]

def test_allocate_caps_by_available_and_falls_through():
    narrow = Schedule("a", "Xmas", "12-01", "12-31", [Slice(["ai-variant:christmas"], 40)])
    wide = everyday([Slice(["kind:photograph"], 50)])
    assert allocate([narrow, wide], 100, available=[10, 100]) == [10, 45]

def test_photo_share_moves_into_everyday(tmp_path):
    p = tmp_path / "s.json"; p.write_text(json.dumps({"photo_share": 20, "choose_from": "collection"}))
    s = Settings.load(p)
    assert [(sl.tags, sl.share) for sl in s.schedules[0].slices] == [(["kind:photograph"], 20)]
    assert not hasattr(s, "photo_share")

def test_dropped_settings_are_ignored(tmp_path): ...   # every dropped key present → loads, others kept
def test_slice_tags_must_be_grouped(): assert validate(Schedule("a","A","01-01","01-31",[Slice(["christmas"], 10)]))
```

- [ ] **Step 2: Run** `uv run pytest tests/test_schedules.py tests/test_settings.py -q` — FAIL.
- [ ] **Step 3: Implement.** Other modules that import removed names (`variants`, `jobs`, `web`) will break; keep them importable by deleting the dead call sites only where needed to import, marked `# replaced in Task 6/7/11` — Tasks 6–12 rewrite them. The full suite is allowed to have failures **only** in `tests/test_variants.py`, `tests/test_portraits.py`, `tests/test_publish.py` and `tests/test_web.py` until those tasks; list them in the ledger.
- [ ] **Step 4: Run** `uv run pytest tests/test_schedules.py tests/test_settings.py -q` — pass.
- [ ] **Step 5: Commit** — `feat!: schedules hold tag slices; drop choose_from, photo share and filter settings`

---

### Task 5: The mix planner

**Files:**
- Create: `src/frame_art/mix.py`
- Test: `tests/test_mix.py`

**Interfaces:**
- Consumes: `library.Item`, `library.Live`, `schedules.Schedule`, `schedules.flatten`, `schedules.allocate`.
- Produces:

```python
REST: frozenset[str] = frozenset()

def member(item: Item, slices: list[frozenset[str]]) -> int | None
    # first slice whose tags ⊆ item.tags; a version only matches a slice containing its
    # "ai-variant:<type>" tag; non-versions fall to len(slices) (Rest); versions to None

@dataclass
class Plan:
    slices: list[frozenset[str]]   # flattened, Rest last
    targets: list[int]
    retire: list[str]              # live filenames to retire, in order
    fill: list[list[str]]          # per slice: waiting item keys, best first

def plan(layers: list[Schedule], items: list[Item], live: list[Live], pool: int, count: int,
         pinned: set[str], favorites: set[str], last_shown: dict[str, float],
         now: float, seed: int) -> Plan
def spread(count: int, targets: list[int]) -> list[int]   # largest-remainder split of count by target
```

Algorithm (`plan`), `items` being the eligible items:
1. Live files whose mapped key isn't an eligible item retire first (pins don't save them).
2. `P = min(pool, number of distinct roots among items)`.
3. Members per slice via `member`; `available[i]` = distinct roots among slice i's members. `targets = allocate(layers, P, available[:-1])`, Rest gets `min(P - sum, available[-1])`.
4. Per slice: live members `L` (oldest published first). Retire `len(L) - target` oldest unpinned. Then retire `r = min(spread(count, targets)[i], len(waiting))` more oldest unpinned, where `waiting` = members not live whose root isn't live **in this slice**.
5. `fill[i]` = waiting keys: pinned first; then never-shown keys, round-robin across groups (the item's first `artist:` tag, else its `source:` tag), groups shuffled by `random.Random(seed)`; then the rest by effective age descending, where effective age = `(now - last_shown[key]) * (2 if favorite else 1)`.

Keys in `pinned`/`favorites`/`last_shown` are item keys (`variant:<id>` for versions).

- [ ] **Step 1: Failing tests** (helper `it(key, *tags, kind="museum", root=None)` builds an `Item`; `lv(key, at, root=None)` builds a `Live`):

```python
def test_rest_takes_what_slices_leave():
    p = plan([everyday([Slice(["kind:photograph"], 20)])], ten_paintings + five_photos, [], pool=10, count=7, ...)
    assert p.targets == [2, 8]

def test_shortfall_goes_to_rest():               # photo slice wants 5, has 2 → [2, 8]
def test_pool_shrinks_to_library():              # 4 items, pool 150 → sum(targets) == 4
def test_versions_only_via_their_slice():
    assert member(version_xmas, [frozenset({"kind:painting"})]) is None
    assert member(version_xmas, [frozenset({"ai-variant:christmas"})]) == 0
def test_first_matching_slice_counts():          # item matching slices 0 and 1 → 0
def test_removed_live_work_retires_even_if_pinned():
def test_rotation_retires_about_count_oldest():  # 20 live, 20 waiting, count 7 → 7 retired, oldest first
def test_rotation_keeps_pins():
def test_no_waiting_no_rotation():
def test_never_shown_first_then_least_recent():
def test_favorite_comes_round_twice_as_often():  # fav shown 10 days ago beats plain shown 15 days ago
def test_one_root_live_once_across_slices():     # original live in Rest; its xmas version waits in slice 0 → in fill[0]; original not in fill[Rest]
def test_spread():
    assert spread(7, [50, 50]) == [4, 3] or spread(7, [50, 50]) == [3, 4]
    assert sum(spread(7, [3, 140, 7])) == 7
```

- [ ] **Step 2: Run** `uv run pytest tests/test_mix.py -q` — FAIL (no module).
- [ ] **Step 3: Implement `mix.py`.**
- [ ] **Step 4: Run** `uv run pytest tests/test_mix.py -q` — pass.
- [ ] **Step 5: Commit** — `feat: plan the TV's mix from Library slices`

---

### Task 6: One publisher for every kind of item

**Files:**
- Modify: `src/frame_art/publish.py`, `src/frame_art/catalogue.py` (`published`)
- Delete: `src/frame_art/portraits.py`, `tests/test_portraits.py` (port its relevant cases)
- Test: `tests/test_publish.py` (rewrite against the new API)

**Interfaces:**
- Consumes: Tasks 2, 5.
- Produces:

```python
class Publisher:
    def __init__(self, cat: Catalogue, http: Http | None, dest: Path, pad_style: str,
                 crop: str, photos: PhotoStore, studio: StudioStore, data: Path)
    def run(self, layers: list[Schedule], pool: int, count: int, seed: int,
            now: float | None = None) -> PublishResult
    def sync(self) -> list[str]          # retire live files that are no longer eligible; adds nothing
    def publish_item(self, item: Item) -> str   # "<folder>/<filename>"; raises pipeline.Rejected etc.
```

- `Catalogue.published(active=None)` drops the `portraits` parameter and returns every key; update callers (`pending`, `content_index`, history, now showing).
- `run`: build items, eligible, live (`library`), pins (`overrides` `pinned`, mapped keys), favorites (`collection`), `last_shown` per item key from all `published` rows (max of `retired_at or published_at`, mapped through `published_variants`); call `mix.plan`; retire `plan.retire` via `_retire_files`; then fill slice by slice in order, taking keys from `fill[i]` until the slice's live count reaches its target. Track live roots: skip a candidate whose root is live **unless** the candidate is a version and the live file of that root is its original, in which case retire the original first (it is refilled when its slice comes up; Rest is last). `_write_meta` at the end.
- Rendering (`publish_item`):
  - museum: `pipeline.exclusion(w, prefs)`; a string → `cat.record_reject(key, reason)` and raise `pipeline.Rejected`; else `pipeline.render`. Duplicate-phash check against live museum hashes as today. Filename `pipeline.filename(w)`. Folder `pipeline.folder_for`.
  - photo / portrait / version: `library.source_image` → `pipeline.render_image(work, im, prefs, style)` with `work` = the item's `Work` or a stand-in `Work(source="photo"|"portrait", source_id=…, title=item.title, artist=None, date=None, kind="photograph"|"painting", license="own")`. Folders per R5. Filenames `photo-<id>.jpg`, `portrait-<id>.jpg`, version `<original stem>--<type>-<n>.jpg` (original stem: museum `pipeline.filename`, else `photo-<id>`/`portrait-<id>`). All through `cat.unused_filename`.
  - `record_publish` key: museum key; photo `photo:<id>`; portrait `portrait:<id>`; a version under its root with `variant=<id>`.
  - Render failures: `Rejected` → recorded reject, skip; `NotFullScreen` can't happen (prefs); other exceptions → `failed`, skip.
- `swap`, `prefs_for`, `_add`, `_take`, `diversify` go (diversify's job is now `mix`'s round-robin). `COOLDOWN` and `DAY` go.

- [ ] **Step 1: Failing tests** (fake `pipeline.render`/`render_image` as in today's tests):

```python
def test_run_fills_the_pool_from_the_library(setup)        # 5 in Library, 3 not → only the 5 go up
def test_nothing_outside_the_library(setup)                 # empty Library → nothing published
def test_photo_and_portrait_publish_under_their_keys(setup) # keys photo:<id>, portrait:<id>; files in full/
def test_version_replaces_its_live_original(setup)          # Christmas 100% slice: original file retired, version file live under original key + published_variants
def test_version_of_removed_original_retires(setup)
def test_sync_retires_removed_and_deleted(setup)            # removed museum work; deleted photo
def test_rejected_work_is_recorded_and_skipped(setup)
def test_pending_lists_all_kinds(setup)                     # to_upload includes photo/portrait files
def test_pinned_stays_live_past_rotation(setup)
def test_matte_style_puts_padded_museum_works_in_matted(setup)  # kept from today
def test_retire_reads_sidecar_and_lists_content_ids(setup)      # kept from today
```

- [ ] **Step 2: Run** `uv run pytest tests/test_publish.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_publish.py tests/test_mix.py tests/test_library.py -q` — pass.
- [ ] **Step 5: Commit** — `feat!: one publisher for museum works, photos, portraits and versions`

---

### Task 7: Seasonal versions for ai-variant slices

**Files:**
- Modify: `src/frame_art/variants.py`
- Test: `tests/test_variants.py` (rewrite), `tests/test_variants_catalogue.py` (keep)

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:

```python
NIGHTLY_FAILURES = 3                          # unchanged
def variant_path(data: Path, original_key: str, type: str, n: int) -> Path
    # data/variants/<type>/<pipeline.slug(original_key)>/<n>.jpg
def generate(cat, http, data, provider, original: Item, type: str, crop: str,
             prompt: str | None = None) -> Variant
def needed(cat, all_items: dict[str, Item], schedules: list[Schedule], pool: int,
           days: list[date], type: str | None = None) -> list[tuple[str, str]]
    # (original key, type) to make, narrowest schedule first, deduplicated
def nightly(cat, http, data, all_items, settings, api_key: str | None, today: date) -> dict
    # makes up to settings.variant_limit from needed(... days=[today .. today+7]); returns
    # {"generated": [...], "gen_failed": [...], "cost": float}
def make_now_count(cat, all_items, settings, type: str, today: date) -> int
def regenerate(cat, http, data, provider, all_items, variant_id: int, crop: str) -> Variant
    # makes another of the same original and type, then marks the old one removed
```

`needed` for one day: for each flattened slice of that day's layers whose tags contain an `ai-variant:<t>` tag: `want` = the slice's count from `schedules.allocate(layers, P)` (no availability cap; `P` = min(pool, distinct eligible roots)); `have` = distinct roots with an eligible version of `t`; missing = `want - have`. Candidates: eligible non-version items with no non-removed version of `t` and `variant_state` failures `< NIGHTLY_FAILURES` for `(key, t)`; live roots first, then newest Library `added_at` first. `make_now_count(type)` uses the first day, from today, on which any schedule with an `ai-variant:<type>` slice runs (search up to 366 days); 0 if none.

Remove: `assign_slots`, `needed` (old), `apply_versions`, `share_types`, `museum_target`, `_plan`, `_live`, `progress`, `Catalogue.slots/set_slots/set_slot` callers (keep the methods and table for older releases).

- [ ] **Step 1: Failing tests**: `test_needed_fills_a_short_slice`, `test_needed_looks_a_week_ahead`, `test_needed_skips_removed_and_failing`, `test_needed_prefers_live_originals`, `test_nightly_respects_the_limit`, `test_nightly_without_a_key_makes_nothing`, `test_generate_from_photo_and_portrait` (fake provider as today), `test_regenerate_removes_the_old_version`, `test_make_now_count_uses_next_run`.
- [ ] **Step 2: Run** `uv run pytest tests/test_variants.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_variants.py tests/test_variants_catalogue.py -q` — pass.
- [ ] **Step 5: Commit** — `feat: make seasonal versions for ai-variant slices, of any Library work`

---

### Task 8: Jobs, service and CLI wiring

**Files:**
- Modify: `src/frame_art/jobs.py`, `src/frame_art/service.py`, `src/frame_art/cli.py`
- Test: `tests/test_jobs.py` (create), `tests/test_service.py` if present, else in `tests/test_web.py` fixtures only

**Interfaces:**
- Produces in `jobs`:
  - `publisher(cat, http, dest, s: Settings, data: Path) -> Publisher` (opens `PhotoStore(data/"frame-art.db", data/"photos")` and `StudioStore(data/"frame-art.db", data/"studio")`).
  - `publish(cat, http, dest, data, settings, today: date, seed: int | None = None) -> PublishResult` — layers from `schedules.layers(settings.schedules, today)`.
  - `nightly(cat, http, data, dest, settings, today) -> dict`: crawl if due → prune tries → `variants.nightly` (failures logged, run marked partial) → `publish`. Summary keys: `added`, `retired`, `failed`, `live`, `variants`, `tries_pruned`, plus crawl's keys. Versions are made **before** publishing so a schedule's versions go up on its first day.
  - `crawl` summary gains `"new": <count of works whose added_at ≥ the run start>` and `"since": <run start epoch>`.
  - Remove `sync_portraits`.
- `service`: job kinds `portraits` and `versions` go; `generate` takes `keys` (originals) and `type`, uses `variants.generate` per key, then submits nothing else (the next publish shows them); new kinds `make-now` (`{"type": t}`) and `regenerate` (`{"variant": id}`). `sync` calls `publisher(...).sync()`. `start()` calls `migrate.upgrade(...)` (Task 9) after `pins_to_collection`.
- `now_showing`: a `photo:` file → `{"ours": True, "kind": "photo", "title": "Your photo", "file": …}`; portrait as today; a version file → the original's fields plus `variant` label (as today).
- CLI: drop `--photo-share`, `--photo-crop`, `--full-screen-only`, `--allow-nudes` from publish/nightly; `publish` and `nightly` pass `--data`.

- [ ] **Step 1: Failing tests**: `test_nightly_makes_versions_before_publishing` (monkeypatched `variants.nightly` records call order), `test_crawl_reports_new_works`, `test_now_showing_a_photo_shows_no_filename`.
- [ ] **Step 2: Run** `uv run pytest tests/test_jobs.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` — only `tests/test_web.py` may still fail (Tasks 10–12).
- [ ] **Step 5: Commit** — `feat: nightly run makes versions then publishes from the Library`

---

### Task 9: One-time upgrade to 0.12.0

**Files:**
- Create: `src/frame_art/migrate.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Produces: `upgrade(cat: Catalogue, studio: StudioStore) -> bool` — runs once (kv `"library_0_12"`), returns whether it ran. Settings migrate in `Settings.load` (Task 4), not here.

Steps of `upgrade`, in one transaction where possible:
1. `work_added`: for each `works` key without a row, the earliest of `works.crawled_at`, `rendered.rendered_at`, `rejected.rejected_at`, `min(published.published_at)`.
2. `source_crawls`: per `works.source`, `max(crawled_at)`, unless a row exists.
3. Library `"in"`, unless the key is banned (`overrides`), with `added_at` = the collection date, else the first `published_at`, else the painting's `saved_at`: every `collection` key; every distinct `published.key` (all prefixes; version files count for their original); every saved painting's `portrait:<id>`.
4. Banned keys → `"removed"` (with `set_at`).
5. Versions: for each `variant_state` row, the current variant stays eligible (no row) unless `dont_use`, which makes it `"removed"`; every non-current variant → `"removed"` (R3).

- [ ] **Step 1: Failing tests**:

```python
def test_library_starts_with_collection_published_and_portraits(setup)
def test_banned_become_removed_and_stay_out(setup)
def test_dont_use_and_old_versions_are_removed(setup)
def test_dates_backfilled_from_earliest_known(setup)
def test_runs_once(setup)
def test_first_run_after_upgrade_does_not_churn(setup):
    # 12 live (museum, a version file, a portrait), Everyday migrated from photo_share 0;
    # Publisher.run(count=0, pool=150) retires nothing and adds nothing
```

- [ ] **Step 2: Run** `uv run pytest tests/test_migrate.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_migrate.py -q` — pass.
- [ ] **Step 5: Commit** — `feat: move the collection, bans, portraits and versions into the Library`

---

### Task 10: Library page and actions

**Files:**
- Modify: `src/frame_art/web.py`
- Create: `src/frame_art/templates/library.html`
- Modify: `src/frame_art/templates/work.html`, `_macros.html`, `static/app.js` (tag picker groups), `static/style.css`
- Test: `tests/test_web_library.py` (create)

**Interfaces:** routes (all POSTs redirect back via `back(request)` and submit `"sync"` when a live item became ineligible):

| Route | Does |
|---|---|
| `GET /library` | query: `scope` (`library` default \| `all`), `view` (`in` \| `out` \| `removed` \| `favorites`; default `in` for Library scope, none for All works), `q`, `source`, `kind`, `tag` (repeatable, `group:value`), `sort` (`added` default \| `title`), `added_since` (`YYYY-MM-DD`), `versions=1`, `nudes=hide`, `full_screen=1` (the old Settings filters, via `pipeline.exclusion` at the `crop` level), `page` |
| `GET /library/tags.json` | `q`, current `tag`s → `[{"tag": "kind:photograph", "label": "photograph", "group": "Kind", "count": n}]` |
| `POST /library/add` | form `key` (repeatable) or `all=1` + the filter's query string → `library.add` |
| `POST /library/remove` | same + `with_versions` (checkbox, default on) → `library.remove` |
| `POST /library/put-back` | same → `library.put_back` |
| `POST /library/star` | `key`, `on` → `cat.collect` |
| `POST /library/tag` | `key`/`all=1`, `tag`, `on` → `cat.set_tag` |
| `POST /library/make-versions` | `key`/`all=1`, `type` → submits `generate`; a confirm page first shows count and cost (`count × 0.12`) unless `confirm=1` |
| `POST /versions/{id:int}/remove`, `/put-back`, `/regenerate` | version actions |

- Cards: thumbnail (museum `pipeline.thumb_url`; photo `/photos/{id}/thumb`; portrait `/portraits/{id}/thumb`; version `/variant-file/{id}`), title, the relevant date small under the title ("added 3 Nov" / "in Library since 3 Nov"), state, star, "On the TV" badge, version link, Add / Remove / Put back. A rejected item shows its reason. Add on a museum work whose `pipeline.exclusion` gives a reason, or that is rejected, shows a warning line, not a block.
- Page header says when the Library is smaller than the pool.
- `/works/{key:path}` accepts `photo:`, `portrait:` and `variant:` keys: shows tags (with add/remove of user tags), Library state and, for portraits and versions, **Delete** (portrait: `studio.remove_painting`; version: delete its file, keep its `variants` row, set its state to removed).

- [ ] **Step 1: Failing tests**: `test_library_lists_only_library_by_default`, `test_all_works_and_views`, `test_tag_filter_is_group_qualified` (`subject:christmas` doesn't match an `ai-variant:christmas` version), `test_sort_newest_and_added_since`, `test_add_and_remove_and_put_back`, `test_remove_live_work_submits_sync`, `test_bulk_add_all_on_filtered_view`, `test_star_does_not_add`, `test_make_versions_confirms_cost_first`, `test_tags_json_groups`, `test_work_page_for_photo_portrait_version`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_library.py -q` — FAIL (404s).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_web_library.py -q` — pass.
- [ ] **Step 5: Commit** — `feat: Library page`

---

### Task 11: Schedule page and Version styles

**Files:**
- Modify: `src/frame_art/web.py`
- Create: `src/frame_art/templates/schedule.html`, `src/frame_art/templates/version_styles.html`
- Delete: `src/frame_art/templates/variants.html`
- Test: `tests/test_web_schedule.py` (create)

**Interfaces:**

| Route | Does |
|---|---|
| `GET /schedule` | timeline (`schedules.bars`), "Today on the TV" stacked bar with live count per slice (via `mix.member` on live items), and per schedule its slices with "N in Library, M wanted" and "See them" → `/library?tag=…` |
| `POST /schedule/schedules` | add a schedule (name, start, end) |
| `POST /schedule/schedules/{id}` | save name, dates and slices: form fields `slice_tags_<i>` (comma-separated `group:value`) and `slice_share_<i>`; validated with `schedules.validate` |
| `POST /schedule/schedules/{id}/delete` | delete |
| `GET /studio/versions` | types (name, prompt, cost so far, versions per type, `make_now_count`), and the per-night limit |
| `POST /studio/versions/types`, `/studio/versions/types/{slug}` | add / edit a type (moved from `/variants/types…`) |
| `POST /studio/versions/limit` | `variant_limit` |
| `POST /studio/versions/{slug}/make-now` | confirm page with count and cost first; with `confirm=1` submits `make-now` |

- An ai-variant slice's row shows "12 ready of 60 wanted · made up to a week before" from `needed`/`have`.
- The page says when the Library is smaller than the pool.

- [ ] **Step 1: Failing tests**: `test_schedule_page_renders_slices_and_counts`, `test_save_schedule_with_slices`, `test_bad_slice_tag_is_rejected`, `test_version_styles_lists_types_and_cost`, `test_make_now_confirms_then_submits`, `test_variant_type_add_and_edit`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_schedule.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_web_schedule.py -q` — pass.
- [ ] **Step 5: Commit** — `feat: Schedule page with tag slices; Version styles under Studio`

---

### Task 12: Nav, redirects, the other pages, docs

**Files:**
- Modify: `src/frame_art/web.py`, `templates/base.html`, `now.html`, `history.html`, `pool.html`, `settings.html`, `studio.html`, `photos.html`, `_runs_table.html`, `static/app.js` (viewer keys)
- Delete: `templates/catalogue.html`, `templates/paintings.html` if unused
- Modify: `src/frame_art/publish.py` docstring ("served publicly at /local/" → the folder is outside www and not served), `README.md`, `docs/design.md`
- Test: `tests/test_web.py` (update), `tests/test_web_library.py`

Changes:
- Nav: Overview · Now · On the TV · Library · Schedule · Studio · History · Compare crops · Settings · Runs (R4).
- Redirects (303, query string kept): `/catalogue` → `/library?scope=all`; `/collection` → `/library?view=favorites`; `/portraits` → `/library?tag=source:studio`; `/variants` → `/schedule`. Removed routes: `/variants/*` POSTs, `/portraits/{id}/remove` (Delete moves to the work page), `/tags.json` (→ `/library/tags.json`).
- Now page and History: Ban → **Remove from Library** (`POST /library/remove`), Collect → star (`POST /library/star`). Viewer keys: `S` star, `P` pin, `R` remove.
- On the TV (`/pool`): Pin / Unpin per live item (`POST /works/{key}/override`, `pinned`/`clear`), for every kind of item.
- `/works/{key}/override`: accepts only `pinned` and `clear`; `banned`/`collect`/`uncollect` return 400.
- Settings page: remove the dropped fields; `kinds` labelled "Kinds to crawl".
- Studio "Save to Portraits" also `cat.set_library([portrait key], "in")`; button label "Save to Library".
- Your photos page: header link "Browse in the Library" → `/library?tag=source:photos`; Library header link "Add photos" → `/photos`.
- Runs: a crawl or import row with `new > 0` links "N new" → `/library?scope=all&sort=added&added_since=<date of since>`.
- README: describe Library and Schedule; list every dropped setting (release notes).

- [ ] **Step 1: Failing tests**: update `test_pages_render` paths (add `/library`, `/schedule`, `/studio/versions`; old paths expect 303), `test_redirects_keep_query`, `test_now_remove_from_library`, `test_pool_pin_any_item`, `test_override_rejects_ban`, `test_save_try_adds_to_library`, `test_settings_form_without_dropped_fields`, `test_runs_links_new_works`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — all pass, clean.
- [ ] **Step 5: Commit** — `feat: new nav, redirects, and Library actions on Now, History and On the TV`

---

## Release notes for the PR (to fill at the end)

Name every touched module; cite the route-table diff (Tasks 10–12 tables plus removals); state **no HA config change**; list dropped settings (`choose_from`, `photo_share`, `photo_crop`, `allow_nudes`, `full_screen_only`, `repeat_days`) and that no env var is dropped; mention base-image drift if the Dockerfile base changed since 0.11.0.
