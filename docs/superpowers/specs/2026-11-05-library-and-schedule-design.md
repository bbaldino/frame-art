# Library and Schedule: design

Status: draft for Brian's review, 2026-11-05. It replaces how works are chosen
for the TV, which today is spread over the catalogue, the collection, the
photo share, seasonal versions and Studio portraits. It builds on the mix
schedules design ([2026-10-28-mix-schedules-design.md](2026-10-28-mix-schedules-design.md))
and the Portrait Studio design ([2026-11-02-portrait-studio-design.md](2026-11-02-portrait-studio-design.md)).

Mockups from the brainstorm are in `.superpowers/brainstorm/` (git-ignored):
`library-v2.html` and `schedule.html`.

## Why

Each new kind of work got its own page and its own rule for reaching the TV.
The nav now has Collection, Catalogue, Your photos, Studio, Portraits,
Seasonal and On the TV. Settings has a photo share and a "collection first /
only / automatic" choice, and Seasonal and Portraits each have a share. Brian:
"collections, photos, catalogue, seasonal... that's adding up quick." He wants
one model that every kind of work fits into.

Success: Brian can answer "what can go on the TV?" on one page (Library) and
"what goes on the TV this month?" on one other page (Schedule). Adding a new
kind of work later doesn't need a new page or a new share setting.

## The model

Four levels, from widest to narrowest:

1. **Works**: everything frame-art knows of. That covers crawled museum works,
   Brian's photos (uploaded or picked from Google Photos), saved Studio
   portraits, and AI versions. A version stays linked to its original.
2. **Library**: the works Brian has put there by hand. This is the only thing
   the TV draws from.
3. **On the TV**: the live set, at most `pool` works (default 150), refreshed
   nightly. The Schedule decides which Library works make it up.
4. **Now showing**: the single work on screen, picked by the TV's own
   slideshow. Unchanged.

The **Schedule** sits between 2 and 3. It says what share of the TV goes to
which slice of the Library on each day of the year.

Concepts that go away: the Collection (absorbed by the Library), the photo
share, the Portraits share, and "choose from" (collection first, collection
only, or automatic).

## Library

### Membership is by hand only

In this phase a work is in the Library only because Brian put it there. Brian:
"to start we could say that things have to be manually added to the library…
we can avoid the rules for that phase." Rules that add works automatically
(by kind, crop, nudity, size) are a possible later phase. They're out of scope
here.

A work is always in exactly one of three states:

- **In the Library**: added by hand.
- **Removed**: Brian took it out on purpose. This is today's ban: a removed
  work never goes on the TV, and it's retired from the TV at once if it's
  live. "Put back" returns it to the Library.
- **Not in the Library**: everything else.

Ways in:

- **Add** on any work's card on the Library page (Works view).
- **Add all N** on a filtered Works view (bulk).
- **Save to Portraits** in Studio: the saved painting goes in.

Ways out:

- **Remove** on a card, or **Remove all N** on a filtered view.
- **Remove from Library** on the Now page, for whatever is on screen. It
  works like today's Ban from that page.

Removing an original offers to remove its versions too, ticked by default.
Put back on an original doesn't bring its versions back *(my call)*.

Hand-added works go on the TV whatever their kind, nudity or shape. Adding by
hand is the decision *(my call)*.

Removing a portrait from the Library doesn't delete the painting, and
removing a version doesn't delete its file. Deleting is a separate action,
offered on the work's own page *(my call)*.

### Rendering checks still apply

Adding a work doesn't promise it can go on the TV. Works that fail to download
or render are rejected at publish time, as now. The Add button warns rather
than blocks when the catalogue already knows of a problem, such as "crop cuts
44%, it will be padded" or "rejected: too small" *(my guess, not confirmed)*.
The Library view marks rejected works so they don't count silently.

### Favorites

The star stays as a mark separate from Library membership. A starred Library
work comes round about twice as often as an unstarred one (see Rotation).
Starring a work that isn't in the Library doesn't add it *(my call)*.
"Favorite" is also a tag, so a schedule can give favorites their own share.

### Your photos on the TV (new)

Brian's own photos (not Studio paintings of them) have never gone to the TV
until now. Under this model they're works like any other, so Brian can add a
photo to the Library and it goes on the TV as it is. Brian didn't ask for this
explicitly. It follows from "library: this contains all images" and the
mockup he approved, which had a "Your photos" slice. *Flagged for Brian.*
Photos still never leave the LAN. The output folder is `/config/frame_art_pool`,
outside `/config/www`, so HA doesn't serve it over HTTP (`docs/design.md`).
The `publish.py` docstring that says it's served at `/local/` is out of date
and should be corrected.

