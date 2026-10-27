# API keys

Most frame-art sources need no key. The ones below do, and stay off until a key
is saved. This list matches `src/frame_art/keys.py`; the web UI's Settings page
shows each one as **present** or **missing**, and never shows the key itself.

## Where a key goes

One file per service, holding just the key on one line:

```
<keys dir>/<service>/key      chmod 600
```

- **Running locally:** the keys dir is `~/.config`, so e.g. `~/.config/pexels/key`.
- **The deployed container (LXC 102):** the keys dir is `/keys` inside the
  container (`FRAME_ART_KEYS=/keys`), bind-mounted read-only from
  `keys/` next to the stack's compose file, e.g. `keys/pexels/key` on the host.
  Files must be readable by uid 1000 (`chown 1000:1000`, `chmod 600`).
  *Not wired yet: needs a small compose change (mount plus env) before the first
  keyed source ships.*

```sh
mkdir -p ~/.config/pexels && umask 077 && printf '%s\n' 'PASTE-KEY' > ~/.config/pexels/key
```

Keys never go in settings.json, the repo, the message bus or a log.

## AI image editing

| Service | Sign up | Cost | Used for |
|---|---|---|---|
| `openrouter` | https://openrouter.ai/keys | Pay as you go: about $0.12 per edit with `google/gemini-nano-banana-2.1` | Seasonal variants (Settings → Seasonal): AI-edited versions of your works. |

The key file is `<keys dir>/openrouter/key`. Set a spending limit on the key in
OpenRouter, since nightly generation spends money on its own. The deployed
container's keys mount still needs wiring (see above) before the key can be
used there.

## Photography

| Service | Sign up | Free tier | Adds | Verdict |
|---|---|---|---|---|
| `pexels` | https://www.pexels.com/api/ | 200 requests/hour, 20,000/month | Landscape photography; originals are often 6000px+. Pexels licence: free, no attribution required (the API asks for a credit where practical, which our index keeps). | **Best keyed photo source.** |
| `unsplash` | https://unsplash.com/developers | 50/hour (demo), 5,000/hour once approved | High-quality landscapes; Unsplash licence is free. | **Doubtful:** the API guidelines require hotlinking Unsplash's URLs and pinging a download endpoint, and we save copies to upload to the TV. Ask before using. |
| `pixabay` | https://pixabay.com/api/docs/ | 100 requests/minute | Photography, but the standard API stops at 1280px; full resolution needs approved "full API access". | Only worth it if approved. |
| `nps` | https://www.nps.gov/subjects/developer/get-started.htm | 1,000 requests/hour | National park photos, public domain, but most API images are small. | Low priority. |
| `flickr` | https://www.flickr.com/services/apps/create/ | 3,600 requests/hour | Flickr Commons: archive photos with "no known copyright restrictions", mostly historical scans. | Low priority; few are 4K landscapes. |

## Museums (art)

| Service | Sign up | Free tier | Adds |
|---|---|---|---|
| `smithsonian` | https://api.data.gov/signup/ | 1,000 requests/hour | Smithsonian Open Access: CC0 paintings (SAAM, Freer, NPG) and photographs. |
| `harvard` | https://harvardartmuseums.org/collections/api | 2,500 requests/day | Harvard Art Museums paintings; reuse terms vary per image, so filter on them. |
| `parismusees` | https://apicollections.parismusees.paris.fr/ | not published | CC0 works from the 14 Paris city museums (Petit Palais, Carnavalet, ...). |
| `europeana` | https://pro.europeana.eu/pages/get-api | no hard limit (fair use) | Open-licence works aggregated from thousands of European museums. |
| `cooper-hewitt` | https://collection.cooperhewitt.org/api/ | not published | Design and drawings; small images, and the API may have been retired. Lowest priority. |

## Keyless sources (no action needed)

The six museums already in use (AIC, Cleveland, The Met, NGA, Rijksmuseum, SMK)
and **Wikimedia Commons** photography need no key. The Library of Congress
(Carol M. Highsmith archive) and NASA's image library are keyless too.
