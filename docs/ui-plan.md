# Hermes UI plan: desktop and mobile

Working design for the next round of UI work. `docs/plan.md` stays the design of record for
the pipeline; this file covers only the pages, and the two features they carry (album art,
choosing a candidate). Decisions that change the pipeline (a migration, a config key, a
scheduler job, an API route) are logged in `docs/plan.md` section 13 when they land.

Revision 2 (2026-09-15): after an adversarial review of the first draft. Section 7 lists
what the review changed and why, so the reasoning survives.

**Status (2026-09-16): all three phases landed.** Phase A in 0.7.2 (commit 1c6e8c8), phase C
in 0.7.3 (260f232), phase B in 0.8.0. An independent review of the implementation against
this plan found no blockers; its should-fix items (a write error stalling the art backfill,
Prefer offering a magnet-only row, the MBID input on a phone, and tests that could not fail)
were fixed before 0.8.0 shipped. Section 6 stays open.

Constraints this plan works inside (see `CLAUDE.md`, `docs/conventions.md` "UI", and
`docs/plan.md` A9):

- Server-rendered Jinja2 pages, plain forms, POST-redirect-GET with a `?notice=`. No
  JavaScript beyond `confirm()` and the one submit handler in `base.html`. Collapsible
  sections are `<details>`; navigation and prev/next are links computed server-side.
- Button availability derives from `can_transition`, never hard-coded.
- The one screen that spends ratio says what Approve fetches and where Deluge puts it
  before the click (`_approval_preview`). Nothing in this plan may grab without passing
  through that preview.
- Hermes writes nothing under `/music`, keeps no torrent file, and never opens `library.db`.
  Album art is Hermes's own derived file under `HERMES_DATA_DIR`.
- MusicBrainz goes through the one 1 req/s limiter with an identifying User-Agent. The Cover
  Art Archive is a different host and gets its own client with the same politeness.