### Date added

Brian: "are we tracking 'Date added' for both 'images' and 'library'? if we
re-scrape/add a new source it'll be nice to see which ones are new."

Brian: "crawl time should be a property of the source, i think, not the
work. images can model when an image was added there. same for library."

- **Works** record **added**: when the work first entered Works (first
  crawled, uploaded or picked, saved in Studio, or made as a version). It is
  set once and never changes. A re-crawl that sees the work again doesn't
  touch it.
- **Library** records **added to Library** per member: when Brian added it.
  Removing and putting back sets it again *(my call)*.
- **Sources** record **last crawled**, per source. This replaces the
  per-work `crawled_at`, which a re-crawl overwrites and whose only use today
  is `jobs.last_crawl` (the newest crawl overall, for `recrawl_days`). That
  check reads the sources' times instead.

What exists today: photos record when they were added, portraits when they
were saved, versions when they were made, collection works when they were
collected. Museum works have nothing that says when they first arrived.

- Both views sort by newest first and filter to "added since" a date. A
  crawl or import summary in Runs links to "new from this run" *(my call)*.
- Cards show the relevant date small, under the title.
- On upgrade, a museum work's added date is backfilled from the earliest
  timestamp the catalogue has for it (crawled, rendered, rejected or
  published), so older dates are approximate. Photos, portraits and versions
  use the dates they already have. Added to Library comes from the
  collection date, else the first time on the TV, else the portrait's save
  date. Each source's last crawled comes from its newest `crawled_at`
  *(my calls)*.

## Tags

Every work carries tags. The Library filter and the Schedule's slices use the
same tag picker. Tags come in groups, and the picker shows the group:

- **Subject and style**: what the catalogue already derives (artist, style,
  place, subjects, plus "landscape", "portrait" and so on). Unchanged.
- **Kind**: painting, drawing, print, photograph (museum photographs), plus
  "portrait" and "scene" for Brian's photos (their Studio kind).
- **Source**: the museum (The Met, NGA…), **your photos**, **Studio**.
- **AI variant**: `ai-variant-<type>`, such as `ai-variant-christmas` or
  `ai-variant-halloween`. Brian: "make the ai variant tag separate…
  ai-variant-<tag name>." A plain `christmas` tag (a real Christmas painting,
  or Brian's own tag) is never the same as `ai-variant-christmas`. Studio
  portraits are not AI variants: they're works Brian made, tagged with the
  **Studio** source (see AI versions).
- **Yours**: `favorite`, plus any tag Brian types onto a work (for example
  `christmas` on a family photo). Brian's tags are stored per work and can be
  added or removed on the work's page and in bulk from a filtered view.

A version **inherits its original's tags** (subject, style, kind, source,
artist) and adds its `ai-variant-<type>` tag, so a slice like
`[ai-variant-christmas, landscape]` works *(my call)*. Its own `favorite`
and Brian's tags are its own, not inherited.

Tags in a slice and in the Library filter are stored **with their group**
(for example `kind:photograph` or `subject:portrait`), so a Kind tag and a
Subject tag with the same word never match each other. The picker shows the
group *(my call)*.

Originals carry none of the `ai-variant-*` tags. Today's "Halloween version"
tag on an *original* (meaning "has a Halloween version") becomes a filter on
the Library page, "has an AI version", rather than a tag *(my call)*.

## AI versions

There are two flows, and Brian wants both. Brian: "portraits -> studio ->
manual crafting/addition / other variants -> auto create as-needed -> put in
directly … the two use cases are definitely different."

### Studio portraits: made by hand, ordinary Library works

Portraits are crafted in Studio and saved by hand, as in 0.11.0. A saved
portrait is a work with the **Studio** source tag, and saving it adds it to
the Library. From then on it behaves like any other Library work: it can fill
Rest of the Library, and a schedule can give it its own slice with
`[source:studio]`.

### Seasonal versions: made automatically, shown only by their slice

A seasonal version is a treatment of a Library work, not a work Brian picks.

- A slice whose tags include `ai-variant-<type>` asks for versions of that
  type. Brian: "christmas versions of anything" is enough, so a slice is just
  `[ai-variant-<type>]`. Narrowing it with more tags is not needed in this
  phase.
- When such a slice will be short, the nightly run makes versions of Library
  works, as today: up to the per-night limit (`variant_limit`), starting the
  week before the schedule starts, with the failure counter that stops
  retrying a work that keeps failing. Originals are chosen from the Library
  and never from Removed works.
- **Make these now** on the Version styles page still makes them by hand
  ahead of time, with the count and estimated cost shown first.
