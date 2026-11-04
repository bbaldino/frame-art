# Portrait Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Brian turns his photos into paintings in a Studio (choose a painter, paint, nudge, save), and saved paintings rotate onto the TV at a "Portraits" share set in the schedule editor; released as 0.11.0 together with the photo sources.

**Architecture:** A provider interface wraps OpenRouter image edits. A new `studio.py` holds the painter list, the prompt builder, tries and saved paintings (own tables, own folders under the data dir). Painting runs as a worker job; the Studio page reloads itself while one is running. A new `portraits.py` nightly step publishes saved paintings into the same output folder as museum works, *on top of* the museum pool, under keys `portrait:<id>` that every museum code path skips.

**Tech Stack:** Python 3.12, Starlette/Jinja, SQLite, Pillow, pytest, ruff 0.16.1; OpenRouter `google/gemini-nano-banana-2.1`.

**Spec:** `docs/superpowers/specs/2026-11-02-portrait-studio-design.md` (Goals 4–7, Behaviour, "Where paintings go"). Builds on `docs/superpowers/plans/2026-11-02-photo-sources.md` (Tasks 1–7 done on branch `photo-sources`; its Task 8 release is folded into Task 7 here).

## Global Constraints

- Private stays private: photos, tries and paintings never appear on catalogue/public pages, crawls, notability or retirement of museum works; only Brian's LAN UI and the TV.
- The OpenRouter key never reaches a page, log, exception text or the bus. Errors are shown plainly; a 402 reads **"OpenRouter: balance too low"**.
- Tries are painted at size `"4K"`; **Save never calls the model again** (it keeps the exact file shown).
- Every prompt carries the restraint rules verbatim (Task 2).
- Painting filenames carry no personal names or photo titles: `portrait-<painting id>.jpg`.
- `sensor.frame_art_now_showing` and `_meta/index.json` give no title for a painting: now-showing says **"Portrait"**; index.json omits paintings.
- Home Assistant stays the only thing talking to the TV; no HA change.
- Unsaved tries are deleted after **14 days**. Photo originals are kept until removed.
- Lint: `uvx ruff@0.16.1 check .` and `uvx ruff@0.16.1 format --check .`; tests: `uv run pytest -q`.
- Out of scope (next release): "Place them in this artwork" (two-input recipe), recipe stacking, nightly auto-painting.

## Review Focus

1. **A paint fails** (402 low balance, timeout, reply with no image): the try shows the plain reason, no cost is recorded, the page stops waiting, other tries are untouched. → Task 3.
2. **Paint clicked twice / page reloaded after Paint**: each click queues at most one try (POST then redirect), a reload never repaints. → Task 3.
3. **A photo is deleted after painting it**: its unsaved tries and their files go; saved paintings stay in the collection and on the TV; its Studio URL answers 404. → Task 4.
4. **Portraits share with no saved paintings, or at 100%**: 0 saved → nothing published, the share falls through like a short seasonal type; 100% → every saved painting is shown and the museum pool is unchanged. → Task 6.
5. **A saved painting removed while it is on the TV**: its file is retired at once and its TV id reaches HA through the normal retire list; no museum work is retired or added to make up. → Task 6.

---

### Task 1: Image provider interface

**Files:**
- Create: `src/frame_art/providers.py`
- Modify: `src/frame_art/variants.py` (`generate` uses the provider), `src/frame_art/openrouter.py` (no behaviour change)
- Test: `tests/test_providers.py` (new); existing `tests/test_variants.py` stays green

**Interfaces:**
- Produces, in `providers.py`:
  - `Painted = openrouter.Edit` and `PaintFailed = openrouter.EditFailed` (aliases, so variants code keeps its except clauses).
  - `class ImageProvider(Protocol)`: `name: str`; `def paint(self, im: Image.Image, prompt: str, aspect: str | None = None, size: str = "4K") -> Painted`.
  - `class OpenRouterProvider`: `__init__(self, http: Http, api_key: str, model: str = openrouter.MODEL)`; `name = "openrouter"`; `paint` calls `openrouter.edit_image`.
  - `def image_provider(http: Http) -> ImageProvider | None`: OpenRouter with `keys.read_key("openrouter")`, or None without a key.
  - `def plain_error(e: Exception) -> str`: `"OpenRouter: balance too low"` when the message holds `402`; `"OpenRouter: no image came back"` for "returned no image"; otherwise `str(e)`.