- Write transactions are committed before external calls (conventions, "Concurrency and
  safety"); the art job is no exception.

## 1. What is wrong today

One page (`/`) holds the request form, the active table and the finished table. On desktop
the six-column active table has room. On a phone the `.scroll` wrapper keeps it from
breaking but every row becomes a horizontal scroll, and the finished table takes space from
the part that needs attention. The detail page is already close to a triage view; it lacks
prev/next, and the thing Approve will fetch is a sentence of prose in the preview block
rather than something you can read at a glance.

Two small existing defects the work will touch: the submit handler in `base.html` disables
the button even when a Reject or Cancel `confirm()` is cancelled, leaving it stuck on
"Working…" (fix: return early when `e.defaultPrevented`); and an empty request submit is a
full-page 422 with a "go back" that may lose the form.

## 2. Routes and navigation

| Route | Page | Notes |
|---|---|---|
| `/` | Request | The quick-add page. Was the queue. |
| `/queue` | Queue | Active acquisitions, filters as today. |
| `/history` | History | Terminal acquisitions, paged 50, own filters and a text search. Was "Finished". |
| `/acquisitions/{id}` | Detail | Unchanged URL; gains prev/next and the target card. |
| `/acquisitions/{id}/prefer` | Prefer (POST) | Phase C. Promotes a candidate; see 4.2. |
| `/art/{release_group_mbid}.jpg` | Art | Phase B. FileResponse, long cache header. |
| `/manifest.webmanifest`, `/static/icon-*.png` | App icons | Phase A. Static files. |

Old bookmarks: `/?state=...` and `/?origin=...` redirect to `/queue` with the same query;
`/?page=N` redirects to `/history?page=N` (paging was only ever on the finished table).

Every place that hard-codes `/` as the queue changes: the `link()` helper
(`routes.py`, builds `"/"`), "← queue" on the detail page, the "queue" link on the error
page, and the `_redirect` target for actions posted from the queue table, which now go
back to `/queue` rather than to the detail page (the detail page is the target only for
actions posted from the detail page).

**Header, desktop.** One row: brand and version · Request · Queue · History · API · mode
chips on the right. The Queue link carries a count badge when something needs a human
(`ATTENTION` states). The badge is one indexed count query per page; it is skipped on the
error page, which has no session and may be reporting a database failure. The current page
carries `aria-current="page"` and is styled as active.

**Header, mobile (below 640px).** The brand row stays; the links become a full-width row of
three equal tab buttons underneath (Request · Queue · History), each at least 44px tall.
The API link moves into the footer on mobile rather than vanishing. No expanding menu: a
tab row is one tap, always visible, and needs no state, so it needs no JavaScript. Mode
chips shrink to "dry run" / "live" and "timid" / "rules".

## 3. Views

### 3.1 Request (`/`)

The page for pulling out a phone and queueing an album.

- **Two forms, not one.** The name form has artist and album title, both `required`, and
  one large Request button. The MBID form sits inside `<details>` "or paste a MusicBrainz
  ID" with its own button. One form with both alternatives cannot use `required`, and the
  server's 422 is a full page.
- Inputs: full width, at least 44px tall. `autocapitalize="words"` on both;
  `enterkeyhint="next"` on artist and `"go"` on title. Leave `autocomplete` alone: the
  browser's artist history is useful. `autofocus` stays on artist for desktop; on phones
  it does not open the keyboard, so nothing relies on it.
- The POST waits on MusicBrainz, the library check and, for a missing album, the Prowlarr
  search. A MusicBrainz stall can hold the page for minutes with only "Working…". The
  hint under the button says "takes a few seconds; a stalled MusicBrainz can take
  longer". A second tap is deduplicated by the existing "Already requested as #N" path.
- Under the form: the timid/discovery hint as today; "N items need you → Queue" when
  non-zero; and the last five manual requests with their state badge, so the one just
  added can be seen to have gone in.
- `POST /requests` keeps redirecting to the new acquisition's detail page with the notice:
  that page already shows `ALREADY_OWNED`, `AWAITING_APPROVAL` with Approve, or the review
  table, which is the right next step after a quick add.
- A static `manifest.webmanifest` with two PNG icons served from `/static/`, a
  `theme-color`, and an `apple-touch-icon` link, so "Add to Home Screen" gives an app icon
  that opens `/`. No service worker, no script.

### 3.2 Queue (`/queue`)

Desktop table, columns in order:

| Column | Content |
|---|---|
| ~~`#`~~ | Removed 2026-09-17. The position went stale every observer tick and looked like the acquisition id in notices ("Rejected #12"); the walk bar's "3 of 103" is computed from the sort key and needs no list number. |
| art | 56px thumbnail (Phase B) or the placeholder box; `width`/`height` set, `loading="lazy"`. |
| state | State badge, as today. Hidden on mobile, where the stacked cell repeats it. |
| album | `artist – title (year)` is the link to the detail page. Below it, small, two lines at most (2026-09-23): the top two genres (MusicBrainz release-group genres, plain muted text, each a no-wrap unit, general genres dropped for specific ones; three in the detail header); then what varies between rows: the library status only when it is not `missing` (a queued album is missing by definition; an owned lossy copy is the status worth seeing) · "from *playlist*" shortened to series and day. On mobile the state badge leads the second line; the updated time is not on the phone row (the order already encodes age, the detail page has the history). |
| best candidate | Title in mono, linked to the indexer page, as today. Hidden on mobile. |
| updated | As today. Hidden on mobile. |
| actions | As today. Hidden on mobile; actions live on the detail page. |

The album title is the detail link; the cell is not one big anchor, because the desktop
cell also carries the indexer link and an anchor inside an anchor is invalid. The title's
padding gives the phone a large enough target.

```
 3  [art]  Godspeed You! Black Emperor – F♯ A♯ ∞  (1997)
           post-rock · experimental
           needs approval · from Weekly Exploration, 09-14
```

**Filters** (2026-09-17) are seven stable chips: `all` · `needs you` · `in flight` ·
`not found` · `any origin` · `requested` · `discovered`, each state chip with its count.
The three state groups are "who has the ball": a person (`ATTENTION`: approval, review,
stalled, failed), Hermes with Deluge or beets (`LIVE_STATES`: searching, submitted,
downloading, ready, importing), or the re-search schedule (`NO_MATCH`). The earlier bar
had one chip per non-terminal state with rows in it, so approving an album made its chip
migrate through four names and the bar changed shape under the cursor; nobody asks "show
me SUBMITTED rows". The per-state counts, which were the useful part of those chips, are
a muted text line under the chips ("downloading 3 · importing 1"), not links. The query
string still accepts an exact state (`state=DOWNLOADING`), so bookmarks work and an
exact-state control can return if testing shows it is missed; the candidates then are a
dropdown next to the chips or a second row inside the selected group, not the state pill
in the row, which sits too close to the title to be a click target. The states between
stages (`DISCOVERED`, `MANUAL`, `RESOLVED`, `CANDIDATES_READY`) last seconds and only
show under `all`. The state and origin chips are two flex groups, so on a phone the
origin group wraps to the next line whole and no separator is needed (a lone "·" at the
end of the first row was the first render). Each state-group chip has its group's colour
as its outline (amber for needs you, blue for in flight, red for not found) and that
colour's tint as its background when selected; the other chips are neutral with the
accent when selected. A dot inside the chip was tried first and dropped: it spent width
the phone does not have. The row's state pill keeps its own colour (a red `failed` inside
the amber "needs you" group is right: the chip says whose turn it is, the pill says what
happened). Since 2026-09-23 the pill's text is a short human label, the same everywhere
(rows, the history chips, the detail header, notices, the per-state count line):
`needs approval`, `needs review`, `import review`, `ready to import`, `candidates`,
`no match`, `owned`, `requested`, the rest the state's own word. The raw name stays the
CSS class and the API value. Reason: on a phone `AWAITING_APPROVAL` was the widest thing
on the row and pushed the playlist onto a third line. The selected chip is filled and bold; a zero count is muted. Chips wrap and grow to about 28px tall
with more padding. A horizontally scrolling chip row was considered and dropped: it hides
that more chips exist.

