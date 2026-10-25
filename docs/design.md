# Frame Art Fetcher: design draft (v0)

Status: draft for Brian's review. Nothing in Home Assistant or on the TV has been changed.

## Facts checked (read-only, 2026-10-25)
- HA is **Home Assistant Container** (core-2026.9.1) on a Proxmox VM at 192.168.1.73, with config at `/config`.
  That means no add-ons; the fetcher runs as a sibling container or somewhere else.
- The `samsungtv_smart` services we need exist: `art_upload_batch` (uploads every jpg/png in a folder,
  skips unchanged files using a hidden sidecar, has a `throttle` and a `dedup` option, returns content_ids),
  `art_delete` (MY-* only), `art_available` (MY-C0002 = My Photos), `art_set_slideshow`
  (category 2 = My Photos), and `art_get_current`.
- Default matte for uploads is `shadowbox_polar`. For full-bleed 16:9 art we pass `matte_id: none`.

## Architecture

```
 [source adapters] -> catalogue (SQLite) -> filter -> download -> process -> publish
   museums/APIs        dedupe, metadata      PD,size,   original   crop/pad,   atomic write to
   seed lists                                type,AR               3840x2160   frame_upload/
                                                                          |
                         HA nightly automation: art_upload_batch -> art_delete retired -> slideshow
                                                                          |
                                                                  Frame TV (only HA talks to it)
```

- A single Python CLI (`frame-art`). Python because Pillow, numpy, imagehash and httpx cover all the
  image work. Packaged with uv and shipped as a Docker image.
- State lives in SQLite (`/data/frame-art.db`) in the fetcher's own volume:
  `works` (source, source_id, title, artist, date, medium, license, rights_url, image_url, w, h, phash,
  status, reject_reason), `published` (filename, work_id, published_at, retired_at), plus a per-source
  cursor for incremental crawls.
- Subcommands: `crawl` (refresh the catalogue from sources), `select` (pick N new works by score),
  `fetch` (download, process, publish), `retire`, and `nightly` (all of them in order).

## Where it runs (recommendation)
Run a **sibling container on the HA Docker host (192.168.1.73)**, scheduled with supercronic inside the
container (or a host systemd timer that runs `docker run --rm`).
- It bind-mounts only `<ha-config>/www/frame_upload` (read-write) and its own `/data`. It never sees the
  rest of `/config` and has no HA token.
- Running on Unraid would also work, but writing into HA's config over SMB/NFS is more fragile.
  Disk space on the Unraid array is the only reason to choose it (the originals cache can grow; see below).

## Source adapters
Each adapter implements `iter_candidates(cursor) -> Work` and `best_image_url(work) -> (url, w, h)`.

| Tier | Source | Auth | Notes |
|---|---|---|---|
| 1 | Art Institute of Chicago | none | `is_public_domain`; IIIF. Check the maximum IIIF size they serve |
| 1 | Cleveland Museum of Art | none | CC0; gives `images.full` with dimensions |
| 1 | The Met | none | Open Access CSV on GitHub for bulk filtering, then the object API for image URLs |
| 1 | National Gallery of Art | none | opendata CSVs on GitHub plus IIIF |
| 1 | Rijksmuseum | none (new data services) | IIIF; very high resolution |
| 1 | SMK (Denmark) | none | `public_domain` flag; IIIF |
| 2 | Finnish National Gallery, Yale Center for British Art, Getty | check | YCBA/Getty use IIIF and Linked Art |
| 2 | Wikimedia Commons + Wikidata | none | SPARQL for paintings (P31=Q3305213) with P18 image; the widest coverage, and useful as a dedupe/merge key (Wikidata QID) |
| 3 | Library of Congress | none | Prints/posters; rights vary per item, so filter strictly |
| 3 | NASA images | none | Not art. Opt in only if Brian wants space imagery |
| key | Smithsonian (api.data.gov), Cooper Hewitt, Harvard, Paris Musées, Europeana | key | Keys are read from `~/.config/<service>/key` (chmod 600) and mounted read-only |
| key | Unsplash / Pexels / Pixabay | key | Not public domain; their licences allow personal display. Opt in only |
| seed | Frame Gallery catalogue (Apache-2.0), Frame Crop 550+, r/TheFrame 4K list | none | Used as a curated priority list: resolve each entry to a museum original when possible, and use the pre-cropped file only when its licence and provenance are clear |