- [ ] **Step 1: Write the failing tests** (reuse the `FakeHttp`/`png_data_url` shapes from `tests/test_openrouter.py`):
  - `OpenRouterProvider(http, "sk").paint(im, "p", "16:9")` posts `image_config == {"aspect_ratio": "16:9", "image_size": "4K"}` and returns a `Painted` with the cost.
  - `image_provider(http)` is None with `FRAME_ART_KEYS` pointing at an empty dir, and an `OpenRouterProvider` when `openrouter/key` exists.
  - `plain_error(PaintFailed("OpenRouter returned 402: requires at least $1.00"))  == "OpenRouter: balance too low"`; the key `"sk-secret"` never appears in any `plain_error` output for an error built from a reply.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_providers.py` — expected FAIL (no module).
- [ ] **Step 3: Implement** `providers.py`; change `variants.generate` to take the provider (`image_provider(http)` built where the API key is read today) instead of calling `openrouter.edit_image` directly. Keep its prompts, sizes and failure counting unchanged.
- [ ] **Step 4: Run** the new tests, then the full suite and ruff — expected PASS.
- [ ] **Step 5: Commit** `feat: image providers behind an interface (OpenRouter first)`

### Task 2: Painters and the prompt

**Files:**
- Create: `src/frame_art/studio.py`, `src/frame_art/templates/painters.html`
- Modify: `src/frame_art/web.py` (routes), `static/style.css`
- Test: `tests/test_studio.py` (new), `tests/test_web.py`

**Interfaces:**
- Produces, in `studio.py`:
  - `SETTINGS = ("keep", "reinvent")`, `SHAPES = ("fill", "keep")`, `NUDGES = ("closer to the photo", "more painterly", "simpler background", "warmer light")`.
  - `@dataclass Painter(slug: str, name: str, manner: str, suits: tuple[str, ...])` — `suits` ⊆ `photos.KINDS`.
  - `class StudioStore(db_path: Path, files_dir: Path)` with painters: `painters() -> list[Painter]` (name order), `painter(slug)`, `add_painter(name, manner, suits) -> Painter` (slug from name via `pipeline.slug`, `ValueError` on duplicate or empty name), `update_painter(slug, name, manner, suits)`, `remove_painter(slug)`. Seeds the five defaults once (kv flag like `variants.seed_types`).
  - `def suggested(painters, kind: str | None) -> list[Painter]`: painters whose `suits` holds `kind` first, then the rest, each group in name order.
  - `def build_prompt(painter_name: str, manner: str, setting: str, shape: str, nudges: list[str]) -> str` — parts joined with single spaces, in this order: `BASE.format(name=...)`, `manner` (if any), `SETTING_TEXT[setting]`, `PEOPLE`, `RESTRAINT`, `SHAPE_TEXT[shape]` (if any), then `"Also: " + "; ".join(nudges) + "."` when there are nudges.
  - `def retouch_prompt(nudges: list[str]) -> str`: `RETOUCH`, then the same `"Also: …"` sentence, then `RESTRAINT`.
- Exact text (from the trial prompts; restraint verbatim):
  - `BASE = "Paint this photograph as {name} would have painted it: an original oil painting in their manner, not a filtered photograph. Use their palette, their way of handling light, and clearly visible brushwork and paint texture."`
  - `SETTING_TEXT["keep"] = "Like the artist, simplify and edit: soften or reduce background detail and clutter, drop photographic sharpness everywhere, and let the people be the subject, even when they are small in the frame. Keep the place recognisable."`
  - `SETTING_TEXT["reinvent"] = "The setting is yours to reinvent in the painter's own world: keep the people and what they are doing, but repaint or transform the place around them freely."`
  - `PEOPLE = "Keep the people, their faces, expressions, poses and clothing recognisably as in the photograph."`
  - `RESTRAINT = "Add nothing that is not there (no props, costumes, frames, borders, signatures or text), and nothing cute, glossy or theatrical: it should look like a real painting in a museum."`
  - `SHAPE_TEXT = {"fill": "Paint it as a wide canvas, extending the scene naturally to the left and right; no borders.", "keep": ""}`
  - `RETOUCH = "Keep this painting as it is, the same painter, people, composition and finish, changing only what follows."`
  - Default painters (slug, name, suits, manner):
    - `sargent`, "John Singer Sargent", ("portrait",): "Fluid, confident strokes over warm dark grounds, with quick bright highlights on faces and fabric."
    - `monet`, "Claude Monet", ("portrait", "scene"): "Broken, visible strokes of high-key colour and shimmering outdoor light."
    - `homer`, "Winslow Homer", ("portrait", "scene"): "Direct, sturdy handling, clear natural light and a restrained, earthy palette."
    - `hopper`, "Edward Hopper", ("portrait",): "Flat, simplified planes, strong raking light and a quiet, still mood."
    - `dali`, "Salvador Dalí", ("scene",): "His surrealist manner, with his precise, luminous technique, deep dreamlike perspective and clear Catalan light. Let one or two quiet, strange touches grow out of the scene itself (stretched shadows, an uncanny horizon, a subtly impossible landscape) rather than pasted-on symbols: no melting clocks, elephants, ants, crutches or moustaches."
- Routes: `GET /studio/painters`, `POST /studio/painters` (add), `POST /studio/painters/{slug}` (edit), `POST /studio/painters/{slug}/delete`. A painter whose name is typed in the Studio but not on the list is used with an empty manner and never saved to the list.

- [ ] **Step 1: Write the failing tests:**
  - `build_prompt("Edward Hopper", "Flat planes.", "keep", "fill", ["warmer light"])` starts with `"Paint this photograph as Edward Hopper would"`, contains `RESTRAINT` and `SHAPE_TEXT["fill"]`, ends with `"Also: warmer light."`; the `"reinvent"` text replaces the `"keep"` one; `"keep"` shape adds nothing.
  - `retouch_prompt(["simpler background", "less desert, more beach"])` contains both nudges and `RESTRAINT`.
  - A fresh store has the five painters; `suggested(painters, "scene")` starts with Claude Monet, Salvador Dalí, Winslow Homer; removing all and reopening does not re-seed.
  - `add_painter("", ...)` and a duplicate name raise `ValueError`; suits outside `KINDS` raise.
  - Web: `/studio/painters` lists the five; add/edit/delete round-trip; a bad add shows the error with status 400.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.** Tables `painters(slug primary key, name unique, manner, suits)` in the main db.
- [ ] **Step 4: Run** the tests, full suite, ruff — expected PASS.
- [ ] **Step 5: Commit** `feat: Studio painter list and prompt builder`

### Task 3: Tries and the Studio page

**Files:**
- Modify: `src/frame_art/studio.py`, `src/frame_art/service.py` (job `paint`, `Service.studio()`), `src/frame_art/web.py`, `src/frame_art/templates/photos.html` (a "Studio" link per photo), `src/frame_art/templates/base.html` (nav "Studio" → `/studio`, after "Your photos"; the Studio page links to `/studio/painters`)
- Create: `src/frame_art/templates/studio.html`
- Test: `tests/test_studio.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `ImageProvider`, `plain_error` (Task 1); `build_prompt`, `retouch_prompt`, painters (Task 2); `PhotoStore.get`, `open_image` (photo sources).
- Produces:
  - `@dataclass Try(id: int, photo_id: str, painter: str, setting: str, shape: str, nudges: list[str], from_try: int | None, prompt: str, status: str, error: str | None, path: str | None, cost: float | None, made_at: float)`; `status` ∈ `("painting", "done", "failed")`.
  - `StudioStore.start_try(photo_id, painter, setting, shape, nudges, from_try) -> Try` (status painting, prompt filled in), `finish_try(id, im: Image.Image, cost)` (saves `<files_dir>/tries/<id>.jpg`, quality 95), `fail_try(id, error)`, `tries(photo_id) -> list[Try]` (newest first), `get_try(id)`, `prune(now: float) -> int` (deletes unsaved tries older than 14 days, files included; returns how many), `spent(photo_id) -> float`.
  - `def paint(store, photos: PhotoStore, provider: ImageProvider, try_id: int) -> Try`: input is the photo (`open_image`) or, with `from_try`, that try's image; aspect `"16:9"` for shape fill, else `openrouter.nearest_aspect(*photo.size)`; prompt from `start_try`. On `PaintFailed` it calls `fail_try(id, plain_error(e))` and returns the failed try (never raises for a model failure).
  - Job `paint` (args `{"try": id}`) in `ARG_JOBS`; no provider → the try fails with `"No OpenRouter key"`.
  - Routes: `GET /studio` (pick a photo: the Your photos grid, linking each to its Studio), `GET /studio/{photo_id}` (404 for an unknown photo), `POST /studio/{photo_id}/paint` (fields `painter` slug or `painter_name` free text, `setting`, `shape`, `nudge` (multi), `nudge_text`, `from_try`) → creates the try, submits `paint`, redirects 303 to `/studio/{photo_id}`, `GET /studio/tries/{id}.jpg` (the try image).
  - Page: painter select (suggested first) plus free-text painter, setting radios (default `keep` for portrait or unknown kind, `reinvent` for scene), shape radios (default `fill`), nudge checkboxes plus free text, "Paint" button; a "Try again from this" button per finished try (posts `from_try`); tries grid newest first with painter, setting, shape, nudges, cost; failed tries show their error; the page has `<meta http-equiv="refresh" content="5">` while any try is `painting`; header shows "Spent on this photo: $x.xx".