- Each version is a work in Works, tagged `ai-variant-<type>`, linked to its
  original. It is **eligible** while its original is in the Library and it
  isn't itself removed. It is not a Library member of its own: it reaches the
  TV only through a slice naming its `ai-variant-<type>` tag, never through
  Rest of the Library, so a Christmas version never turns up in July.
- Brian can remove a bad version (it goes to Removed, as today's "don't use")
  or regenerate it. Removing keeps the file.
- Versions can be made of any Library work with an image, including Brian's
  photos and Studio portraits. They stack: a Christmas version of a portrait
  carries `source:studio` and `ai-variant-christmas` *(my call)*.

### Version styles

The types (Halloween, Christmas, Birthday) keep their name and prompt. They're
edited on a **Version styles** page under Studio, next to Painters *(my
call)*. That page also shows the cost so far per type, as Seasonal does
today, plus how many versions of each type exist and "Make these now".
Adding a type creates its `ai-variant-<slug>` tag.

### An original and its version are never live together

A slice can pick a version whose original is already on the TV. When that
happens the original retires in the same run, so the version replaces it.
The reverse also holds: the original isn't published while one of its
versions is live. This keeps today's behaviour, where a version swaps in
for its original. Two versions of the same original are never live together
either.

## Schedule

The Seasonal page becomes **Schedule**. The mockup is `schedule.html`.

### Schedules and slices

- A **schedule** has a name, a date range (MM-DD to MM-DD, every year,
  wrapping the new year if needed), and a list of **slices**.
- A **slice** is a set of tags and a share in percent. A work matches a slice
  if it has **all** the slice's tags, which is the same rule as the Library's
  tag filter, so "See them" opens exactly that Library view *(my call)*. "Winter
  or snow" is two slices.
- A schedule's slices sum to at most 100%. **Rest of the Library** is
  implicit and takes whatever is left.
- **Everyday** is an ordinary schedule covering Jan 1 to Dec 31. It's created
  on upgrade (see Moving over) and can be edited like any other.

### Today's mix

Overlap works as in the mix schedules design, generalized from types to
slices: the narrowest schedule covering today takes its slices' shares first,
each wider one takes its shares of what's left, and Rest of the Library gets
the remainder. Ties are broken as before.

Turning shares into works: `share × pool`, rounded so that the counts sum to
the live pool. The live pool is `min(pool, works the Library can supply)`.

### Matching and shortfall

- Each live work counts toward exactly one slice. A work matching several
  slices counts toward the first: the narrowest schedule first, then the
  order of slices in the schedule. Rest of the Library is last *(my call)*.
- A slice with too few matching Library works shows what it has. The gap
  goes to Rest of the Library, never to works outside the Library. This
  matches what the earlier design did with a short schedule: the gap falls
  through.
- The Schedule page shows, per slice, how many Library works match and how
  many the share wants ("9 in Library, 22 wanted").
- The page says plainly when the whole Library is smaller than the pool
  ("Library has 120 works; pool is 150, so the TV holds 120").

### Rotation

The nightly run replaces about `count` works per night (default 7) across the
whole pool, spread over the slices in proportion to their share. Within a
slice it picks the least recently shown first. Never-shown works come first,
and the existing diversify step breaks ties so one artist doesn't cluster.
A favorite counts as if it was last shown half as long ago, so it comes round
about twice as often. Pinned works stay live and count toward their slice.

When the mix changes (a schedule starts or ends), the run moves each slice to
its new target in one go: it retires the excess and fills the gaps. That's
how Christmas versions all arrive on Dec 1.

The old cooldowns go away, along with `repeat_days`: 365 days for a
non-collection work, 60 for a collection work. In a hand-picked Library of a
few hundred works, a work coming back as soon as it's the least recent is the
point.

### What publishing becomes

One publisher handles every kind of work: museum works, Brian's photos,
portraits and versions. It works out each slice's target, retires and fills
per slice, and writes into the same `full/` and `matted/` folders HA already
uploads from. `portraits.sync` and the variants slot-and-swap machinery
(`slots`, `assign_slots`, `apply_versions`) are folded into it. Each live
file still records its key in `published`, so History, Now showing and
retiring keep working. Keys for the new kinds of works need a single key
space covering museum works, photos, portraits and versions. The format is
left to the builder.

HA's side doesn't change: the same folders, the same MQTT messages and the
same upload and delete flow. **No HA config change.**

## Pages and nav

New nav: **Now · On the TV · Library · Schedule · Studio · History ·
Settings** (Runs and Compare crops stay where they are).

- **Library** (`/library`): one page with two scopes. **Library** (the
  default) shows works in the Library. **All works** shows everything. View
  pills: Library, All works, Not in Library, Removed, ★ Favorites. Filters:
  search, source, kind, and the tag picker. A "show versions" toggle shows
  versions next to their originals. Cards show the state, the star, an
  "On the TV" badge, the version link, and Add, Remove or Put back. Bulk
  actions on a filtered view: Add all, Remove all, Tag all, Make versions.
  Today's Settings filters (kinds, nudes, full screen only) become filters
  here.
- **Schedule** (`/schedule`): the timeline, "Today on the TV" as one stacked
  bar with counts per slice, and the schedule editor with slices. As in the
  mockup.
- **Studio**: unchanged, plus **Version styles** beside Painters.
- **Your photos**: the import page (upload, Google Photos) stays at `/photos`,
  linked from the Library as "Add photos". Browsing photos moves into the
  Library under the "your photos" source. Setting a photo's kind stays on
  its card.
- **On the TV**: unchanged, plus **Pin** and Unpin, which move here from the
  catalogue.
- **Now**: Ban becomes **Remove from Library**. Collect becomes the star.
- **History**: Collect and Ban become the star and Remove from Library.

The old URLs `/catalogue`, `/collection`, `/portraits` and `/variants`
redirect to the matching new view *(my call)*.

## Settings

| Setting | Becomes |
|---|---|
| `choose_from` | dropped (the TV draws only from the Library) |
| `photo_share`, `photo_crop` | dropped; museum photographs are a `photograph` slice, rendered with `crop` like everything else |
| `kinds`, `allow_nudes`, `full_screen_only` | dropped as settings; Library filters |
| `repeat_days` | dropped |
| `variant_limit` | stays: versions made per night |
| `crop`, `pad_style` | stay: how works are rendered |
| `sources`, `crawl_limit`, `recrawl_days` | stay: what is crawled |
| `count`, `pool`, `slideshow`, `run_at`, `paused` | stay |
| `schedules` | stay, with slices instead of type shares |

Loading an old settings file must accept the dropped keys and ignore them.
The release notes list every dropped setting.

## Moving over (one-time, on upgrade)

Brian: "start library with my 41 and the others that have been pulled."

- **Library** starts with: every collection work (41 in production), every
  work that has ever been on the TV (live or retired, from `published`), and
  every saved Studio portrait. Banned works are excluded. "Pulled" is read as
  *ever on the TV*, not only the 92 live today. *Flagged for Brian.*
- **Banned** becomes **Removed**. **Collection** becomes **favorite**. Pins
  stay pins.
- **Existing versions** become version works tagged `ai-variant-<type>`,
  linked to their original. Versions Brian marked "don't use" become Removed.
  Older, non-current versions are kept but not eligible *(my call)*.
- **Schedules**: a type share `{halloween: 100}` becomes the slice
  `[ai-variant-halloween] 100%`. The Portraits share becomes the slice
  `[source:studio]`. A non-zero `photo_share` becomes an Everyday
  slice `[photograph]` at that share, creating Everyday if needed.
- A version live on the TV today is recorded under its original's key with
  a variant row beside it (`published_variants`). The upgrade treats that
  file as the version work being live, by mapping it when it reads, not by
  rewriting `published` rows. That way an older release can still read
  them *(my call)*.
- The first nightly run after upgrading must not churn the TV. Works that are
  live and in the Library stay live, apart from what the normal rotation
  would replace *(my call)*.
- The upgrade runs once and is recorded in the catalogue's key-value table.
  It mustn't destroy old tables, so an older version can still start against
  the database.

## Goals, non-goals, anti-goals

Goals: one place to decide what can go on the TV, one place to decide the
mix, every kind of work handled the same way, and fewer settings.

Non-goals for this phase:

- rules that add works to the Library automatically;
- narrowing a seasonal-version slice with more tags;
- OR-matching inside a slice;
- per-work show counts beyond rotation order.

Anti-goals:

- the TV ever showing something outside the Library;
- a removed work coming back on its own;
- a migration that wipes or reshuffles the whole TV on the first night;
- a new kind of work needing its own page or share setting again.

## Release

This lands after 0.11.0 (photo sources and Studio), which is merged, tagged
and deployed first as planned. This work starts on a new branch from main
and ships as 0.12.0 *(my call)*. 0.11.0 brings the Portraits share that this
design then replaces. That's acceptable because the migration converts it.

## Left to the builder

- The key format for photos, portraits and versions, and the table layout
  for Library membership, user tags and version works.
- How to split the publisher. Rounding of slice targets.
- The look of the tag picker's groups, and of the bulk-action bar.
- Whether redirects keep query strings.
- Test strategy, following the repo's existing pytest patterns.
