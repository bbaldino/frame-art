# Seasonal Variants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let frame-art keep AI-edited seasonal variants of works (e.g. Halloween), mix them onto the TV by a hand-set mix, generate them throttled each night or by hand, and manage them in the web UI.

**Architecture:** New tables in `catalogue.py` (variant types, variants, per-(work, type) state, slots, published-file → variant). A new `openrouter.py` client edits an image. A new `variants.py` holds slot assignment, generation, progress and the nightly step. `Publisher.swap` replaces a live work's file with another version through the existing publish/retire path, so HA's sync automation needs no change. Web UI gains a variants page and a per-work page.

**Tech Stack:** Python 3.13, uv, Starlette + Jinja2, SQLite, Pillow, httpx (`net.Http`), pytest, ruff 0.16.1.

**Spec:** `docs/superpowers/specs/2026-10-27-variants-design.md`

## Global Constraints

- Model: `google/gemini-nano-banana-2.1` via `https://openrouter.ai/api/v1/chat/completions`, `modalities: ["image", "text"]`, `image_config: {"aspect_ratio": <nearest>, "image_size": "4K"}`, `usage: {"include": true}`.
- OpenRouter key: `keys.read_key("openrouter")` (`<keys dir>/openrouter/key`). Never in settings, the repo, logs, run logs, exception text or pages.
- Variant files: `<data>/variants/<type slug>/<work file stem>/<n>.jpg`, JPEG quality 95. Trimmed sources: `<data>/sources/<work file stem>.jpg`, quality 95.
- A published file is never rewritten in place, and no filename is ever reused (the HA integration keys uploads by filename + mtime, and `published` is keyed by filename).
- Older frame-art versions must still read the catalogue: add tables only; do not alter `published`, `works` or other existing tables.
- Nightly automatic generation skips a (work, type) after 3 failures; every attempt (success or failure) counts toward the nightly limit.
- Default nightly limit: 10 edits (allowed 0-50). Mix: per-type integer shares 0-100, summing to at most 100; originals get the rest.
- Every live work is eligible for a variant slot (paintings and photographs, pinned and collection works).
- No Home Assistant changes. Do not touch `docs/ha-automation.yaml` except to read it.
- Match the surrounding code style: comment density, `# fmt: skip` habits, plain-language UI copy.
- Run `uv run ruff format --check . && uv run ruff check . && uv run pytest` before each commit.

## Review Focus

1. **Saving the main Settings page wipes the mix.** `settings_page` builds a fresh `Settings()` from the form; it must carry over `mix` and `variant_limit`. Test in Task 2.
2. **Reverting a work to its original reuses the original's filename**, overwriting the retired variant row whose TV id HA still has to delete. `unused_filename` prevents it. Test in Task 1 and Task 6.
3. **The OpenRouter key leaks** through an exception message, a log line or a run log. Test in Task 3.
4. **No key, no credit, or OpenRouter down (HTTP 402/429/5xx)**: the nightly run carries on, counts the failure, and the work keeps its original. Test in Task 7.
5. **A type leaves the mix while the nightly limit is 0 or no key is present**: its works must still go back to originals, since reverting costs no edits. Test in Task 7.

---

### Task 1: Catalogue tables for variants

**Files:**
- Modify: `src/frame_art/catalogue.py` (SCHEMA, dataclasses, `Catalogue` methods, `record_publish`)
- Test: `tests/test_variants_catalogue.py` (create)