- [ ] **Step 1: Write the failing tests** (fake provider: records `(size, aspect, prompt)` and returns a 64×36 image with cost 0.12, or raises `PaintFailed("OpenRouter returned 402: …")`):
  - `paint` for a fill try asks aspect `"16:9"`, size `"4K"`; for a keep try on a 30×40 photo asks `"3:4"`; the saved try is `done` with a file and cost 0.12; `spent` sums done tries.
  - `paint` from an earlier try sends that try's image and a prompt starting with `RETOUCH`.
  - A 402 leaves the try `failed`, error `"OpenRouter: balance too low"`, cost None, no file; `spent` unchanged.
  - `prune` removes a 15-day-old unsaved try and its file, keeps a 13-day-old one (and keeps a saved one, once Task 4 exists — add that assertion there).
  - Web: POST paint queues exactly one `paint` job (fake `svc.submit`) and answers 303; GET after it shows the refresh meta; with a failed try the page shows "OpenRouter: balance too low" and no refresh; a scene photo's page has `reinvent` checked; unknown photo → 404; the page never contains the key planted at `keys/openrouter/key`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.** Tables `tries(...)` in the main db, files under `<data>/studio/`. The nightly job calls `prune` (one line in `jobs.nightly`).
- [ ] **Step 4: Run** tests, full suite, ruff — expected PASS.
- [ ] **Step 5: Try the fill shape for real, early** (spec: untested). On the demo server (as in photo-sources Task 7), paint one portrait and one scene from Brian's demo photos with shape fill, show Brian, and ledger his verdict. If the extended canvas is poor, rule on the default (keep) and record it; that changes only the default radio.
- [ ] **Step 6: Commit** `feat: Studio tries: paint, nudge and repaint`

