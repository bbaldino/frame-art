# Mix schedules: design

Status: draft for Brian's review, 2026-10-28. Builds on
[2026-10-27-variants-design.md](2026-10-27-variants-design.md) (seasonal
variants, released in 0.9.x).

## Why

Today the Seasonal page has one mix (e.g. Halloween 75%), set by hand, with no
dates. Brian wants the mix to follow the calendar on its own: a fall look for
the autumn, Halloween through October, and a birthday look on someone's
birthday. These aren't all seasons or holidays, so this is a generic
**schedule**: a date range plus the mix wanted for those dates. Ranges overlap
(fall Sep–Dec, Halloween Oct 1–31, a birthday on one October day), and the
narrower one changes what the wider one shows.

Success: Brian sets the year up once, and the TV changes by itself on the right
nights, getting more themed each year as versions pile up, without him
touching the mix again.

## Goals

- Schedules can be added, edited and deleted in the web UI. Each has a name, a
  from/to date that repeats every year, and shares for any of the variant types.
- Overlapping schedules combine by the "off the top" rule below.
- The nightly run makes versions for running schedules and for those starting
  within a week, so that a schedule is themed from its first day as far as the
  nightly limit allows.
- On the night a schedule starts or ends, the TV switches over in that one
  night.
- The Seasonal page shows the year as a timeline and today's resulting mix.

## Non-goals (for now)

- One-off schedules tied to a single year. Every schedule repeats yearly.
- Times of day. A schedule's day starts at the nightly run.
- Ramping a schedule in or out over several days.
- Per-schedule nightly limits, or choosing which works a schedule uses.
- Creating a variant type from inside the schedule editor (that stays in the
  Types section).

## Anti-goals

- Costs that run away: the nightly limit is the one throttle, and schedules
  never get around it. A 100% one-day schedule "wants" the whole pool but only
  ever gets what the limit allows each year.
- The TV falling back to Samsung art on a big swap night. (Fixed on the HA
  side on 2026-10-28: the sync moves off the on-screen picture before deleting
  it, and has no per-run delete cap; frame-art 932909a.)
- A themed share that shows originals because its versions aren't made yet,
  while a wider schedule has versions ready (see "Shortfall" below).

## Concepts

### Schedule

- **Name**, free text (e.g. "Autumn", "Halloween", "Sam's birthday").
- **From** and **to**: month and day, inclusive, repeating every year. A range
  may wrap the new year (Dec 20 – Jan 5). From = to is a one-day schedule.
- **Shares**: percent per variant type, for any number of types, summing to at
  most 100. A schedule's **total share** is that sum: how much of the screen it
  claims while it runs.

### Today's mix

The effective mix for a day is built from the schedules covering it:

1. Order them narrowest first (fewest days in the range). Ties: the one that
   starts later in the year wins (counted from Jan 1; for a range that wraps,
   its start date). *My call.*
2. The narrowest takes its total share of the screen, split by its own shares.
3. Each next schedule takes its total share **of what's left**, split by its
   own shares. And so on to the widest.
4. Whatever remains is originals. A day with no schedule is 100% originals.