**Ordering** is one definition used by the queue page and by prev/next on the detail page:
`STATE_ORDER` first (needs-you states, then the in-flight states in pipeline order so the
group reads as a progress board, then the rest), then oldest `updated_at` first within a
state, then id. It is
expressed once as a SQLAlchemy `CASE` over `state` so the detail page can ask for the
neighbours with two bounded queries instead of loading every row (see 3.3). Note that
`updated_at` moves on any column write, a dry-run approval included, which is acceptable
for "oldest first" and is why id is the final tie-break.

### 3.3 Detail (`/acquisitions/{id}`)

Top to bottom:

1. **Triage bar**: `← previous · 3 of 12 · next →`. The filter is the queue's own query
   string (`state=`, `origin=`), carried on the links, so a walk through "needs you" stays
   inside "needs you". Prev and next are two queries bounded by the item's sort key (state
   rank, `updated_at`, id) within the filter; the position and total are one count each.
   Nothing on this page loads the whole table. Terminal rows show "← history" only.
2. **Title block**: art at 160px beside the text on desktop, 200px centered above it on
   mobile,
   state badge, `artist – title (type, year)`, the MusicBrainz and origin line as today.
3. **Target card**: a definition list for what Approve will fetch, replacing the prose
   preview and built from the same `_approval_preview`. Rows: title (linked to the indexer
   page) · indexer · size · format, encoding and media from the parsed title · sample rate,
   stated or inferred (`~`) · seeders and leechers · freeleech · match score · rank · Deluge
   instance, download path and label · the routing-miss warning when there is no instance
   for the indexer · the dry-run line · "preferred by you" when a human promoted this row.
   Two columns on desktop, one on mobile.