### Task 4: Saving and the collection

**Files:**
- Modify: `src/frame_art/studio.py`, `src/frame_art/web.py`, `src/frame_art/templates/studio.html`, `src/frame_art/templates/photos.html`
- Create: `src/frame_art/templates/paintings.html`
- Test: `tests/test_studio.py`, `tests/test_web.py`

**Interfaces:**
- Produces:
  - `@dataclass Painting(id: int, try_id: int, photo_id: str | None, painter: str, prompt: str, path: str, cost: float | None, saved_at: float)`.
  - `StudioStore.save(try_id) -> Painting`: copies the try's file byte for byte to `<files_dir>/paintings/<id>.jpg` (no model call); `ValueError` for a try that isn't done; saving the same try twice returns the existing painting. `paintings() -> list[Painting]` (newest first), `get_painting(id)`, `remove_painting(id) -> bool` (row and file).
  - `StudioStore.forget_photo(photo_id)`: deletes that photo's tries (files too) and sets `photo_id = None` on its paintings. `delete_photo` in web calls it.
  - Routes: `POST /studio/tries/{id}/save` → redirect to the Studio with notice "Saved to Portraits."; `GET /portraits` (the collection: thumbnails, painter, cost, date, "Remove"); `POST /portraits/{id}/remove`; `GET /portraits/{id}.jpg`. Nav gets "Portraits" next to "Studio".

