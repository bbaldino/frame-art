# Portrait Studio and photo sources: design

Status: draft for Brian's review, 2026-11-02. Builds on the variants design
([2026-10-27-variants-design.md](2026-10-27-variants-design.md)) and the mix
schedules design ([2026-10-28-mix-schedules-design.md](2026-10-28-mix-schedules-design.md)),
released in 0.10.x.

## Why

frame-art fills the Frame TV with public-domain art and gives it seasonal
versions. Brian also wants the family on the wall: his own photos turned into
paintings that look like real works by real painters, in a style that changes
from photo to photo. One example is the kids' school photo painted as a
Sargent. Another is a day on the lake painted as a Dalí.

Unlike the seasonal versions, these are personal. With seasonal versions, a
surprise is part of the fun, and most of them just work. A family portrait
should be crafted: Brian sits down, tries painters, nudges the result until
it's right, and keeps the one he likes.

Success: on an evening, Brian picks a batch of photos and makes a few
paintings he's happy to hang. Those paintings then come up on the TV as part
of the normal rotation, at a share he sets in his schedules.

## Anti-goal: kitsch

The worst outcome is a painting that is "too much": costume-party touches,
added props, glossy cuteness, a pumpkin on every surface. Every prompt the
Studio sends carries restraint rules: add nothing that isn't in the
photograph (no props, costumes, frames, borders, signatures or text), keep
faces true to the photo, and nothing cute, glossy or theatrical. The goal is a
real painting that could hang in a museum. The trial prompts below carry
these rules, and none of the 51 trial paintings came out kitschy.

The same anti-goal applies to the seasonal types. The live Halloween prompt
overdid the jack-o'-lanterns. Toning it down is a separate small change, not
part of this project.

## What the trial showed (2026-11-02)

Brian supplied five photos: two school portraits, a toddler on a front step,
a toddler standing in a river, and Brian with his son on a tube on a lake. We
painted them in three rounds, 51 paintings in all, at about 5.6¢ each with
`google/gemini-nano-banana-2.1` via OpenRouter at 2K. The prompts and scripts
are under the session scratchpad. The prompts are repeated here because they
are the starting recipes.

1. A prompt that said "keep the composition, setting and clothing exactly"
   gave a photo with a paint filter on it. Vermeer and Sargent were barely
   changed.
2. The looser prompt below gave real paintings for **portraits** (a face that
   fills the frame). Brian liked Hopper, Monet and Sargent, and Homer was also
   good. Vermeer was too photographic and is dropped.
3. **Scenes** (small figures in a big setting) barely changed under any painter
   whose prompt keeps the setting, even after cropping tighter and sending a
   softer, smaller copy of the photo. Dalí, whose prompt lets the setting be
   reinvented, transformed both scenes well. Brian liked the river and the
   lake ones. On portraits Brian found Dalí worse than the others.

The conclusion: what decides the result is a photo's **kind** (portrait or
scene), together with whether the painter may **reinvent the setting**.

The portrait prompt (round 2):

> Paint this scene as {artist} would have painted it: an original oil painting
> in their manner, not a filtered photograph. Use their palette, their way of
> handling light, and clearly visible brushwork and paint texture. Like the
> artist, simplify and edit: soften or reduce background detail and clutter,
> drop photographic sharpness everywhere, and let the people be the subject,
> even when they are small in the frame. Keep the people, their faces,
> expressions, poses and clothing recognisably as in the photograph, and keep
> the place recognisable. Add nothing that is not there (no props, costumes,
> frames, borders, signatures or text), and nothing cute, glossy or
> theatrical: it should look like a real painting in a museum.

The Dalí prompt (round 3):

> Paint this scene as Salvador Dali would have painted it: an original oil
> painting in his surrealist manner, with his precise, luminous technique,
> deep dreamlike perspective and clear Catalan light. Let one or two quiet,
> strange touches grow out of the scene itself (stretched shadows, an uncanny
> horizon, a subtly impossible landscape) rather than pasted-on symbols: no
> melting clocks, elephants, ants, crutches or moustaches. Keep the people,
> their faces, expressions and poses recognisably as in the photograph. No
> text, frames or signatures, nothing cute or glossy: it should look like a
> real Dali in a museum.

## Goals

1. **Sources: one interface for every place works come from.** Today's museum
   crawlers (Met, AIC, Cleveland, NGA, Rijks, SMK, Commons, LoC) become
   implementations of a general source interface. Google Photos is the first
   **personal** source, and the interface allows more later (a NAS folder,
   Immich, a direct upload).
2. **Google Photos via the Picker.** Brian picks single photos or a whole album
   in Google's picker. frame-art copies the photos it hasn't seen before. When
   he picks the same album again, only the new photos come in.
3. **A "Your photos" page.** It holds the photos brought in, each with its kind
   (portrait or scene). The kind is suggested automatically and Brian can
   change it in one click.