4. **Action row**: as today, in flow. On mobile a `position: fixed` bar at the bottom of
   the viewport (with `env(safe-area-inset-bottom)` padding and matching bottom padding on
   `main` so nothing hides under it) repeats only the primary action: Approve, Approve
   again, or Try next candidate. Reject with its reason field, Cancel, Search and the
   retries stay in flow, because a text input in a fixed bottom bar fights the phone
   keyboard. `sticky` was considered and does not work: a mid-page sticky element scrolls
   away once its normal position is passed, which is exactly when the bar is wanted.
5. **Resolution review table** (`NEEDS_REVIEW` only), **Candidates**, **Grab attempts**,
   **Events**: as today. The candidates table collapses on mobile to rank + title + one
   stacked meta line (indexer · size · rate · seeds · free · match · reason). Grab attempts
   keep the `.scroll` wrapper; it is diagnostic and may scroll.

**Advancing.** Approve, reject, cancel and "Try next candidate" posted from a detail page
that carries the queue filter redirect to the next item only when the action moved the
item out of the `ATTENTION` states or out of that filter, and not into `FAILED`. Otherwise
they redirect to the item itself,
as today. Concretely:

- Dry-run approve leaves the row in `AWAITING_APPROVAL` and "needs you", so it stays on
  the item, which is the screen that explains dry run and offers "Approve again".
- A submit that ends in `FAILED` raises nothing (Deluge down, no candidate left); it stays
  on the item.
- A live approve that reaches `SUBMITTED`, or a reject or cancel, advances, from the
  unfiltered queue too: the row is still listed there, lower down, but it is done with
  (found in the first live week: staying on an approved album felt like nothing happened).
- The next item is computed at POST time from the acted item's sort key, not from a hidden
  field rendered with the page: the observer and importer ticks change states every 45 s,
  so a rendered id goes stale and a newer higher-priority item would be skipped.
- The notice on the next item names what happened to the previous one and links back:
  "Approved #12, sent to Deluge. Next up:". When there is no next item the redirect goes
  to `/queue` with "Nothing else needs you".
- The API and CLI are untouched: advancing is a UI-route concern.

### 3.4 History (`/history`)

Terminal acquisitions, newest first, paged 50 as today. Columns: art · album cell (title
line linked to the detail page; then origin or playlist; on `IMPORTED` rows, later, the
library path from the import event) · state · finished (`updated_at`). Chip filters for
state (`IMPORTED`, `ALREADY_OWNED`, `REJECTED`, `CANCELLED`) and origin. Same mobile
collapse as the queue.