Rate limits: one polite client per source, with a per-host token bucket, ETag/If-Modified-Since caching,
and a User-Agent that includes a contact address.

## Filter and processing pipeline
1. **Metadata filter**: classification is painting, drawing, print or watercolor (each adapter maps the
   source's vocabulary); licence is PD/CC0 (or opt-in sources only); there is an image.
2. **Size filter**: long edge of at least 3000px (configurable). Use the reported dimensions first and
   check again after download.
3. **Aspect handling** (target 16:9 = 1.778):
   - AR 1.6 to 2.0: crop to 16:9, losing at most about 12%. Saliency/entropy-weighted crop with centre bias.
   - Wider than 2.0 (panoramas): letterbox onto a blurred background.
   - Narrower (portrait or square): **option A** pad onto a blurred, darkened, enlarged copy of the work
     (the SAWSUBE look). **Option B** upload at native aspect and let the TV's matte frame it. *Question for Brian.*
4. **Resize** to 3840x2160 with Lanczos, sRGB, JPEG quality 90, stripped EXIF (about 2 to 5 MB each).
5. **Dedupe**: (a) normalised artist+title+date key; (b) Wikidata QID when the source gives one;
   (c) a 64-bit pHash of the downloaded image within Hamming distance 8. If two sources have the same
   work, keep the higher-resolution or better-crop copy.
6. **Quality heuristics** (to drop junk): reject near-monochrome scans and scans with a colour bar or ruler
   (strong edge columns), and images with very low entropy.
7. **Score/select**: weighted by source tier, presence on a seed list, resolution, aspect fit, and Brian's
   taste filters (movements, eras, artists in or out). Later: thumbs up or down via `art_get_current`.

## Getting files into `/config/www/frame_upload/` (as built in phase 2)
`frame-art publish --dest <dir>` (or `nightly`) maintains this layout:

```
<dest>/full/      3840x2160 renders (crops, blur/solid pads); HA uploads with matte none
<dest>/matted/    native-shape works when --pad-style matte; HA uploads with a matte
<dest>/.staging/  renders in progress; renamed into place when complete
<dest>/_meta/index.json   "full/<file>" -> {title, artist, date, source, license, url}
<dest>/_meta/retire.json  {"updated", "content_ids": [...], "files": [...]}
```
- `<dest>` is proposed as `/config/www/frame_upload/frame-art` so Brian's existing files in frame_upload are left alone.
- Filename: `<source>-<id>-<artist-slug>-<title-slug>.jpg`. Published files are immutable: the integration skips
  on filename + mtime, so rewriting a file would upload a duplicate.
- Pool rotation: each run adds `--count` (7) new works and retires the oldest beyond `--pool` (150).
  Selection is by score with a little daily jitter, interleaved across sources. Live works are never
  re-picked, and retired ones can come back after a year.
- Retiring a work deletes its file and looks up its TV content_id in the integration's sidecar
  (`<folder>/.samsungtv_upload.json`, read-only for us). `retire.json` lists ids retired in the last 14 days,
  so a night HA misses doesn't leak art onto the TV. Re-deleting an id is harmless with `continue_on_error`.
- `_meta`, not `.meta`, because HA reads retire.json through `/local/`. Note that `/config/www` is served
  unauthenticated at `/local/`. That is fine for public-domain art; nothing private should go there.

## HA automation (draft, not applied)
A rest sensor exposes the retire list:
```yaml
rest:
  - resource: http://127.0.0.1:8123/local/frame_upload/frame-art/_meta/retire.json
    scan_interval: 3600
    sensor:
      - name: Frame art retire list
        unique_id: frame_art_retire_list
        value_template: "{{ value_json.updated }}"
        json_attributes: [content_ids]
```
```yaml
alias: Frame art - nightly refresh
triggers:
  - trigger: time
    at: "04:00:00"          # fetcher runs at 02:00
conditions:
  - condition: not
    conditions:
      - condition: state
        entity_id: media_player.<bedroom_frame>
        state: unavailable
actions:
  - action: homeassistant.update_entity
    target: { entity_id: sensor.frame_art_retire_list }
  - action: samsungtv_smart.art_upload_batch
    target: { entity_id: media_player.<bedroom_frame> }
    data: { folder: /config/www/frame_upload/frame-art/full, matte_id: none, throttle: 3 }
  # only when pad style is "matte":
  # - action: samsungtv_smart.art_upload_batch
  #   data: { folder: /config/www/frame_upload/frame-art/matted, matte_id: <type_color>, throttle: 3 }
  - repeat:
      for_each: "{{ state_attr('sensor.frame_art_retire_list', 'content_ids') or [] }}"
      sequence:
        - action: samsungtv_smart.art_delete
          continue_on_error: true
          target: { entity_id: media_player.<bedroom_frame> }
          data: { content_id: "{{ repeat.item }}" }
  - action: samsungtv_smart.art_set_slideshow
    target: { entity_id: media_player.<bedroom_frame> }
    data: { duration: 1h, shuffle: true, category_id: 2 }
mode: single
```
Upgrading samsungtv_smart (8.13.0 -> 8.13.2) first is recommended. That is Brian's call.

## Things to evaluate (phase 1, read-only research)
- **Frame Gallery**: reuse its catalogue (Apache-2.0) and possibly its source/crop metadata.
- **Frame TV Art Changer**: compare its feature set; we take ideas only, since HA stays the only thing that talks to the TV.
- **SAWSUBE**: borrow its blurred-pad and crop parameters.
- **Docent**: weather- or season-matched art. This conflicts with a shuffled slideshow; a later phase could tag works
  (season, palette, warm/cool) and have HA call `art_select_image`, or keep separate pools.
- **Plex stills**: Plex backdrops are already 16:9 and high-res. This would be an easy optional adapter using
  the Plex API (copyrighted, personal display only, kept as a separate opt-in pool).

## Findings from read-only checks (2026-10-25)
Integration source: TheFab21/ha-samsungtv-smart (read at tag 8.14.2; Brian is on 8.13.0 and HACS offers 8.13.2).
- `art_upload_batch` returns `{uploaded: [content_id...], skipped, skipped_duplicate, failed: [path...]}`.
  The response does **not** map filenames to content_ids. The mapping is in the per-folder sidecar
  `frame_upload/.samsungtv_upload.json`, which holds `{filename: {content_id, modified}}`.
  -> **Retirement is simpler**: the fetcher reads the sidecar (it already mounts the folder) and writes
  `.meta/retire.json` with **content_ids**. The HA automation then loops `art_delete` over them, with no
  mapping step in HA. The fetcher must never write to the sidecar; HA owns it.
- Skipping is decided by **filename + mtime**. Rewriting a published file in place changes its mtime and
  causes a re-upload, which leaves a duplicate on the TV. Published files are therefore immutable: a new version gets a new filename.
- Files are listed only from the top level of the folder (`.jpg/.jpeg/.png`). Subfolders such as `.staging/` and `.meta/` are ignored. Good.
- The integration re-encodes every upload to fit the panel (it reports a cap of 3840x2160). Even so, we should emit
  baseline (non-progressive) 8-bit sRGB JPEGs at exactly 3840x2160, so the TV decodes the file we produced.
- There is a throttle because batches of more than about 25 fail without it. About 7 per night is well under that.
- `dedup: true` compares against TV thumbnails with a strict dHash (distance 4 or less). Our own pHash dedupe runs first,
  so we can leave it off.

Source API probes (keyless, all worked):
- Cleveland: `type=Painting&cc0=1&has_image=1` gives 3,907 works; `images.full` is a TIFF with dimensions
  (often 70MB or more, so prefer `images.print` when it is big enough).
- AIC: 62,119 public-domain works. IIIF served a 3000px-wide image (maxArea about 58MP), so the old 843px limit does not apply.
  **Gotcha**: Cloudflare blocks a custom `User-Agent` that contains an email address. Use AIC's `AIC-User-Agent` header instead.
- The Met: `isPublicDomain`, `primaryImage` is the original (e.g. Wheat Field with Cypresses).
- SMK: `public_domain:true` + `object_names:maleri` gives 5,096 paintings with `image_width/height` and a native download URL.
- Rijksmuseum data services: a Linked Art search with `type=painting&imageAvailable=true` gives 4,920 paintings.

## Phases
1. Skeleton, SQLite catalogue, and the six tier-1 adapters, plus a dry-run report (counts after each filter, and a contact sheet).
2. Processing (crop/pad, dedupe), publishing to a **test folder** (not frame_upload) for Brian to review.
3. Brian approves -> the container is deployed on the HA host and the automation is added (with approval).
4. Keyed sources, seed lists, taste feedback, and optionally Plex and Docent-style tagging.

## Decisions (2026-10-25, via hub)
- It runs as a sibling container on the Proxmox HA host. Deployment details are coordinated with the proxmox agent (homelab-stacks/Komodo).
- Taste: "good pieces" plus some seasonal art. Highlights come first, and works tagged with the current season get a boost.
- Defaults: portraits get the blurred-background pad; the TV pool is about 150 with 7 new per night; written in Python; v1 uses keyless sources only.
- Upgrading the integration (now 8.13.2) needs Brian's explicit OK.

## Post-v1 ideas (from Brian)
- **Google Photos** as a personal source. The Library API no longer lists a whole library, so this probably means
  the Picker API, or a shared album exported/synced to a folder. Evaluate when we get to it.
- **AI restyling** (e.g. Gemini "nano banana" image editing): turn personal photos into paintings in chosen styles.
  It would be an optional processing stage, before crop/pad, with its own pool and cost limits. The API key would be read from a file.

## Phase 1 findings
- With 300 crawled per tier-1 source, 85–99% pass the filters. Most works are padded rather than cropped (about 5–10% are close
  enough to 16:9 to crop).
- The AIC CDN answers many IIIF widths with a long-cached 843px fallback and caps real renders at about 3000px. The
  fetcher checks the size it actually received, steps through standard widths, and refuses anything needing more than 1.35x upscaling.
- The Met: v1/search was retired on 2026-10-01, so we use v1.1 (paginated). Its CDN answers bursts with 403, so we retry.
- Rijksmuseum has no highlight flag, so a capped crawl returns arbitrary items (manuscript pages, fans). It needs a quality
  signal, e.g. a Wikidata sitelink count, or seeding from curated lists.
- Peak memory is about 1.2GB with 2 parallel renders (100MP masters). Production runs renders one at a time.
- There is no cross-source duplication between the six museums (they hold different physical works). Dedupe matters
  once Wikimedia and the seed lists are added.

## Phase 2 (done locally, 2026-10-25)
- `publish` / `nightly` commands with staging, index.json, and retire.json built from the sidecar. These were tested end to end in
  a local folder with a simulated sidecar (10 added, then 10 added and 5 retired with the right content_ids).
- Pad style is configurable: `--pad-style blur|solid|matte`. With matte, padded works go to `matted/` for a second
  batch call with a TV matte.
- Notability: Wikidata sitelinks, matched by QID or by collection-qualified inventory number (P217 with a P195
  qualifier). It adds up to +4 to the score. Rijksmuseum crawls notable works first. About 1,000 of 2,500 catalogued works match.
  WDQS rejects generic library User-Agents, so it gets its own descriptive UA.
- Drawings and prints: `--kinds painting,drawing,print` on crawl, dry-run and publish (default painting only).

## Open questions for Brian
1. **Where should it run?** My recommendation is a sibling container on the HA Docker host (192.168.1.73). The alternative is Unraid. Which host
   runs the HA container, and what is the host path of HA's `/config`?
2. **Taste**: eras, movements and artists to favour or exclude? Paintings only, or also drawings, prints and Japanese
   woodblocks? Any religious or graphic subjects to skip? Include NASA or photography?
3. **Portrait works**: blurred-background pad (fills the screen) or native aspect with a TV matte? If a matte, which style?
4. **Pool size and cadence**: about 150 on the TV and about 7 new per night OK? Slideshow interval (15min/1h/12h)?
5. **Keys**: which keyed sources do you want? Save each to `~/.config/<service>/key` (chmod 600) and don't
   paste them on the bus.
6. **Language**: Python is my default for this (best imaging libraries). Any objection, or would you rather have Rust?
7. OK to upgrade samsungtv_smart to 8.13.1 before we automate?
8. HA was confirmed as Container (not OS), so this question is settled.