**Interfaces:**
- Produces (in `catalogue.py`):
  - `@dataclass VariantType: slug: str; name: str; prompt: str`
  - `@dataclass Variant: id: int; key: str; type: str; n: int; prompt: str; model: str; cost: float | None; path: str; created_at: float`
  - `@dataclass VariantState: current: int | None; dont_use: bool; failures: int`
  - `@dataclass Slot: type: str; manual: bool`
  - `Catalogue.add_variant_type(slug: str, name: str, prompt: str) -> None` (ValueError if slug exists)
  - `Catalogue.update_variant_type(slug: str, name: str, prompt: str) -> None`
  - `Catalogue.variant_types() -> list[VariantType]` (creation order)
  - `Catalogue.next_variant_n(key: str, type: str) -> int` (1 for the first)
  - `Catalogue.add_variant(key, type, n, prompt, model, cost, path) -> Variant` — makes it current, clears `dont_use`, resets `failures`
  - `Catalogue.variants(key: str | None = None) -> list[Variant]` (oldest first)
  - `Catalogue.variant(variant_id: int) -> Variant | None`
  - `Catalogue.set_current_variant(key: str, type: str, variant_id: int) -> None` — clears `dont_use`
  - `Catalogue.set_dont_use(key: str, type: str, on: bool) -> None`
  - `Catalogue.note_variant_failure(key: str, type: str) -> int` (new count)
  - `Catalogue.variant_states() -> dict[tuple[str, str], VariantState]`
  - `Catalogue.usable_variants() -> dict[tuple[str, str], Variant]` — current and not `dont_use`
  - `Catalogue.slots() -> dict[str, Slot]`; `Catalogue.set_slots(slots: dict[str, Slot]) -> None` (replaces all); `Catalogue.set_slot(key: str, type: str, manual: bool = True) -> None`
  - `Catalogue.record_publish(filename, folder, key, phash, variant: int | None = None)` — also writes `published_variants` when `variant` is set
  - `Catalogue.published_variants() -> dict[str, int]` (filename → variant id)
  - `Catalogue.unused_filename(name: str) -> str` — `name` if no `published` row has it, else `stem-2.jpg`, `stem-3.jpg`, … first unused

New tables (append to SCHEMA, `create table if not exists`): `variant_types(slug primary key, name, prompt, created_at)`, `variants(id integer primary key, key, type, n, prompt, model, cost, path, created_at, unique(key, type, n))`, `variant_state(key, type, current, dont_use, failures, primary key(key, type))`, `slots(key primary key, type, manual)`, `published_variants(filename primary key, variant)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_variant_lifecycle(cat):
    cat.add_variant_type("halloween", "Halloween", "Make it spooky")
    with pytest.raises(ValueError):
        cat.add_variant_type("halloween", "Again", "x")
    assert cat.next_variant_n("nga:1", "halloween") == 1
    v1 = cat.add_variant("nga:1", "halloween", 1, "p1", "m", 0.12, "/d/1.jpg")
    v2 = cat.add_variant("nga:1", "halloween", 2, "p2", "m", None, "/d/2.jpg")
    assert cat.next_variant_n("nga:1", "halloween") == 3
    assert cat.usable_variants()[("nga:1", "halloween")].id == v2.id
    cat.set_dont_use("nga:1", "halloween", True)
    assert ("nga:1", "halloween") not in cat.usable_variants()
    cat.set_current_variant("nga:1", "halloween", v1.id)  # clears don't use
    assert cat.usable_variants()[("nga:1", "halloween")].id == v1.id
    assert cat.note_variant_failure("nga:1", "halloween") == 1
    cat.add_variant("nga:1", "halloween", 3, "p3", "m", 0.1, "/d/3.jpg")
    assert cat.variant_states()[("nga:1", "halloween")].failures == 0

def test_unused_filename_never_reuses_a_published_name(cat):
    cat.record_publish("a.jpg", "full", "nga:1", 0)
    cat.record_retire("a.jpg", "MY_F0001")
    assert cat.unused_filename("a.jpg") == "a-2.jpg"
    assert cat.unused_filename("b.jpg") == "b.jpg"

def test_published_variant_recorded_beside_published(cat):
    cat.record_publish("a--halloween-1.jpg", "full", "nga:1", 0, variant=7)
    assert cat.published_variants() == {"a--halloween-1.jpg": 7}
    assert len(cat.published()) == 1  # older readers still see 7 columns

def test_slots_replace_and_set(cat):
    cat.set_slots({"nga:1": Slot("halloween", False)})
    cat.set_slot("nga:2", "halloween")
    assert cat.slots() == {"nga:1": Slot("halloween", False), "nga:2": Slot("halloween", True)}
    cat.set_slots({})
    assert cat.slots() == {}
```