4. **The Studio.** Start from one photo (optionally plus an artwork from the
   catalogue to place the people into). Choose a painter (suggested ones first,
   or any artist typed in). Choose the setting rule (keep or reinvent) and the
   shape (fill the screen, or keep the photo's shape with a mat). Paint, look,
   nudge (one-click nudges or free text), and repeat, either from the photo or
   from an earlier try. **Save** the try you like. The session shows its
   running cost.
5. **Portraits on the TV.** Saved paintings form a collection. A new
   "Portraits" entry in the schedule editor gives them a share of the screen,
   combined by the same rules as the seasonal types. A saved painting can be
   removed from the collection.
6. **A style list.** A short, editable list of painters. Each has a prompt and
   records which kinds it suits. It starts with Sargent, Monet, Homer and
   Hopper for portraits (and scenes where it keeps them), and Dalí for scenes.
   Brian can add, edit and prune painters.
7. **Providers behind interfaces.** The image model (OpenRouter today) sits
   behind an interface, so another provider can be added later. The same goes
   for photo services.

## Non-goals (for now)

- Automatic painting of personal photos by the nightly run. Personal paintings
  are made by hand in the Studio, and the nightly run only rotates saved ones.
- Seasonal recipes applied to personal photos or portraits (recipe stacking,
  "option B"). The design must not rule it out.
- Tagging who is in a photo, or "only photos of the birthday person" on a
  birthday schedule.
- Watching an album for new photos. The Picker API cannot do it (see
  Constraints).
- A model run locally. Photos going to OpenRouter and Gemini is accepted for
  now.
- Changes to how museum works are crawled, scored, published or retired,
  beyond fitting the crawlers to the new interface.

## The model (decided with Brian)

Three parts:

- **Sources produce works.** A source says whether it is *crawled* (on a
  schedule, many candidates, frame-art chooses) or *picked* (Brian chooses,
  every item is meant to be used), whether it needs a login, and whether it is
  *private*. A work has a kind: today's painting, drawing, print and
  photograph, plus the new **personal photo**.
- **Recipes remake a work.** Today's variant types (Halloween, Christmas,
  Birthday) are recipes, and so are the painter styles. "Place them in this
  artwork" is a recipe that takes two works. Each recipe says which kinds of
  work it accepts. A recipe produces a version of a work.
- **A schedule share picks a pool and a recipe.** "Halloween 50%" means museum
  art with the Halloween recipe. "Portraits 15%" means saved Studio paintings.

**One recipe per version** ("option A"): a version is a work with one recipe
applied. Stacking (a Vermeer portrait later given a Christmas version) is a
possible extension. The Studio's "try again from this" counts as editing a
try, not as stacking recipes.

## Behaviour

**Private stays private.** Personal photos and the paintings made from them
live only in frame-art's own storage and on the TV. They never appear in the
public catalogue pages, crawls, notability scoring or retirement rules. They
are also never shown on any page that isn't Brian's LAN UI. The web UI stays
LAN-only, as now.

**Kind suggestion.** When a photo comes in, a cheap vision model call labels
it portrait or scene. A portrait is a face that fills much of the frame; a
scene is small figures in a larger setting. The call also writes a short
reason ("close portrait, 1 person"; "2 people, water, outdoors"). The kind
picks the suggested painters and the default setting rule:
- portraits keep their setting;
- scenes default to reinventing it.

**Shape.** The default is **fill the screen**: the painting is made at 16:9 and
the painter extends the scene to the sides. The trial only painted at the
photo's own shape, so the extended canvas is untested. Try it early in the
build. The **keep the photo's shape** option paints at the photo's shape, and
the painting is shown with a mat like a tall museum work.

**How a prompt is built** *(my call)*. A painter's entry in the style list
holds only that painter's manner: palette, light and handling, plus anything
the painter adds of their own. Dalí's "one or two quiet, strange touches, no
melting clocks" goes there. The setting rule is a separate block of text,
"keep" or "reinvent", added by the Studio. The prompt sent is: the base
instruction ("an original oil painting, not a filtered photograph"), then the
painter's manner, the setting block, the restraint rules, the shape, and any
nudges. That way every painter works with either setting. Each painter's
"suits" field only decides which painters are suggested first; it doesn't
hide any.

**Resolution** *(my call)*. Tries are painted at 4K, the size the seasonal
versions use (about 12¢ a try, against 5.6¢ at 2K). What Brian saves is
exactly the try he saw, so Save never calls the model again. The Studio is
low volume, so the extra cost is small next to the cost of a saved painting
that doesn't match the approved try.

**Nudges.** A nudge adds a short instruction to the prompt for the next try.
Examples: "closer to the photo", "more painterly", "simpler background",
"warmer light", or free text such as "less desert, more beach". The base
prompt and its restraint rules always stay in place.