- [ ] **Step 1: Write the failing tests:**
  - `save` of a done try gives a painting whose file bytes equal the try's; a fake provider records zero calls during `save`; a second `save` returns the same id; a failed try raises `ValueError`.
  - `prune` keeps a 15-day-old try that was saved (and its painting file).
  - Deleting a photo through `POST /photos/{id}/delete` removes its tries and files, keeps its paintings (photo_id None), and `/studio/{id}` then answers 404.
  - `/portraits` lists saved paintings; remove deletes row and file (TV side is Task 6).
  - None of `/`, `/catalogue`, `/pool`, `/collection`, `/variants`, `/history` contains a painting's painter or file name.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, full suite, ruff — expected PASS.
- [ ] **Step 5: Commit** `feat: save Studio tries as portraits`

### Task 5: The Portraits share in the schedule editor

**Files:**
- Modify: `src/frame_art/schedules.py` or `src/frame_art/settings.py` (the type set), `src/frame_art/web.py` (`variants_view`, `schedule_from_form`), `src/frame_art/variants.py` (`nightly`, `progress`), `src/frame_art/templates/variants.html`
- Test: `tests/test_schedules.py`, `tests/test_variants.py`, `tests/test_web.py`

**Interfaces:**
- Produces:
  - `PORTRAITS = "portraits"` in `schedules.py`, and `def share_types(cat) -> dict[str, str]` in `variants.py`: the variant types' `slug -> name` plus `{"portraits": "Portraits"}` (last). Every place that builds the slug set for schedules (`schedule_from_form`, `variants_view`, `Settings.today_shares` callers, `variants.nightly`, `variants.progress`, `service` line using `today_shares`) uses it.
  - `variants.nightly(..., portraits: int = 0)`: allocates over `len(live) + portraits` with `ready["portraits"] = portraits`, then drops `portraits` from the target before `assign_slots`. `progress` leaves Portraits out of the per-type rows (it has no versions).
  - Timeline and schedule list show "Portraits N%" like any type; the share row reads "Portraits (saved Studio paintings)".

- [ ] **Step 1: Write the failing tests:**
  - A schedule saved with `share-portraits=20` keeps it; `schedules.known(..., share_types)` keeps `portraits`.
  - `variants.nightly` with `portraits=0` gives the same slots as before for a Halloween 50% schedule (regression); with a layer `{"portraits": 20, "halloween": 50}`, 100 live works and `portraits=25`, Halloween gets 62 slots (`round(50 * 125 / 100)`: half of everything on screen, all on museum works) and no slot is typed `portraits`.
  - The variants page lists "Portraits" in the share rows and in the schedule list.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, full suite, ruff — expected PASS.
- [ ] **Step 5: Commit** `feat: a Portraits share in the schedules`

### Task 6: Portraits on the TV

