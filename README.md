# frame-art

Public-domain art for a Samsung Frame TV. frame-art crawls museum open-access
collections (Art Institute of Chicago, Cleveland, The Met, National Gallery of
Art, Rijksmuseum, SMK), picks well-known paintings, renders them at 3840x2160
(cropped to fill the screen, or on a blurred background), and keeps a rotating
pool of them in a folder. Home Assistant uploads that folder to the TV with the
[SamsungTV Smart](https://github.com/TheFab21/ha-samsungtv-smart) integration;
frame-art itself never talks to the TV.

## Running

```sh
docker run -p 8740:8740 \
  -v ./data:/data -v /path/to/ha/config/frame_art_pool:/out \
  -e TZ=America/Los_Angeles \
  -e FRAME_ART_MQTT_HOST=mqtt.home \
  -e FRAME_ART_HA_FOLDER=/config/frame_art_pool \
  ghcr.io/bbaldino/frame-art:<version>
```

| Variable | Meaning |
|---|---|
| `FRAME_ART_DATA` | State: catalogue, settings, caches (default `/data`) |
| `FRAME_ART_DEST` | Where rendered works are written (default `/out`) |
| `FRAME_ART_HA_FOLDER` | The same folder as Home Assistant sees it |
| `FRAME_ART_MQTT_HOST`, `FRAME_ART_MQTT_PORT` | Broker Home Assistant listens on |
| `FRAME_ART_PORT` | Web UI port (default 8740) |

The web UI (no authentication; keep it on the LAN) has these pages:

- **Library**: one place for everything the TV can show: crawled museum
  works, your own photos, Studio portraits and AI versions of any of them.
  The TV draws only from works you add to the Library. "All works" shows
  everything crawled or added; filter by source, kind, search or tags, star
  favourites, tag works yourself, and add, remove or put back in bulk. A
  removed work never comes back on its own.
- **Schedule**: the mix. Each schedule is a yearly date range with slices,
  each a share of the TV for Library works carrying a set of tags (for
  example `ai-variant:christmas` 50% from December 1 to 31, or
  `kind:photograph` 20% all year). What the slices leave is filled from the
  rest of the Library.
- **Now**, **On the TV** and **History**: what the TV shows, has live and has
  shown. Star a work or remove it from the Library from any of them; pin a
  work on the TV so it is never retired.
- **Studio**: paint your photos in a painter's style and save them to the
  Library; **Version styles** holds the AI version types (a Halloween look,
  say) and how many are made each night. Versions need an OpenRouter key (see
  `docs/api-keys.md`). Your photos are uploaded or picked from Google Photos
  on the Your photos page (linked from the Library as "Add photos"). They
  never leave the LAN: the folder HA uploads from is outside `/config/www`.
- **Settings**: crop tolerance, background style, kinds to crawl, museums,
  pool size, works per night, nightly run time.

The old pages redirect: `/catalogue` to All works, `/collection` to
Favorites, `/portraits` to the Studio portraits in the Library, and
`/variants` to the Schedule. The design is in
`docs/superpowers/specs/2026-11-05-library-and-schedule-design.md`.

### Upgrading to the Library (0.12.0)

The first start moves everything over once: your collection, every work that
has ever been on the TV and every saved portrait go into the Library (banned
works don't); bans become Removed; the collection becomes stars; pins stay
pins; existing versions become version works; and old type shares become
slices. These settings are dropped (an old settings file still loads; the
keys are ignored):

| Setting | Now |
|---|---|
| `choose_from` | gone: the TV draws only from the Library |
| `photo_share` | becomes a `kind:photograph` slice in Everyday |
| `photo_crop` | gone: photographs render at the `crop` setting like everything else |
| `allow_nudes` | gone: nothing is published unless you add it to the Library |
| `full_screen_only` | a Library filter instead of a setting |
| `repeat_days` | gone |

`kinds` stays, but only controls what is crawled ("Kinds to crawl"); it no
longer filters what is published. `variant_limit` stays. No environment
variable changed, and Home Assistant needs no change.

Home Assistant learns what to do from an MQTT-discovered `sensor.frame_art` and
acknowledges on `frame-art/ack`; see `docs/design.md` for the automation.
The overview's TV buttons (upload now, show frame-art's works, back to the
Samsung art) only publish a request on `frame-art/command`; a Home Assistant
automation decides whether to act on it. While an upload runs, the upload
automation reports its progress on `frame-art/ack`, and the overview's TV panel
refreshes itself until it's done; a fourth automation publishes, retained on
`frame-art/uploads`, whether uploads are turned on. `docs/ha-automation.yaml`
holds all four automations.

## Development

```sh
uv sync
uv run pytest
uv run frame-art --help
```