**Saving and storage.** Saving keeps a try at full resolution, together with
the prompt, the painter, the source photo and the cost. Unsaved tries are
deleted after a while (builder's call: about two weeks). The originals of
photos brought in are kept until Brian removes them.

**Portraits on the TV** *(my call)*. The Portraits share works like any other
share in the schedule editor and takes its slice "off the top" by the existing
rules. Saved paintings are not versions of museum works, so they sit **on top
of** the museum pool. The museum pool (150 works, 7 added a night) and its
retirement are unchanged. The nightly run publishes enough saved paintings that
they make up the Portraits share of everything on the screen, rotating through
the collection over time. When the share shrinks or its schedule ends, it takes
the extra paintings off the TV. Nothing needs refilling, because no museum work
was retired to make room. If there are fewer saved paintings than the share
asks for, all of them are shown, and the rest of the share goes to originals,
the same as a seasonal type that is short of versions. Home Assistant stays the only
thing that talks to the TV: paintings reach the TV through the same output
folder as everything else (moved out of `www`, see below).

## Moving frame-art's output out of the web-served folder (decided with Brian)

Today frame-art writes to `/config/www/frame_upload/frame-art/`. Home Assistant
serves `/config/www` at `/local/` with no login, and `hass.baldino.me` makes
that reachable from the internet (checked 2026-11-02: the Nginx Proxy Manager
host has no access list). That is harmless for public-domain art but not for
family paintings. Brian chose to move **all** of frame-art's output, museum art
included, to one folder outside `www`, so everything lives in the same place.

- The new folder is outside `/config/www`, so nothing in it is served over
  HTTP. Brian's own files in `frame_upload/` are left alone.
- **HA changes are needed**, and they need Brian's OK and a proxmox review:
  1. The upload automation uses the new folder. samsungtv_smart may need the
     folder in `allowlist_external_dirs`; verify this.
  2. The retire list can no longer be read through `/local/`. Today a rest
     sensor reads it there. *(My call)*: frame-art serves the retire list
     from its own LAN web UI, and HA's rest sensor reads it from there.
  3. The frame-art compose file mounts the new folder in place of the old
     one.
- **Migration risk.** The integration remembers what it uploaded in a sidecar
  file (`.samsungtv_upload.json`), stored per folder. Moving the folder could
  make it upload every work again and leave duplicates on the TV, or lose
  track of which TV content ids to delete. The builder must plan the move so
  that neither happens, for example by carrying the sidecar entries across or
  retiring old content ids first, and must test it before the real switch.
- This move ships **before** any personal photo or painting is written
  anywhere HA can see.
- The `_meta/index.json` titles and the `sensor.frame_art_now_showing` entity
  show a portrait as "Portrait" with no file title. *(My call)*: the
  sensor's state lives in HA, which can be reached from outside, so it should
  give nothing about a family photo away.

## Constraints

- **Google Photos API (since 2025-03-31).** Apps can no longer read a user's
  library. The read-only library scopes were removed, and apps can only list
  photos they uploaded themselves. Choosing existing photos goes through the
  **Picker API**:
  1. frame-art creates a picking session and shows Brian its link, which opens
     Google Photos.
  2. Brian selects photos or an album.
  3. frame-art polls the session and downloads the chosen items.
  Picking must happen again for each batch.
  Sources: [Google's update notice](https://developers.google.com/photos/support/updates).
- **Google sign-in setup is Brian's to do.** It needs a Google Cloud project
  with the Picker API enabled and an OAuth client. The client secret and
  tokens follow the key rule: Brian saves them to a file under
  `~/.config/<service>/` (chmod 600), frame-art reads them from there, and
  they never go on the bus or into logs. Sign-in is the normal Google sign-in in a
  browser, done when Brian is at the Studio. The web UI is plain HTTP on a LAN
  IP, and Google's OAuth redirect rules may not accept that. The builder must
  choose a flow that works without making anything public. One option is a
  desktop-style client where Brian pastes back the code or URL. Verify this
  before committing to a flow. The Google app stays in "testing" status:
  Brian accepts signing in again about once a week (Google expires
  testing-mode sign-ins after 7 days). The Studio must notice an expired
  sign-in and offer "Sign in again", not fail.
- **API keys never go on the bus.** Key files are never read out or printed.
- **Cost.** The OpenRouter balance is shared with Brian's other keys. On
  2026-10-29 the nightly run failed silently when the balance fell under the
  $1 minimum for image output. The Studio must show the error plainly when a
  paint fails ("OpenRouter: balance too low"). It must not just say "failed".
- **Deployment.** Release and deploy follow the usual flow: a pinned image, a
  homelab-stacks PR, and proxmox's check. Any HA change needs Brian's OK and a
  proxmox review. This design needs the HA changes listed in the
  section on moving the output folder.

## Left to the builder

- The exact source interface and how the crawlers fit it. Keep the crawlers'
  behaviour unchanged.
- The storage layout for photos, tries and saved paintings, and the database
  schema.
- The vision model used for the kind suggestion, and its prompt.
- The Studio's layout. The sketch from brainstorming is a starting point, not
  a spec.
- How the nightly run chooses and rotates saved paintings within the
  Portraits share.
- The wording of the nudges, beyond the examples above.
- Whether "Place them in this artwork" ships in the first release or the
  next. Brian said it "could maybe" be added. It must fit the design either
  way.