A **text search** box (artist or title, `LIKE` on `album_target.artist_name` and `title`,
falling back to the signal's requested fields for unresolved rows). At fifty rows a week
history is where you look for "did that album ever come through", and no page has a
search today. The same box goes on the queue once it exists.

A "gave up" filter (NO_MATCH rows the re-search job closed) was in the first draft and is
out: those rows are `REJECTED` and only the event message says why. If it is wanted later
it needs a reason column on `Acquisition`, which is a migration.

### 3.5 Tables in general

One rule in `base.html`: below 640px, `th.opt, td.opt { display:none }`. Every table marks
its secondary columns `opt`; the stacked album cell repeats what those columns held, so no
information is lost on a phone and nothing depends on width in a test. The rows are still
sent, which is fine at 50 rows a page. `.scroll` stays as the fallback for tables that are
allowed to scroll (grab attempts, the resolution review table).

State badges pair colour with the state name, so colour is never the only signal. Focus
styles are the browser's; nothing removes outlines.

## 4. Features

### 4.1 Album art

Worth doing, as a background job that can never slow a request or a pipeline stage.

- **Source**: Cover Art Archive. Try `release/{preferred_release_mbid}/front-500` when the
  target has a preferred release, else `release-group/{mbid}/front-500`. Public, no
  credentials. It answers with two redirects to archive.org, which is slow and sometimes
  down; that is why it must not run inline during resolution. A 404 means no front art.
- **Client**: `hermes/integrations/coverart.py`. httpx, follows redirects, User-Agent from
  `MUSICBRAINZ_CONTACT`, its own 1 req/s limiter, 30 s timeout (archive.org tail latency
  is routinely over 10 s), `health()` makes no network call and never contributes to
  `/healthz`: art is cosmetic, and a 503 there means the pod is unhealthy.
- **No image processing.** CAA serves `front-500` (a JPEG, typically 30–60 KB) already
  square-ish and sized; it is stored as served, after checking it is a JPEG or PNG under
  1 MB. That covers the 56px thumbnail and the 200px detail image at 2x without Pillow, a
  C extension the image does not otherwise need. If a later need for WebP or exact
  squares appears, conversion is a contained change in `services/art.py`.
- **Storage**: `HERMES_DATA_DIR/art/<release_group_mbid>.jpg`, keyed by release group so
  an album that is requested twice shares one file. That is the existing `/data` volume
  (the PVC in `deploy/hermes.yaml`, `./dev/data` in compose). At Weekly Exploration
  volume this is well under 200 MB a year.
- **Tracking**: three columns on `AlbumTarget`: `art_status` (`pending`, `fetched`,
  `missing`, `failed`), `art_checked_at`, and `art_failures` (failures in a row, for the
  backoff). `failed` retries after an hour, then daily; `missing` retries after thirty
  days, because CAA gains art all the time. This is a migration.
- **Scheduling**: `hermes/services/art.py` with `tick()` on the APScheduler every five
  minutes plus the usual immediate run as the startup reconcile, working through
  `pending` targets in small batches. The session is committed before each fetch and the
  row re-read before the write, per the concurrency rules. The request route asks the
  scheduler for one immediate run after a manual request resolves, using the existing
  `modify_job(next_run_time=now)` pattern, guarded for `app.state.scheduler is None`
  (tests build the app with `scheduler=False`; the CLI has no scheduler). A CLI
  `hermes art` runs one tick.
- **Serving**: `GET /art/{mbid}.jpg` as a `FileResponse` with
  `Cache-Control: public, max-age=31536000, immutable`; 404 when there is no file. The
  template renders `<img>` only when `art_status` is `fetched`, with `width`, `height`,
  `loading="lazy"` and `decoding="async"`, so a 50-row page does not fetch 50 images on
  first paint and does not reflow as they arrive. Otherwise a CSS placeholder box with the
  artist's initial. No broken images, no script.
- **Config**: one key, `art.enabled` (default `true`), so a shared deployment can switch
  the archive.org calls off. `config.example.yaml` gains the key.
- **Not done**: reading beets' `cover.jpg` from `/music` (Hermes does not mount it and does
  not read the library), or fetching art for candidates (they have no MBID).

### 4.2 Choose a different candidate

Small and high value; it is what the approval gate is for. The first draft posted the
choice straight to Approve; the review showed that bypasses the preview and, in live
mode, spends ratio in one tap. The design is now two steps: prefer, then the existing
Approve.

- **UI**: a "Prefer" button on every ranked candidate row of the detail page, shown when
  the state is `AWAITING_APPROVAL`, `CANDIDATES_READY` or `STALLED`. It posts to
  `POST /acquisitions/{id}/prefer` with `candidate_id` and redirects to the same page. The
  target card then shows the chosen row (it is built from `next_candidate`, which reads
  rank order) with "preferred by you", and the existing Approve grabs it with the routing
  check and dry-run line intact. The button is its own column, visible at every width.
