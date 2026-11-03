# Photo sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Brian can bring his own photos into frame-art, by upload or from Google Photos via the Picker. Each photo is sorted as a portrait or a scene, and the photos are kept private and apart from the museum catalogue.

**Architecture:** A source registry describes every source: the existing crawlers (crawled, public) and two new picked, private ones (`upload`, `google-photos`). Personal photos live in their own `photos` table and `data/photos/` folder, never in `works`, so crawl, publish, retire and the catalogue pages never see them. A "Your photos" page lists them. A cheap vision call suggests each photo's kind, which Brian can override. Google sign-in uses a desktop OAuth client with PKCE and a paste-back of the redirect URL. Picking uses the Photos Picker API.

**Tech Stack:** Python 3.12, Starlette/Jinja, SQLite, httpx (through `net.Http`), Pillow, pytest, ruff 0.16.1.

**Spec:** `docs/superpowers/specs/2026-11-02-portrait-studio-design.md` (goals 1–3 and 7's "photo services" half; the "Your photos" page and kind suggestion). The Studio, the style list and the Portraits share are later plans.

## Global Constraints

- Personal photos never enter the `works` table, the public catalogue pages, crawls, notability, publish or retirement, and never get written under the output folder (`svc.dest`).
- Photos are served only to the LAN web UI, by photo id, never by a path taken from the request.
- Keys: the Google OAuth client is a file Brian saves at `<keys dir>/google-photos/client.json` (chmod 600). The keys dir is `$FRAME_ART_KEYS`, mounted read-only in the container. Tokens go to `<data>/google-photos/token.json`, chmod 600. Never log, print or render a key, client secret, token or auth code. Never send one on the bus.
- Google scope: `https://www.googleapis.com/auth/photospicker.mediaitems.readonly` only.
- The Google app stays in "testing" status. An expired or revoked sign-in shows "Sign in again", not an error page.
- Kind values are exactly `"portrait"` and `"scene"`. A photo whose suggestion failed has kind `None` and is shown as "Choose a kind".
- Vision model for the kind suggestion: `google/gemini-3.1-flash-lite` via OpenRouter.
- No Home Assistant change, no new dependency, no compose change.
- Match the surrounding code: comment density, naming, the `# fmt: skip` idiom, inline JS without semicolons.

## Review Focus

1. **The same photo picked twice** (re-picking an album, or uploading a photo already brought in from Google) is skipped, not duplicated. That holds by Google id and by file content (sha256). → Task 2.
2. **A non-image or oversized upload** (a .mov from a phone, a 200 MB file, a file that isn't really a JPEG) is refused with a message naming the file, and the rest of the batch still comes in. → Task 3.
3. **The kind suggestion fails or returns junk** (402 when credit is low, a timeout, text that isn't JSON, a kind outside the two values). The photo is still added, with kind `None`, and the run log names the reason. → Task 4.
4. **The Google sign-in has expired** (testing-mode 7-day expiry, `invalid_grant`). The page offers "Sign in again", and a pick never 500s. → Task 6.
5. **Brian closes the picker without choosing anything, or the session times out.** The page returns to normal, no import job runs, and the session is deleted. → Task 7.

---

### Task 1: Source registry, and correct the stale design note

**Files:**
- Modify: `src/frame_art/sources/__init__.py`
- Modify: `docs/design.md` (the `www/frame_upload` and `/local/` passages, lines ~38, 84–110, 134–137, 168, 245)
- Test: `tests/test_sources.py` (new)

**Interfaces:**
- Produces: `SourceInfo(name: str, label: str, mode: Literal["crawled", "picked"], private: bool, needs_login: bool)` (frozen dataclass) and `SOURCE_INFO: dict[str, SourceInfo]`, covering every name in `settings.SOURCES` plus `"upload"` and `"google-photos"`. Also `def info(name: str) -> SourceInfo` (KeyError for an unknown name).

- [ ] **Step 1: Write the failing test**

```python
from frame_art.settings import SOURCE_NAMES, SOURCES
from frame_art.sources import SOURCE_INFO, all_sources, info


def test_every_crawler_is_crawled_and_public(tmp_path):
    for src in all_sources(tmp_path, ("painting", "drawing", "print", "photograph")):
        i = info(src.name)
        assert (i.mode, i.private) == ("crawled", False)
    assert {n for n in SOURCES} <= set(SOURCE_INFO)
    assert info("met").label == SOURCE_NAMES["met"]


def test_personal_sources_are_picked_and_private():
    assert info("upload") == SOURCE_INFO["upload"]
    assert (info("upload").mode, info("upload").private, info("upload").needs_login) == ("picked", True, False)
    g = info("google-photos")
    assert (g.mode, g.private, g.needs_login, g.label) == ("picked", True, True, "Google Photos")
```

- [ ] **Step 2: Run it**: `uv run pytest tests/test_sources.py -q`. Expected: FAIL on the import of `SOURCE_INFO`.
- [ ] **Step 3: Implement** `SourceInfo`, `SOURCE_INFO` and `info` in `sources/__init__.py`. Crawler labels come from `settings.SOURCE_NAMES`. The `upload` label is "Uploaded". The crawlers' own code stays unchanged.
- [ ] **Step 4: Run it**: expected PASS. Then run the full suite with `uv run pytest -q`: all pass.
- [ ] **Step 5: Correct `docs/design.md`.** The output folder is `/config/frame_art_pool` (outside `www`, not web-served). The retire list reaches HA as attributes of the MQTT `sensor.frame_art`. Keep the history short: one line saying the `www/frame_upload` draft was replaced before deploy.
- [ ] **Step 6: Commit**: `feat: source registry (crawled vs picked, public vs private); fix stale output-folder note`

### Task 2: The photo store

**Files:**
- Create: `src/frame_art/photos.py`
- Test: `tests/test_photos.py` (new)

**Interfaces:**
- Produces:
  - `Photo` (dataclass) with fields `id: str`, `source: str`, `source_id: str`, `sha256: str`, `filename: str`, `path: str`, `width: int`, `height: int`, `added: float`, `kind: str | None`, `kind_reason: str | None`, `kind_auto: bool`.
  - `PhotoStore(db_path: Path, files_dir: Path)`, using the same SQLite file as `Catalogue` (`<data>/frame-art.db`) and a new table `photos`. Files go under `<data>/photos/`. Methods:
    - `add(source: str, source_id: str, data: bytes, filename: str) -> Photo | None`. Returns `None` when `(source, source_id)` or the sha256 already exists.
    - `all() -> list[Photo]`, newest first.
    - `get(id: str) -> Photo | None`.
    - `set_kind(id: str, kind: str | None, reason: str | None, auto: bool) -> None`. Raises ValueError for a kind outside `None | "portrait" | "scene"`.
    - `remove(id: str) -> bool`, which deletes the row and the file.
  - Also `def open_image(p: Photo) -> PIL.Image.Image`, with EXIF orientation applied.

- [ ] **Step 1: Write the failing tests**

```python
def jpeg(color="red", size=(40, 30)) -> bytes: ...  # tiny JPEG via Pillow

def test_add_get_and_dedupe(tmp_path):
    st = PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")
    p = st.add("upload", "a.jpg", jpeg(), "a.jpg")
    assert p and (p.width, p.height, p.kind) == (40, 30, None)
    assert Path(p.path).is_file() and Path(p.path).parent == tmp_path / "photos"
    assert st.add("upload", "a.jpg", jpeg("blue"), "a.jpg") is None          # same source id
    assert st.add("google-photos", "g1", jpeg(), "IMG_1.jpg") is None        # same bytes
    assert [x.id for x in st.all()] == [p.id]

def test_kind_and_remove(tmp_path):
    st = PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")
    p = st.add("upload", "a", jpeg(), "a.jpg")
    st.set_kind(p.id, "scene", "2 people, water", True)
    assert (st.get(p.id).kind, st.get(p.id).kind_auto) == ("scene", True)
    with pytest.raises(ValueError):
        st.set_kind(p.id, "landscape", None, False)
    assert st.remove(p.id) and not Path(p.path).exists() and st.get(p.id) is None

def test_photos_never_reach_the_catalogue(tmp_path):
    st = PhotoStore(tmp_path / "frame-art.db", tmp_path / "photos")
    st.add("upload", "a", jpeg(), "a.jpg")
    assert Catalogue(tmp_path / "frame-art.db").works() == []

def test_exif_orientation_is_applied(tmp_path):
    # a 40x30 JPEG saved with EXIF Orientation=6 (rotate 90) opens as 30x40
    ...
```

- [ ] **Step 2: Run it**: `uv run pytest tests/test_photos.py -q`. Expected: FAIL, because `frame_art.photos` doesn't exist.
- [ ] **Step 3: Implement** `photos.py`.
  - The file name on disk is `<id>.<ext>`, where `id = secrets.token_hex(8)` and the extension comes from the image format Pillow detects. The user's filename is never used as a path.
  - `width`/`height` are measured after EXIF orientation.
  - The table is created with `create table if not exists`, the way `catalogue.py` creates its tables.
- [ ] **Step 4: Run it**: expected PASS. Then the full suite: all pass.
- [ ] **Step 5: Commit**: `feat: private photo store (own table and folder, deduped by id and content)`

### Task 3: Upload source and the "Your photos" page

**Files:**
- Modify: `src/frame_art/web.py` (routes, handlers), `src/frame_art/service.py` (`Service.photos() -> PhotoStore`)
- Create: `src/frame_art/templates/photos.html`
- Modify: `src/frame_art/templates/base.html` (nav: add `("photos", "Your photos")` after `("collection", "Collection")`), `src/frame_art/static/style.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `PhotoStore`, `Photo`, `open_image` (Task 2); `info()` (Task 1).
- Produces these routes:
  - `GET /photos`
  - `POST /photos/upload` (multipart field `files`, one or more)
  - `GET /photos/{id}/thumb` (a 480px JPEG, cached under `<data>/thumbs/photos/`)
  - `POST /photos/{id}/kind` (form field `kind` = `portrait` | `scene`, which sets `auto=False`)
  - `POST /photos/{id}/delete`
- Constant `MAX_UPLOAD = 50 * 1024 * 1024`. Accepted formats: JPEG, PNG and WebP, as detected by Pillow, not by extension.

- [ ] **Step 1: Write the failing tests** (they use the existing `client` fixture):

```python
def test_upload_lists_and_dedupes(client):
    c, svc = client
    r = c.post("/photos/upload", files=[("files", ("a.jpg", jpeg(), "image/jpeg")),
                                        ("files", ("b.jpg", jpeg(), "image/jpeg"))])
    assert r.status_code == 200 and "Added 1" in r.text and "already here" in r.text
    [p] = svc.photos().all()
    page = c.get("/photos").text
    assert f'src="/photos/{p.id}/thumb"' in page and "Choose a kind" in page

def test_upload_refuses_non_images_and_keeps_the_rest(client):
    c, svc = client
    r = c.post("/photos/upload", files=[("files", ("clip.mov", b"\x00\x00\x00 ftypqt", "video/quicktime")),
                                        ("files", ("a.jpg", jpeg(), "image/jpeg"))])
    assert "clip.mov" in r.text and "not a photo" in r.text
    assert len(svc.photos().all()) == 1

def test_kind_override_and_delete(client):
    c, svc = client
    c.post("/photos/upload", files=[("files", ("a.jpg", jpeg(), "image/jpeg"))])
    [p] = svc.photos().all()
    c.post(f"/photos/{p.id}/kind", data={"kind": "portrait"})
    assert svc.photos().get(p.id).kind == "portrait" and not svc.photos().get(p.id).kind_auto
    assert c.post(f"/photos/{p.id}/kind", data={"kind": "nope"}).status_code == 400
    assert c.post(f"/photos/{p.id}/delete").status_code == 200 and svc.photos().all() == []
    assert c.get(f"/photos/{p.id}/thumb").status_code == 404

def test_photos_stay_off_public_pages(client):
    c, svc = client
    c.post("/photos/upload", files=[("files", ("secret-name.jpg", jpeg(), "image/jpeg"))])
    for page in ("/", "/catalogue", "/pool", "/collection", "/variants"):
        assert "secret-name" not in c.get(page).text
    assert not svc.dest.exists() or not any(svc.dest.rglob("*"))
```

  The oversize case uses `monkeypatch.setattr(web, "MAX_UPLOAD", 100)` and asserts "too large" plus the filename.
- [ ] **Step 2: Run them**: `uv run pytest tests/test_web.py -q -k photos`. Expected: FAIL with 404.
- [ ] **Step 3: Implement** the routes and `photos.html`. The page has three parts:
  - an upload form;
  - a "Google Photos" box: a "Set up" link (to `docs/google-photos.md` text, Task 6) when `client.json` is missing, otherwise "Sign in" or "+ Add from Google Photos";
  - one card per photo: thumb, filename, source label, added date, a kind control (Portrait/Scene buttons, with the reason in muted text when `kind_auto`, or "Choose a kind" when `None`), and a Delete form.
  After an upload, the handler submits the `classify-photos` job (Task 4) when any photo was added. Until Task 4 lands, `Service.submit` must accept the kind, so add `"classify-photos": "Suggest portrait or scene for new photos"` to `JOB_KINDS` in this task and have `_do` return `{"classified": 0}`.
- [ ] **Step 4: Run them**: expected PASS. Then the full suite and `uvx ruff@0.16.1 check . && uvx ruff@0.16.1 format --check .`.
- [ ] **Step 5: Commit**: `feat: "Your photos" page with upload, kind and delete`

### Task 4: Kind suggestion

**Files:**
- Modify: `src/frame_art/openrouter.py` (add `describe`)
- Create: `src/frame_art/photo_kind.py`
- Modify: `src/frame_art/service.py` (`_do` for `classify-photos`)
- Test: `tests/test_openrouter.py`, `tests/test_photo_kind.py` (new)

**Interfaces:**
- Consumes: `PhotoStore.all/set_kind`, `open_image` (Task 2).
- Produces:
  - `openrouter.describe(http, api_key: str, im: Image.Image, prompt: str, model: str) -> str`. It returns the reply text, sends the image downscaled to at most 1024px, and raises `EditFailed` with the same error-message handling as `edit_image` (so a 402 reads "OpenRouter returned 402: …").
  - `photo_kind.VISION_MODEL = "google/gemini-3.1-flash-lite"`.
  - `photo_kind.suggest(http, api_key, im) -> tuple[str, str]`, returning `(kind, reason)`. It raises `ValueError` on a reply that isn't JSON with `kind` in `{"portrait", "scene"}`, and trims the reason to 80 characters.
  - `photo_kind.classify_new(store: PhotoStore, http, api_key) -> dict` returns `{"classified": n, "failed": [{"id": ..., "error": ...}]}`. It runs only for photos with `kind is None and kind_auto is False` and never overwrites one that Brian set.

- [ ] **Step 1: Write the failing tests.**
  - `describe` sends a `messages` list with an `image_url` part and returns `choices[0].message.content` (use the `FakeHttp` already in `test_openrouter.py`).
  - `suggest` parses `{"kind": "scene", "reason": "2 people, water, outdoors"}`. It also parses the same object wrapped in a ```json fence. It raises on `{"kind": "landscape"}` and on plain prose.
  - `classify_new`:
    - sets an auto kind on new photos;
    - skips a photo Brian has already set (`kind_auto=False`, `kind="portrait"`);
    - when `describe` raises `EditFailed("OpenRouter returned 402: …")` for one photo, leaves that photo at `None`, lists it in `failed` with the message, and still classifies the others.
- [ ] **Step 2: Run them**: expected FAIL, because `describe` and `photo_kind` don't exist yet.
- [ ] **Step 3: Implement.** The prompt asks for JSON only. It defines **portrait** as one or more faces filling much of the frame, and **scene** as people small within a larger setting. It asks for a reason of at most 8 words, for example "close portrait, 1 person" or "2 people, water, outdoors". `_do("classify-photos")` reads the key with `keys.read_key("openrouter")`. With no key it returns `{"classified": 0, "note": "no OpenRouter key"}`. A non-empty `failed` makes the run "partial" through the existing mechanism.
- [ ] **Step 4: Run them**: expected PASS. Then the full suite.
- [ ] **Step 5: Commit**: `feat: suggest portrait or scene for new photos`

### Task 5: Verify the Google sign-in flow by hand, before writing it

**Files:**
- Create: `docs/google-photos.md` (setup steps for Brian)

This task is a check with Brian, not code. It confirms that the flow the next two tasks build works with Google today.

- [ ] **Step 1: Write `docs/google-photos.md`.**
  1. Create a Google Cloud project.
  2. Enable the "Photos Picker API".
  3. Set up the OAuth consent screen: External, Testing, with Brian's account as a test user.
  4. Create a credential: OAuth client ID, type **Desktop app**. Download the JSON.
  5. Save it as `<keys dir>/google-photos/client.json` and `chmod 600` it. On the server that's the stack's `keys/` directory, so `/keys/google-photos/client.json` in the container.

  Then explain the sign-in: after approving, the browser lands on a page that fails to load at `http://127.0.0.1…`. That's expected. Copy that page's full address and paste it into frame-art.
- [ ] **Step 2: Ask Brian** (in the terminal, or on the bus as a request through hub) to do steps 1–5, and confirm that `client.json` exists with `test -s`. Don't read it.
- [ ] **Step 3: Run a throwaway script** from the scratchpad, never committed, that:
  1. builds the auth URL with PKCE, `redirect_uri=http://127.0.0.1:8765`, scope as in Global Constraints, `access_type=offline` and `prompt=consent`;
  2. has Brian open the URL, approve, and paste back the address;
  3. exchanges the code at `https://oauth2.googleapis.com/token`;
  4. creates a Picker session (`POST https://photospicker.googleapis.com/v1/sessions`);
  5. prints only the `pickerUri` host and the session's keys.

  Expected: a refresh token is returned and the session has `id`, `pickerUri` and `pollingConfig`. If the desktop-client loopback flow is refused, stop: record what Google said and bring it to Brian before building Task 6. Print no token or code.
- [ ] **Step 4: Have Brian pick 1–2 photos through the `pickerUri`.** Confirm that `GET /v1/sessions/{id}` reports `mediaItemsSet: true` and that `GET /v1/mediaItems?sessionId=…` lists items with `mediaFile.baseUrl`. Check whether picking a whole album is offered, and write the answer in `docs/google-photos.md`.
- [ ] **Step 5: Commit** the doc: `docs: Google Photos setup for frame-art`

### Task 6: Google sign-in

**Files:**
- Create: `src/frame_art/google_photos.py`
- Modify: `src/frame_art/web.py`, `src/frame_art/templates/photos.html`
- Test: `tests/test_google_photos.py` (new), `tests/test_web.py`

**Interfaces:**
- Consumes: `keys.keys_dir()`; what Task 5 verified.
- Produces:
  - In `google_photos.py`:
    - `SCOPE`, `REDIRECT = "http://127.0.0.1:8765"`.
    - `class SignInNeeded(Exception)`.
    - `client_path() -> Path` (`keys_dir() / "google-photos" / "client.json"`) and `has_client() -> bool`.
    - `auth_url(client: dict, state: str, challenge: str) -> str`.
    - `exchange(http, client: dict, pasted: str, verifier: str, state: str, token_path: Path) -> None`. `pasted` is the full address or the bare code. It raises `ValueError` on a `state` mismatch or a missing code, and writes the token file with mode 0o600.
    - `access_token(http, client: dict, token_path: Path) -> str`. It refreshes when expired, and raises `SignInNeeded` when there's no token file or Google answers `invalid_grant`.
    - `signed_in(token_path) -> bool`.
  - Routes:
    - `GET /photos/google/signin`: shows the link and a paste box. The PKCE verifier and state are held server-side in `cat.put("google_signin", …)` and expire after 15 minutes.
    - `POST /photos/google/signin` (field `pasted`).
    - `POST /photos/google/signout`: deletes the token file.

- [ ] **Step 1: Write the failing tests** (FakeHttp, as in `test_openrouter.py`):
  - `auth_url` contains `code_challenge_method=S256`, the scope and the redirect, and no secret.
  - `exchange` accepts `http://127.0.0.1:8765/?state=S&code=C&scope=…` and also the bare `C`. It posts `code_verifier`, and writes a file whose mode is `0o600`. It raises on `state=X`.
  - `access_token` returns the stored token while it's valid. It refreshes and rewrites the file when expired, and raises `SignInNeeded` on a 400 with `{"error": "invalid_grant"}`.
  - Web: with no `client.json`, `/photos` shows "Set up Google Photos". With a client but no token, it shows "Sign in". A POST with the wrong state shows "That sign-in didn't match" (status 400). No page ever contains the client secret: plant `"client_secret": "SEKRIT"` in the test client file and assert `"SEKRIT" not in` every response.
- [ ] **Step 2: Run them**: expected FAIL.
- [ ] **Step 3: Implement.** Use `Http.request` for the token calls. Parse the redirect with `urllib.parse`.
- [ ] **Step 4: Run them**: expected PASS. Then the full suite and ruff.
- [ ] **Step 5: Commit**: `feat: Google Photos sign-in (desktop client, PKCE, paste-back)`

### Task 7: Picking from Google Photos

**Files:**
- Modify: `src/frame_art/google_photos.py`, `src/frame_art/web.py`, `src/frame_art/service.py`, `src/frame_art/templates/photos.html`
- Test: `tests/test_google_photos.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `access_token`, `SignInNeeded` (Task 6); `PhotoStore.add` (Task 2); the `classify-photos` job (Task 4).
- Produces:
  - In `google_photos.py`:
    - `PickSession(id: str, picker_uri: str, expires: float)`.
    - `create_session(http, token) -> PickSession`.
    - `session_done(http, token, id) -> bool`.
    - `picked_items(http, token, id) -> list[dict]`, which pages through `mediaItems` and keeps `type == "PHOTO"`.
    - `download(http, token, item) -> bytes`, which fetches `baseUrl + "=d"` with an `Authorization` header.
    - `delete_session(http, token, id) -> None`.
  - Routes:
    - `POST /photos/google/pick` creates a session, stores it as `cat.put("google_pick", …)`, and redirects to `/photos`.
    - While a pick is pending, `GET /photos` shows "Open Google Photos" (the `pickerUri`, `target="_blank"`) and "Cancel", and refreshes itself every 5 s. On each load it checks `session_done` once. When it's done, it submits job `import-google` and clears the pending pick. When the pick has expired or been cancelled, it deletes the session and clears it, and no import runs.
    - `POST /photos/google/cancel`.
  - Job `import-google` (args `{"session": id}`) downloads each item, calls `store.add("google-photos", item["id"], data, item["mediaFile"]["filename"])`, then deletes the session and submits `classify-photos`. It returns `{"added": n, "skipped": m, "failed": [...]}`.

- [ ] **Step 1: Write the failing tests:**
  - `picked_items` follows `nextPageToken` and drops a `VIDEO` item.
  - `download` asks for `…=d` and sends the bearer header.
  - The `import-google` job:
    - adds two new photos, and skips one that's already in the store (same Google id);
    - calls `DELETE` on the session;
    - names a failed download in `failed` and still adds the rest.
  - Web:
    - With a pending pick and `session_done → False`, the page shows "Open Google Photos" and a refresh.
    - With `→ True`, it submits `import-google`; assert with a fake `svc.submit`.
    - An expired pick is cleared with no submit.
    - `SignInNeeded` during a pick shows "Sign in again", status 200.
- [ ] **Step 2: Run them**: expected FAIL.
- [ ] **Step 3: Implement**, following the paging and session handling that Task 5 confirmed.
- [ ] **Step 4: Run them**: expected PASS. Then the full suite and ruff.
- [ ] **Step 5: Try it end to end with Brian** on the demo server at `http://192.168.1.21:<port>/photos`, using the real `client.json`: sign in, pick 2 photos, see them arrive with kinds. Delete them after.
- [ ] **Step 6: Commit**: `feat: add photos from Google Photos through the Picker`

### Task 8: Release

- [ ] **Step 1:** Run the full suite and ruff check/format on main after merging.
- [ ] **Step 2:** Bump to 0.11.0 in `pyproject.toml`, `.release-please-manifest.json` and `uv.lock`. Commit `chore: release 0.11.0`, tag `v0.11.0`, push, watch `publish-image.yml`, and verify that 0.11.0, 0.11, latest and sha- share one digest.
- [ ] **Step 3:** Open the homelab-stacks PR (pin `0.11.0@sha256:…`). The body names every touched module and gives the route-table diff (count the `Route(` entries before and after; the new paths are `/photos`, `/photos/upload`, `/photos/{id}/thumb`, `/photos/{id}/kind`, `/photos/{id}/delete`, `/photos/google/signin` (GET and POST), `/photos/google/signout`, `/photos/google/pick`, `/photos/google/cancel`). It also states:
  - the new `photos` table and `<data>/photos/` folder;
  - no HA change, no env or compose change;
  - that Brian puts `client.json` in the stack's `keys/google-photos/` himself;
  - whether the base image drifted.
- [ ] **Step 4:** Send the PR to proxmox for the digest check. Deploy only on Brian's word, relayed as human="true" or given directly.