Worked example (Brian's): Fall 30%, Halloween 50%, birthday 30%.

- Oct 15: Halloween 50; Fall 30% of the remaining 50 = 15; originals 35.
- Oct 28: birthday 30; Halloween 50% of 70 = 35; Fall 30% of 35 = 10.5;
  originals 24.5.

A schedule at 100% replaces everything below it for its days. If two
schedules share a type, that type's shares add up.

Rounding to whole works is the builder's call, as long as the counts sum to the
live pool.

### Shortfall: only made versions count

A schedule's share is a **target**. On a given night a type shows only on works
that have a usable version of it; the nightly limit decides how fast the target
fills, and versions are kept when a schedule ends, ready for its next year.

When a schedule has fewer versions ready than its share, **the rest of its
share falls to the next schedule down**, not to originals. A 30% birthday with
10 versions ready shows those 10, and the rest of the screen follows Halloween,
Fall and originals as if the birthday had only claimed 10 works. *My guess,
confirmed by Brian ("those sound good").*

This also bounds TV churn: a one-day schedule swaps only the versions it has.

### Lookahead and the nightly limit

- A schedule counts as **upcoming** from 7 days before its first day. Fixed for
  now. The nightly run makes versions for upcoming schedules as well as running
  ones, but they aren't shown until the schedule starts.
- The nightly limit (today "New versions made per night", default 10) is one
  shared budget. It goes to schedules in the same order as the overlap rule,
  narrowest first, across running and upcoming schedules together. *My call.*
  So in the week before a birthday, the birthday gets the night's versions
  ahead of a Halloween that's already running.
- For an upcoming schedule, "what it needs" is computed as if it were running
  on its first day.

### Start and end

- A schedule takes effect at the first nightly run on or after its first day,
  and ends at the first nightly run after its last day. *My call* (not
  midnight).
- The whole switch happens in that run: slots are recomputed against the new
  mix and every swap goes out in one sync. HA has no delete cap any more;
  deletes take about 0.2 s each and uploads about 5 s each, so a 75-work
  swap is roughly 6 minutes of uploading.

### Leap day

A schedule on Feb 29 runs on Feb 28 in other years. *My call.*

## The existing mix

The single mix and its Mix section go away. On upgrade, a non-empty mix becomes
a schedule named "Everyday", Jan 1 – Dec 31, with the same shares. It's an
ordinary schedule: editable and deletable. An empty mix migrates to no
schedules. *My call.*

Places that today ask "is this type in the mix" (keeping a hand-picked or
hand-generated version on the TV) ask "is this type in today's mix" instead.

## Web UI (Seasonal page)

Mockups agreed with Brian: the year timeline (layout A), with the editor in
which types are added from a list (editor v2).

Top to bottom:

1. **Timeline.** Twelve month columns, one bar per schedule spanning its dates,
   widest at the top and narrowest at the bottom (the winner). A wrapping range
   draws as two pieces. A marker shows today. Each bar shows the schedule's name
   and total share; one-day bars may be too narrow for text, so their label sits
   beside them.
2. **Today's screen.** One strip split by today's resulting mix, by type plus
   originals, using what is actually on the TV (after shortfall), with the
   target alongside.
3. **+ Add schedule**, and the global **New versions made per night** setting
   (moved here from the Mix section).
4. **Editor.** Clicking a bar (or Add) opens the editor under the timeline:
   - name; from and to (month + day); "every year";
   - one row per type in the schedule: name, share %, a progress bar and
     "N of M ready" (M = the works that type gets at full target), and × to
     remove it;
   - "+ Add a type", a list of the variant types not yet in this schedule;
   - a readout: "This schedule takes 30% of the screen; the rest follows
     whatever wider schedule is running";
   - Save, and Delete schedule.
5. The existing **Progress**, **Types** and **Generate for…** sections stay.
   Progress's "Wanted" column becomes today's target share.

Validation as today: shares are whole numbers 0–100, each schedule's shares sum
to at most 100, dates must be real month/days; a rejected form comes back
showing what was typed, with the errors.

Only the web page changes; the page stays LAN-only, holds no HA token, and
never talks to the TV.

## Left to the builder

- Where schedules are stored (the settings file beside `mix`, or a catalogue
  table) and the upgrade from `Settings.mix`. Note the settings form today
  carries `mix` and `variant_limit` through on save (web.py, around line 523),
  and anything moved must keep surviving a settings save.
- How slot assignment (`variants.assign_slots`, `needed`) is extended for
  today's mix, the shortfall rule and lookahead generation for slots that aren't
  shown yet. The current rules for manual slots and for which works keep their
  slot should carry over.
- How the timeline is drawn (plain HTML/CSS is enough; no new JS framework).
- What "today" is: the date of the nightly run in the service's timezone.
- Settled by the builder check as minor (*my calls*):
  - Two schedules with identical ranges: the one created later wins.
  - A schedule's share for a deleted variant type is ignored, as the mix's is
    today.
  - Which works an upcoming schedule makes versions for should stay stable from
    night to night, so a week of lookahead doesn't scatter versions across
    different works.
  - Hand-picked (manual) slots count toward their type's share, as today.
  - Edits to schedules take effect at the next nightly run, like mix edits
    today.

## Testing notes

- Today's mix: the worked examples above, a 100% schedule, a wrap-around range
  on Dec 31 and Jan 1, a tie in length, no schedules at all, Feb 29.
- Shortfall: a narrow schedule with fewer ready versions than its target hands
  the rest to the next layer.
- Lookahead: 7 days before a schedule, its versions are made but not shown; on
  its first nightly run they're shown.
- Budget order: a narrow upcoming schedule beats a wider running one.
- Migration: an old mix becomes "Everyday"; an empty one becomes nothing.