- **Service**: `approval.prefer(session, acq, candidate, by)`. It renumbers ranks so the
  chosen row is 1 and the others keep their relative order, writes an event "preferred
  *X* over *Y* (by ui)", and commits. `submit()` is untouched and the observer's stall
  fallback continues from the new order.
- **Refusals**: a candidate whose Prowlarr guid is in `attempted_guids` (a re-search can
  re-offer a tried torrent as a new row) is refused with a 409 naming the earlier attempt,
  because `next_candidate` would skip it silently and Approve would grab something else.
  A candidate from another acquisition is a 404.
- **Rejected rows**: "Use anyway" only on rows whose reason is "acceptable, but beyond
  keep_candidates": they passed every policy check and lost on rank. Rows rejected by a
  quality rule do not get the button in this phase. The sample-rate rule is re-checked
  inside `submit()` on the torrent's own file sizes, so a UI override would be reversed
  at submit; honouring it needs a per-candidate override flag, and the matching and
  release-type rejections describe a different album, where a wrong grab spends ratio and
  a Grab Limit slot. Whether quality overrides are ever needed is an open point after a
  few live weeks (section 6).
- **Lifetime**: "Search again" deletes candidates no attempt references and re-ranks from
  scratch, so a preference does not survive a re-search. The target card says so
  ("preferred by you; a new search resets this").
- **Modes**: dry run and timid behave exactly as today; prefer only reorders.
- **Parity**: `POST /api/acquisitions/{id}/prefer` with `{"candidate_id": n}`, and
  `hermes prefer <acquisition-id> <candidate-id>` on the CLI. Logged in `docs/plan.md`
  section 13 as an API addition.

## 5. Phases

| Phase | Scope | Version |
|---|---|---|
| A | Routes split (`/`, `/queue`, `/history`) with the bookmark redirects, header and mobile tabs with the needs-you badge, request page (two forms), queue and history tables with the `opt` collapse, history text search, ordering as SQL with prev/next, detail target card, fixed mobile action bar, advance-on-action rules, `defaultPrevented` fix, manifest and icons | patch |
| C | Prefer a candidate: service, refusals, UI button, "Use anyway" on keep_candidates rows, API route, CLI command, plan.md §13 entry | patch |
| B | Album art: coverart client, migration, `art.enabled`, scheduler job with the immediate-run hook, art route, `<img>` attributes and placeholder | minor |

Versions follow the rule in `CLAUDE.md`: the minor bumps for a config key, a migration or
an agent route; A and C add none of those, so they are patches even though they add pages
and an API route. B has a migration and a config key.

C before B: it builds on the new detail view and needs no schema change, while B brings a
migration, a config key and a new external call and deserves its own bump. Phase A's queue
and detail templates leave room for the art column (the placeholder box) so B is a template
change, not a relayout.

**Files.**

- A: `hermes/ui/routes.py`, `hermes/ui/templates/base.html`, `queue.html` (becomes the
  queue), new `request.html` and `history.html`, `acquisition.html`, `error.html`,
  `hermes/app.py` (the `/` redirects, static files, manifest), `docs/conventions.md` "UI"
  (page list, the advance rule, the fixed bar).
- C: `hermes/services/approval.py`, `hermes/api/schemas.py`, `hermes/api/routes.py`,
  `hermes/ui/routes.py`, `acquisition.html`, `hermes/cli.py`, `docs/plan.md` §13,
  `docs/conventions.md` "UI" (prefer sits before the preview, never bypasses it).
- B: `hermes/integrations/coverart.py`, `hermes/services/art.py`, `hermes/domain/models.py`,
  an Alembic migration, `hermes/config.py`, `config.example.yaml`, `hermes/app.py`
  (scheduler job, art route), `hermes/cli.py`, `deploy/hermes.yaml` (no change: `/data`
  already mounted), `docs/plan.md` §13.