(`cat` fixture: `Catalogue(tmp_path / "db.sqlite")` with two works `nga:1`, `nga:2` upserted.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_variants_catalogue.py -v` → FAIL (ImportError / AttributeError).
- [ ] **Step 3: Implement the tables, dataclasses and methods above in `catalogue.py`.**
- [ ] **Step 4: Run** `uv run pytest -q` → all pass (existing 72 plus new).
- [ ] **Step 5: Commit** — `git commit -m "feat: catalogue tables for seasonal variants"`

### Task 2: Settings: mix and nightly limit

**Files:**
- Modify: `src/frame_art/settings.py`, `src/frame_art/web.py` (`settings_page` only)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `Settings.mix: dict[str, int]` (default `{}`: originals 100%), `Settings.variant_limit: int = 10`, `Settings.originals_share() -> int` (100 minus the sum).
- `validate()` adds: mix must be a dict of str → int, each 0-100, sum ≤ 100 ("seasonal shares must add up to 100% or less"); `variant_limit` 0-50 ("variants per night must be 0-50").

- [ ] **Step 1: Write the failing tests**

```python
def test_mix_validates(tmp_path):
    assert Settings(mix={"halloween": 50}).validate() == []
    assert Settings(mix={"halloween": 60, "autumn": 50}).validate()
    assert Settings(mix={"halloween": -1}).validate()
    assert Settings(variant_limit=51).validate()
    assert Settings(mix={"halloween": 30}).originals_share() == 70

def test_saving_settings_keeps_the_mix(client):
    c, svc = client
    svc.save_settings(dataclasses.replace(svc.settings(), mix={"halloween": 50}, variant_limit=4))
    form = {"crop": "medium", "pad_style": "blur", "kinds": "painting",
            "sources": "nga", "count": "5", "pool": "100", "run_at": "03:30",
            "recrawl_days": "7", "crawl_limit": "300"}  # fmt: skip
    assert "Saved" in c.post("/settings", data=form).text
    assert svc.settings().mix == {"halloween": 50}
    assert svc.settings().variant_limit == 4
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Add the fields and validation; in `settings_page` set `new.mix, new.variant_limit = s.mix, s.variant_limit` before validating.**
- [ ] **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** — `feat: settings for the seasonal mix and nightly variant limit`

### Task 3: OpenRouter image-edit client

**Files:**
- Create: `src/frame_art/openrouter.py`
- Test: `tests/test_openrouter.py`

**Interfaces:**
- Produces:
  - `MODEL = "google/gemini-nano-banana-2.1"`, `URL = "https://openrouter.ai/api/v1/chat/completions"`
  - `ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")`
  - `class EditFailed(Exception)`
  - `@dataclass Edit: image: Image.Image; cost: float | None; generation_id: str | None`
  - `nearest_aspect(width: int, height: int) -> str` (closest by ratio)
  - `edit_image(http: Http, api_key: str, im: Image.Image, prompt: str, model: str = MODEL, size: str = "4K") -> Edit`

Request: one user message with a text part (the prompt) and an `image_url` part holding a `data:image/jpeg;base64,` of `im` downscaled to at most 2048px on its long side (JPEG quality 90); header `Authorization: Bearer <api_key>`; aspect from `nearest_aspect(*im.size)`. Response image is `choices[0].message.images[0].image_url.url` (a data URL); cost is `usage.cost`; id is `id`. Use `http.request("POST", URL, json=..., headers=...)` so `Http`'s retry covers 429/5xx; the caller supplies an `Http(timeout=300)`. Any non-2xx status, an `error` key, or no image raises `EditFailed` with the status and OpenRouter's error message only (never headers or the request).

- [ ] **Step 1: Write the failing tests** with a fake `http` whose `request()` records its arguments and returns a stub response (`status_code`, `json()`):

```python
def test_edit_sends_image_and_reads_result():
    http = FakeHttp(200, {"id": "gen-1", "usage": {"cost": 0.118},
        "choices": [{"message": {"images": [{"image_url": {"url": png_data_url(64, 36)}}]}}]})
    out = openrouter.edit_image(http, "sk-secret", Image.new("RGB", (3000, 2000)), "spooky")
    assert out.image.size == (64, 36) and out.cost == 0.118 and out.generation_id == "gen-1"
    body = http.calls[0]["json"]
    assert body["model"] == openrouter.MODEL
    assert body["image_config"] == {"aspect_ratio": "3:2", "image_size": "4K"}
    assert http.calls[0]["headers"]["Authorization"] == "Bearer sk-secret"
    sent = Image.open(io.BytesIO(base64.b64decode(
        body["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1])))
    assert max(sent.size) == 2048

@pytest.mark.parametrize("status,payload", [
    (402, {"error": {"message": "Insufficient credits"}}),
    (200, {"error": {"message": "boom"}}),
    (200, {"choices": [{"message": {"content": "no can do"}}]}),
])
def test_edit_failures_never_mention_the_key(status, payload):
    with pytest.raises(openrouter.EditFailed) as ex:
        openrouter.edit_image(FakeHttp(status, payload), "sk-secret", Image.new("RGB", (10, 10)), "p")
    assert "sk-secret" not in str(ex.value)

def test_nearest_aspect():
    assert openrouter.nearest_aspect(3840, 2160) == "16:9"
    assert openrouter.nearest_aspect(2000, 2600) == "3:4"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `openrouter.py`.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** — `feat: OpenRouter image-edit client`

### Task 4: Render from an image in hand

**Files:**
- Modify: `src/frame_art/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `pipeline.render_image(work: Work, im: Image.Image, prefs: Prefs, pad_style: str = "blur") -> Output` — the part of `render()` after `load_work` (plan from `im.size`, `NotFullScreen` / `TooSmall` checks, phash, render, palette). `render()` becomes `render_image(cand.work, load_work(http, cand), cand.prefs, pad_style)`.

- [ ] **Step 1: Write the failing test** `test_render_image_fills_the_panel`: a 3000x2250 image with default prefs gives `out.image.size == imaging.PANEL` and `out.plan.mode == "pad"`; a 600x400 image raises `pipeline.TooSmall`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Extract `render_image`.**
- [ ] **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** — `refactor: render a work from an image already loaded`

### Task 5: Slot assignment and what needs generating

**Files:**
- Create: `src/frame_art/variants.py`
- Test: `tests/test_variants.py`

**Interfaces:**
- Consumes: `Slot`, `VariantState` (Task 1).
- Produces:
  - `NIGHTLY_FAILURES = 3`
  - `assign_slots(live: list[str], mix: dict[str, int], current: dict[str, Slot], usable: set[tuple[str, str]]) -> dict[str, Slot]`
  - `needed(slots: dict[str, Slot], states: dict[tuple[str, str], VariantState], live: list[str]) -> list[tuple[str, str]]`

`live` is oldest-published first (the order of `cat.published(active=True)`). The algorithm, which the tests pin down:

```text
types   = mix types with share > 0, in mix order
target  = {t: round(share * len(live) / 100)}
slots   = current slots whose key is live and whose type is in types
for t over target: drop non-manual members until at target, dropping those
    without a usable (key, t) variant first, then the oldest
for t: fill from works with no slot until at target, taking works with a
    usable (key, t) variant first, then the newest
```

`needed`: the (key, type) pairs in `slots` with no usable variant, not `dont_use`, and fewer than `NIGHTLY_FAILURES` failures, newest work first.

- [ ] **Step 1: Write the failing tests**

```python
LIVE = ["a", "b", "c", "d"]  # oldest first

def test_half_mix_takes_newest_works():
    assert assign_slots(LIVE, {"hw": 50}, {}, set()) == {"c": Slot("hw", False), "d": Slot("hw", False)}

def test_slots_stick_and_prefer_works_with_variants():
    cur = {"a": Slot("hw", False)}
    got = assign_slots(LIVE, {"hw": 50}, cur, {("b", "hw")})
    assert set(got) == {"a", "b"}

def test_type_leaving_the_mix_clears_its_slots_even_manual():
    assert assign_slots(LIVE, {}, {"a": Slot("hw", True)}, set()) == {}

def test_shrinking_share_keeps_manual_and_drops_variantless_first():
    cur = {k: Slot("hw", k == "a") for k in LIVE}
    got = assign_slots(LIVE, {"hw": 50}, cur, {("a", "hw"), ("c", "hw"), ("d", "hw")})
    assert set(got) == {"a", "d"}  # b has no variant; then oldest (c) goes

def test_retired_works_lose_their_slot():
    assert assign_slots(["a"], {"hw": 100}, {"z": Slot("hw", False)}, set()) == {"a": Slot("hw", False)}

def test_needed_skips_usable_dont_use_and_repeated_failures():
    slots = {k: Slot("hw", False) for k in LIVE}
    states = {("a", "hw"): VariantState(5, False, 0), ("b", "hw"): VariantState(None, True, 0),
              ("c", "hw"): VariantState(None, False, 3)}
    assert needed(slots, states, LIVE) == [("d", "hw")]
```

(`("a", "hw")` usable means `states` has a current id and not `dont_use`; `needed` reads only `states`.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement both functions.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** — `feat: assign seasonal slots to the works on the TV`

### Task 6: Generate a variant and swap versions on the TV

**Files:**
- Modify: `src/frame_art/variants.py`, `src/frame_art/publish.py`
- Test: `tests/test_variants.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5.
- Produces:
  - `Publisher.prefs_for(w: Work) -> pipeline.Prefs` (photo prefs for photographs)
  - `Publisher.swap(p: Published, im: Image.Image, variant: Variant | None) -> str` — renders `im` with `render_image`, writes it under `cat.unused_filename(name)` (`name` is `pipeline.filename(w)` for the original, or `<stem>--<type>-<n>.jpg`), calls `record_publish(..., variant=variant.id if variant else None)`, then retires `p` the same way `_retire` does (extract that loop body into `_retire_files(doomed: list[Published])`). Returns `"<folder>/<new filename>"`.
  - `variants.source_path(data: Path, w: Work) -> Path`, `variants.variant_path(data: Path, w: Work, type: str, n: int) -> Path`
  - `variants.load_source(http, data: Path, w: Work, prefs: pipeline.Prefs) -> Image.Image` — the cached trimmed source, or `pipeline.load_work` on a `Candidate` built from `pipeline.exclusion(w, prefs)` (falling back to `imaging.plan_for(w.width, w.height, prefs.max_crop)` when excluded), saved at quality 95
  - `variants.HALLOWEEN_PROMPT` — the spec appendix prompt, verbatim (defined here; Task 7 reuses it)
  - `variants.generate(cat, http, data: Path, api_key: str, pub: Publisher, key: str, type: str, prompt: str | None = None) -> Variant` — prompt defaults to the type's; saves the edit at quality 95; `add_variant`. On any exception it calls `note_variant_failure` and re-raises.
  - `variants.apply_versions(cat, http, data: Path, pub: Publisher, keys: set[str] | None = None) -> dict` — for each live work (optionally only `keys`): wanted = the usable variant for its slot's type, else the original; if the live file's `published_variants` entry differs, `pub.swap` it. Returns `{"swapped": [...], "failed": [...]}`; a failure leaves the work as it is. Calls `pub._write_meta()` once when anything changed.

- [ ] **Step 1: Write the failing tests** (reuse the `setup` fixture pattern from `tests/test_publish.py`; monkeypatch `openrouter.edit_image` to return `Edit(<solid 3840x2160 image>, 0.12, "gen-x")` and `pipeline.load_work` to return a solid 3840x2160 image; smaller fakes trip `TooSmall`. Add the "halloween" type with `HALLOWEEN_PROMPT` in the fixture):

```python
def test_generate_then_swap_and_revert(vsetup):
    cat, dest, data, pub = vsetup                     # one live work "s1:1", with sidecar content id MY_F0001
    v = variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert v.n == 1 and Path(v.path).exists() and v.prompt == HALLOWEEN_PROMPT
    cat.set_slots({"s1:1": Slot("halloween", False)})
    res = variants.apply_versions(cat, None, data, pub)
    [live] = cat.published(active=True)
    assert live.filename.endswith("--halloween-1.jpg") and res["swapped"]
    assert cat.published_variants()[live.filename] == v.id
    assert publish.pending(cat, dest)["retire"] == ["MY_F0001"]
    cat.set_slots({})
    variants.apply_versions(cat, None, data, pub)
    [back] = cat.published(active=True)
    assert back.filename == "s1-1-artist-1-work-1-2.jpg"   # never the old name
    assert "MY_F0001" in publish.pending(cat, dest)["retire"]  # still owed a delete

def test_generate_failure_is_counted_and_reraised(vsetup, monkeypatch):
    cat, dest, data, pub = vsetup
    def boom(*a, **k): raise openrouter.EditFailed("HTTP 402: Insufficient credits")
    monkeypatch.setattr(openrouter, "edit_image", boom)
    with pytest.raises(openrouter.EditFailed):
        variants.generate(cat, None, data, "k", pub, "s1:1", "halloween")
    assert cat.variant_states()[("s1:1", "halloween")].failures == 1

def test_source_is_cached(vsetup, monkeypatch): # second load_source does not call pipeline.load_work
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `prefs_for`, `_retire_files`, `swap` in `publish.py`; `source_path`, `variant_path`, `load_source`, `generate`, `apply_versions` in `variants.py`.**
- [ ] **Step 4: Run** `uv run pytest -q` → pass (existing publish tests too).
- [ ] **Step 5: Commit** — `feat: generate variants and swap them onto the TV`

### Task 7: Nightly step, progress, and jobs with arguments

**Files:**
- Modify: `src/frame_art/variants.py`, `src/frame_art/jobs.py`, `src/frame_art/service.py`
- Test: `tests/test_variants.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces:
  - `variants.DEFAULT_TYPES = (("halloween", "Halloween", HALLOWEEN_PROMPT),)` (`HALLOWEEN_PROMPT` from Task 6); `variants.seed_types(cat) -> None` — once (kv `variant_types_seeded`), only if there are no types; called from `Service.start()`
  - `variants.nightly(cat, http, data: Path, pub: Publisher, settings: Settings, api_key: str | None) -> dict` — `assign_slots` (mix types not in `variant_types()` are ignored) → `set_slots` → generate `needed(...)[: settings.variant_limit]` (skipped with a logged note when `api_key` is None; each failure logged and skipped) → `apply_versions`. Returns `{"slots": {type: count}, "generated": [...], "gen_failed": [...], "swapped": [...], "cost": float}`.
  - `variants.progress(cat, settings) -> list[dict]` — per variant type: `slug, name, prompt, share, live_share` (percent of live files that are this type's variants), `slotted`, `ready` (slotted with a usable variant), `needed`, `stuck` (keys at `NIGHTLY_FAILURES`), `cost` (sum of the type's variant costs)
  - `jobs.nightly` calls `variants.nightly` after `publish`, with `keys.read_key("openrouter")` and `Http(timeout=300)` for the edits, and merges its summary under `"variants"`; a `"gen_failed"` entry makes the run `partial`.
  - `service.Job.args: dict | None = None`; `Service.submit(kind: str, args: dict | None = None) -> bool` — de-duplicates on (kind, args); accepts `JOB_KINDS` plus `ARG_JOBS = {"generate": "Generate seasonal variants", "versions": "Update seasonal versions on the TV"}`. `/jobs/{kind}` still only accepts `JOB_KINDS`.
  - Job `generate` (`args = {"type": str, "keys": list[str], "prompt": str | None}`): for each key, `variants.generate`; on success, if the work is live and the type is in the mix, `cat.set_slot(key, type, manual=True)`; then `apply_versions(keys=…)`. Summary `{"generated", "failed", "cost"}`.
  - Job `versions` (`args = {"keys": list[str]}`): `apply_versions(keys=…)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_nightly_generates_up_to_the_limit_and_swaps(vsetup4):     # 4 live works, mix {"halloween": 100}, limit 2
    out = variants.nightly(cat, None, data, pub, settings, "k")
    assert len(out["generated"]) == 2 and len(out["swapped"]) == 2

def test_nightly_without_key_or_limit_still_reverts(vsetup4):
    # two works showing variants, then mix {}:
    out = variants.nightly(cat, None, data, pub, dataclasses.replace(settings, mix={}, variant_limit=0), None)
    assert len(out["swapped"]) == 2
    assert not {p.filename for p in cat.published(active=True)} & set(cat.published_variants())

def test_nightly_carries_on_after_a_failed_edit(vsetup4, monkeypatch):
    # edit_image fails for the first work only
    out = variants.nightly(cat, None, data, pub, settings, "k")
    assert len(out["gen_failed"]) == 1 and len(out["generated"]) == 1

def test_progress_counts(vsetup4): # after one nightly: ready == 2, needed == 2, cost == 0.24

def test_generate_job_pins_slot_and_dedupes(client):
    c, svc = client
    assert svc.submit("generate", {"type": "halloween", "keys": ["nga:1"], "prompt": None})
    assert not svc.submit("generate", {"type": "halloween", "keys": ["nga:1"], "prompt": None})
    assert c.post("/jobs/generate").status_code == 404
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Keep the existing job flow (`_run`, run log, `publish_state` afterwards).
- [ ] **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** — `feat: nightly seasonal variants and generate jobs`

### Task 8: Variants page

**Files:**
- Modify: `src/frame_art/web.py`, `src/frame_art/templates/base.html` (nav entry "Seasonal" → `/variants`)
- Create: `src/frame_art/templates/variants.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `variants.progress`, `Catalogue` variant-type methods, `Service.submit`.
- Routes (add before the catch-all work route of Task 9):
  - `GET /variants` — per-type progress rows ("Halloween: 50% wanted, 12% live, 9 of 75 ready", stuck works, cost so far), total cost, whether the OpenRouter key is present (`keys.has_key("openrouter")`, never the key), the mix form, the add-type form, a per-type edit form (name, default prompt), and "Generate for…": a type select plus checkboxes of live works without a usable variant of it, with the estimated cost (`0.12` × count, labelled an estimate).
  - `POST /variants/types` (name, prompt; slug = `pipeline.slug(name)`; error if empty or taken)
  - `POST /variants/types/{slug}` (name, prompt)
  - `POST /variants/mix` (`share-<slug>` per type, `variant_limit`) → validate via `Settings.validate`, save, re-render with errors or "Saved"
  - `POST /variants/generate` (`type`, `keys` list) → `svc.submit("generate", …)`, redirect to `/runs`

- [ ] **Step 1: Write the failing tests**: `/variants` renders and shows "Halloween" after `seed_types`; posting the mix `{"share-halloween": "50", "variant_limit": "5"}` saves `{"halloween": 50}` and 5; posting `share-halloween=150` shows the validation error and saves nothing; adding a type with an existing name shows an error; `POST /variants/generate` queues one `generate` job with the posted keys; the page never contains the key file's contents (`monkeypatch.setenv("FRAME_ART_KEYS", …)` with key `sk-secret`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement routes and template**, following `settings.html` for forms and `_runs_table.html` style for tables.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** — `feat: seasonal variants page`

### Task 9: Work page

**Files:**
- Modify: `src/frame_art/web.py`, `src/frame_art/templates/_macros.html` (`caption` gains a "Details" link to `/works/<key>`)
- Create: `src/frame_art/templates/work.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: Task 1 methods, `Service.submit`, `variants.variant_path`.
- Routes:
  - `GET /variant-file/{id:int}` — the variant JPEG; `?thumb=1` serves a cached 480x270 thumbnail under `<data>/thumbs/variants/<id>.jpg` (same approach as `thumb`)
  - `POST /works/{key:path}/variants/generate` (`type`, `prompt`) → `svc.submit("generate", {"type", "keys": [key], "prompt": prompt if it differs from the type's default else None})`
  - `POST /works/{key:path}/variants/{id:int}/current` → `set_current_variant`, then `submit("versions", {"keys": [key]})`; the slot becomes manual if the type is in the mix
  - `POST /works/{key:path}/variants/{type}/dont-use` (`on` = `1`/`0`) → `set_dont_use`, then `submit("versions", {"keys": [key]})`
  - `GET /works/{key:path}` — registered **last** among `/works/...` routes; 404 for an unknown key. Shows the original (`preview_url`) with the existing override buttons, then each variant type: the current variant large, older generations small, each with prompt, model, cost and date; "Use this one", "Don't use" / "Use again"; and a regenerate form whose textarea is pre-filled with the type's default prompt, with the default prompt also shown read-only beside it ("Default prompt: …").

- [ ] **Step 1: Write the failing tests**: the work page renders for `nga:1` and 404s for `nga:999`; with a variant added, the page shows its prompt and cost, and the regenerate form's textarea and the "Default prompt" text both contain the type's prompt; posting dont-use sets the state and queues a `versions` job; posting generate with the unchanged default queues `prompt: None`, and with an edited prompt queues that prompt; `/variant-file/<id>` returns `image/jpeg`, and `/variant-file/999` returns 404; `/works/nga:1/override` still works (route order).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** — `feat: work page with seasonal variants`

### Task 10: Now showing, docs

**Files:**
- Modify: `src/frame_art/service.py` (`now_showing`), `src/frame_art/templates/now.html`, `src/frame_art/web.py` (`history_page`), `src/frame_art/templates/history.html`, `docs/api-keys.md`, `README.md`, `docs/superpowers/specs/2026-10-27-variants-design.md` (Status line)
- Test: `tests/test_web.py`

**Interfaces:**
- `now_showing()` adds `variant: {"type": <type name>, "n": <n>}` when the shown file is in `published_variants`, else no key. `now.html` links the work to `/works/<key>`.
- History rows whose content id maps (via `find_content`) to a variant file get `"variant": <type name>`, shown as "Halloween version". This also reaches `sensor.frame_art_now_showing`'s attributes; call that out in the release PR.

- [ ] **Step 1: Write the failing test** `test_now_showing_names_the_variant`: publish a variant file, report its content id as current, and assert `now_showing()["variant"] == {"type": "Halloween", "n": 1}` and that `/now` contains "Halloween version"; `test_history_names_the_variant`: the same content id in `shown` makes `/history` contain "Halloween version".
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement; document `openrouter` in `docs/api-keys.md`** (what it's for, `<keys dir>/openrouter/key`, the container keys mount still to be wired, "set a spending limit on the key"); README: one paragraph on seasonal variants; spec Status → "approved 2026-10-27; implemented".
- [ ] **Step 4: Run** `uv run ruff format --check . && uv run ruff check . && uv run pytest` → pass.
- [ ] **Step 5: Commit** — `feat: say when the TV shows a seasonal version; document the OpenRouter key`

## After the plan (not tasks for the implementer)

The release and deploy go through the usual route, each step with its own approval:
- release-please release, then a homelab-stacks pin bump (tag@digest);
- the container keys mount (`FRAME_ART_KEYS=/keys`, read-only bind of `keys/`) in the same PR, for proxmox to review;
- Brian saves `keys/openrouter/key` on the host and adds OpenRouter credit.
