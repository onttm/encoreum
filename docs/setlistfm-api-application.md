# setlist.fm API — application text for Encoreum

What to paste into the API key application form at
<https://www.setlist.fm/settings/api> (you must be logged in; registration is free).

Written 2026-08-21. Kept here because it was needed twice and written down zero
times. If the app's behaviour changes in a way that makes any sentence below
untrue, **change it here too** — this is a description of what we actually do,
and it stops being useful the moment it drifts into a description of what we
meant to do.

> **Note on the name.** The form should say **Encoreum** (renamed 2026-11-16;
> see CLAUDE.md). The existing key was issued under an earlier name; update the
> application name at setlist.fm/settings/api by hand if it still shows it.

---

## Application name

```
Encoreum
```

## Short description (one line)

```
A self-hosted personal tool that labels home recordings of live concerts with the correct setlist.
```

## Full description

```
Encoreum is a free, open, self-hosted application for archiving live music in a
private home media library (Plex). It is a personal, non-commercial project — it
is not sold, has no users beyond my own household, serves no advertising, and
generates no revenue.

I use it for two kinds of material:

1. Concerts recorded from publicly available live streams.
2. Audience recordings collected from taper communities, where the artists
   permit and encourage non-commercial taping and trading.

Both arrive as bare audio or video with, at best, a date and a venue. The setlist
is the piece that makes them usable — without it a two-hour recording is a single
opaque block with no way to find a song in it.

How the API is used:

- One lookup per recording: a search for a single performance by artist and date,
  to identify what was played that night.
- The returned song titles, tour and venue are written into that file's own
  metadata so the recording is browsable and searchable in my library.
- Every match displays "Setlist data from setlist.fm" together with a link back
  to the setlist page it came from, both in the visible description and as a
  metadata tag, so the credit travels with the file.

What it does NOT do:

- No bulk downloading, crawling or mirroring of the database. Requests are made
  only for a specific artist and date, when a specific recording exists.
- No redistribution or republication of setlist data. Nothing is exposed to the
  public internet; the data stays inside a private media library.
- No commercial use of any kind.

Request volume is deliberately low and enforced in code rather than left to
chance. Lookups are spaced at least one second apart — well inside the 2.0/second
limit — and capped at 1,200 per rolling 24-hour period, under the 1,440/day
allowance, so the application stops itself before it reaches your ceiling. The
count is tracked persistently, so restarting the application cannot reset it.
Realistic use is in the low tens of requests per day.

Because a setlist is usually not posted until well after a show, a recording is
retried on a widening schedule — 6, 12, 18, 24, 36, 48, 72, 96 and 168 hours
after the performance ends — and then automatic polling stops permanently. Past
a week, a setlist almost certainly does not exist, and continuing to ask would
waste your capacity for nothing. After that point setlists are only ever added
by hand.

The application also works completely without an API key: setlists can be
entered manually. The key simply automates a step I would otherwise do by
copying and pasting.
```

## Application URL

The repository is private today. Either leave the field blank, give your own
site, or make the repo public before applying — do not invent a URL.

---

## Terms this text commits us to

These are not decoration. If any becomes untrue the key can be revoked, and more
to the point the description above becomes a false statement:

| Claim | Enforced by |
|---|---|
| Non-commercial | The project's nature; nothing to enforce |
| No bulk/crawl access | `setlistfm.find()` only ever queries one artist + one date |
| Attribution shown | `setlistfm.ATTRIBUTION`, written into the NFO plot **and** as a tag |
| No redistribution | Data is written into a private library only |
| Low request volume | `app/ratelimit.py` — 1s interval, 1,200/24h rolling, persisted; under their 2/sec + 1,440/day |
| Polling stops after a week | `captures.RETRY_HOURS`, then state `unavailable` forever |

Their [FAQ](https://www.setlist.fm/faq) and
[guidelines](https://www.setlist.fm/guidelines) are worth reading before
applying; the guideline that most affects us is that "tape" entries — recorded
music played over the PA — are not performances, and Encoreum drops them.