**Tests.** In `tests/integration/test_ui.py` the route split breaks, by name:
`test_review_flow_shows_request_and_lets_a_human_pick`,
`test_queue_offers_search_for_resolved_rows`,
`test_failed_unresolved_request_can_be_retried`,
`test_not_found_offers_mbid_instead_of_retry`,
`test_candidate_title_links_to_the_indexer_page`, and
`test_queue_filters_and_paging` (which also asserts "Finished (1)" and pages on `/`).
`test_notice_confirms_and_dry_run_is_explained` only needs "Timid mode" on `/`, which the
request page keeps. New:

- A: request page renders both forms and the recent list; `/` with `state=` redirects to
  `/queue` and with `page=` to `/history`; the queue link carries the count and the error
  page renders without a session; prev/next are the right neighbours under a filter and
  the position matches the queue's number; advance happens on a live approve, a reject and
  a cancel, and does not on a dry-run approve or a `FAILED` submit; the last item goes to
  `/queue`; actions posted from the queue return to `/queue`; `opt` columns carry the
  class and the stylesheet has the rule; history filters, paging and text search; the
  submit handler fix (a test can only assert the script text).
- C: prefer renumbers ranks, writes the event, and `next_candidate` returns the chosen row;
  the target card shows it; an attempted guid is refused with 409; a foreign candidate is
  404; "Use anyway" appears only on keep_candidates rows; a re-search resets the order;
  API and CLI paths.
- B: the tick on a 404 marks `missing`; on a fetched image (respx, a small JPEG fixture,
  following a redirect) writes the file and marks `fetched`; on a timeout marks `failed`
  with a checked-at and the backoff is honoured; the preferred release is tried before the
  group; the art route serves the file with the cache header and 404s otherwise; the
  template renders the placeholder when there is no art and the lazy `<img>` when there
  is; `art.enabled: false` schedules no job; the immediate-run hook is a no-op without a
  scheduler.

## 6. Open points

1. Mode chips on the phone header, or only next to the action row where the click spends
   ratio. Recommended: both.
2. "Use anyway" on quality-rejected candidates: decide after a few live weeks whether the
   ranker's rejections are ever wrong. If yes, it needs an override flag on `Candidate`
   that `submit()` honours (a migration), not a UI-only rank change.
3. A reason column on `Acquisition` for "gave up" rows in history, if that filter is missed.
4. Whether the queue's text search should also match candidate titles.

## 7. What the review changed

An adversarial review of the first draft (UX practice and integration lenses, verified
against the code) led to these changes:

- Advance-on-action now advances only when the action left the filter and not into
  `FAILED`, with the next item computed at POST time. The draft would have looped in dry
  run, skipped failures, and used a hidden id that goes stale within one observer tick.
- Choosing a candidate is a separate prefer step before the existing Approve, never a
  one-tap grab. Attempted torrents are refused; quality overrides are deferred because
  `submit()` re-checks the sample rate on the torrent itself and would reverse them.
- The album title, not the whole cell, is the detail link (nested anchors).
- The mobile bar is `fixed`, carries only the primary action, and the stuck "Working…"
  after a cancelled confirm is fixed on the way.
- Two request forms so `required` works; input hints corrected; the slow-call warning.
- Ordering moved from Python-over-all-rows to a SQL `CASE` with bounded neighbour queries;
  the detail page reuses the queue's filter parameters.
- Art drops Pillow (store `front-500` as served), tries the preferred release first, uses
  a 30 s timeout with hourly and monthly retries, lazy-loads thumbnails, guards the
  scheduler hook, and never affects `/healthz`.
- History drops the uncomputable "gave up" chip and gains a text search.
- The route split's full blast radius (error page, `link()`, queue-originated redirects,
  six named tests) is enumerated; `/?page=N` goes to `/history`.
- Chips wrap at 32px instead of scrolling; the API link survives on mobile in the footer;
  "rules" replaces "auto"; the desktop table keeps a State column.