**Files:**
- Create: `src/frame_art/portraits.py`
- Modify: `src/frame_art/jobs.py` (nightly step), `src/frame_art/publish.py` (museum paths skip portraits), `src/frame_art/variants.py` (`_live`, `apply_versions`), `src/frame_art/service.py` (`now_showing`, job `portraits`), `src/frame_art/web.py` (pool/history pages, remove → job)
- Test: `tests/test_portraits.py` (new), `tests/test_publish.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `StudioStore.paintings()` (Task 4), `PORTRAITS` and the Task 5 allocation, `Publisher._retire_files`, `Catalogue.record_publish`.
- Produces, in `portraits.py`:
  - `PREFIX = "portrait:"`; `def is_portrait(key: str) -> bool`; `def key_for(painting_id: int) -> str`.
  - `def target(share_pct: float, museum_live: int, saved: int) -> int`: `saved` when `share_pct >= 100`, else `min(saved, round(museum_live * f / (1 - f)))` with `f = share_pct / 100`; 0 when either is 0.
  - `def sync(cat, store, pub: Publisher, dest: Path, want: int, seed: int) -> dict`: keeps `want` paintings live. Retires live portrait files whose painting is gone first, then the longest-live ones beyond `want`; adds paintings not live, least recently shown first (`cat.published()` history), ties by `seed`. A new file is the painting rendered to the panel: shape fill → `imaging.plan_for` + `imaging.render(im, plan, pub.pad_style)`; a non-16:9 painting → `pad_style "matte"`, folder `matted`. Filename `portrait-<id>.jpg` via `cat.unused_filename`, recorded with `record_publish(name, folder, key_for(id), phash)`. Returns `{"live": n, "added": [...], "retired": [...]}`.
  - `jobs.nightly` runs, after `publish` and before variants: `want = target(shares_for(today)["portraits"], museum_live, len(saved))`, `sync(...)`, and passes `portraits=want` to `variants.nightly`; summary gains `"portraits"`.
  - Job `portraits` (no args, in `JOB_KINDS`: "Update portraits on the TV") runs the same `sync` with today's target; `POST /portraits/{id}/remove` submits it.
  - Museum paths ignore portrait keys: `Publisher._retire` (pool count and doomed list), `variants._live`, `variants.apply_versions`, `jobs` "live" counts, the pool page and `service` line 309. `Publisher._write_meta` already skips them (no catalogue work) — keep it that way.
  - `now_showing` for a portrait file: `{"ours": True, "kind": "portrait", "title": "Portrait", "file": ...}` and nothing else from the painting (no painter, no photo name). History lists it as "Portrait".

- [ ] **Step 1: Write the failing tests:**
  - `target(20, 150, 100) == 38`, `target(20, 150, 10) == 10`, `target(0, 150, 10) == 0`, `target(100, 150, 7) == 7`, `target(20, 150, 0) == 0`.
  - `sync` with 3 saved and `want=2` publishes 2 files named `portrait-<id>.jpg` in `full/`; a 3:4 painting lands in `matted/`; `want=1` next retires the longest-live one; removing a live painting from the store and syncing retires its file and records its TV id from the sidecar (`pending(cat, dest)` lists it).
  - With 150 museum works live and pool 150, a nightly publish with 2 portraits live retires no museum work beyond its usual count (the pool count ignores portraits); `variants.apply_versions` does not touch portrait files.
  - `_meta/index.json` has no portrait entry; `now_showing` for a portrait's content id gives title "Portrait" and no painter; `/history` shows "Portrait".
  - Review Focus 4: share 20% with 0 saved → `sync` publishes nothing and Halloween's slots are as without Portraits; share 100% → every saved painting is live, museum count unchanged.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests, full suite, ruff — expected PASS.
- [ ] **Step 5: End-to-end on the demo** with Brian: save two paintings, set a Portraits share, run the `portraits` job, see two `portrait-*.jpg` in the demo's `out/full`, remove one, see it retired. (The demo has no HA; nothing reaches the TV.)
- [ ] **Step 6: Commit** `feat: saved portraits rotate onto the TV at their share`

### Task 7: Release 0.11.0

**Files:** `pyproject.toml`, `.release-please-manifest.json`, `uv.lock`; homelab-stacks `frame-art/compose.yaml` (pin only); `docs/google-photos.md` (server step)

- [ ] **Step 1:** Merge `photo-sources` (both plans) to `main` after the final whole-branch review; full suite and ruff green on `main`.
- [ ] **Step 2:** Bump to 0.11.0 in the three files, commit `chore: release 0.11.0`, annotated tag `v0.11.0`, push; `gh run watch` the publish-image run; verify `0.11.0`, `0.11`, `latest` and `sha-<short>` share one digest (`docker buildx imagetools inspect`).
- [ ] **Step 3:** homelab-stacks PR changing the pin to `0.11.0@sha256:<digest>`. The body names every touched module, cites the route-table diff (new `/photos…`, `/studio…`, `/portraits…` routes), says no HA change and no dropped env var, notes base-image drift if any, and says Brian must place `keys/google-photos/client.json` (chmod 600) on the server for Google Photos; the token is written under the data volume.
- [ ] **Step 4:** Send the PR to proxmox. Deploy only on Brian's `human="true"` go-ahead (a relay from me does not count). After deploy, check `/photos`, `/studio`, `/portraits` on the live app and the next nightly run's summary.
