# Conventions

The rules the code follows that are not visible from the type signatures. `CLAUDE.md` is the
map (commands, architecture, invariants); this file is the detail it points at. `docs/plan.md`
is the design and its decision log. When a review finding changes a rule, record it here and
add a regression test in `tests/integration/test_review_fixes.py`.

## Concurrency and safety (from the 2026-09-07 reviews; keep these true)

- Commit before every external call. SQLAlchemy sessions are sync inside async code, so a
  write transaction held across an `await` blocks every other coroutine on the SQLite busy
  timeout. Pattern: `session.commit()` right before `await client.something()`.
- After an `await`, re-check `acq.state` (`session.refresh`) before transitioning if another
  actor (tick, click) could have moved it. A scheduled job that lists rows and then waits on
  the network must `session.refresh()` each row before acting on it: `transition()` writes
  unconditionally. `discovery._research` is the pattern.
- Never interpolate `{exc}` for an httpx error whose URL carries a secret (Prowlarr download
  URLs, Navidrome calls). The clients re-raise sanitised errors; keep it that way.
- Any code path that ends in `submit()` must be gated by `dry_run` and `approval.timid`
  (`decide`, `approve`, and the observer fallback are the three today).
- Never delete a `Candidate` a `GrabAttempt` references; supersede it (`rank=None`).
- A submit can die between the Deluge add and the attempt commit: `submit()` first looks
  for a torrent at the acquisition's own download path and adopts it (`_adopt_existing`).
  Keep that path per acquisition; it is what makes a torrent attributable without a file.
- beets adds the album to its library before copying files: an import is only done when the
  agent job exited 0 with no error and the album path is under `beets.library_root`.
- Discovery never marks a track failed for a dependency being down: `_process` raises
  `UpstreamUnavailable`, the playlist stays `partial`, the next tick resumes at the pending
  track. Only a verdict (no such recording, nothing acquirable) is final.
- One non-terminal acquisition per target is a partial unique index
  (`uq_acquisition_active_target`); check for an active one first, and expect an
  IntegrityError to mean another process won the race.
- Migrations that add a NOT NULL JSON column need `server_default='{}'` or the upgrade fails
  on existing rows.

## Ranking and the quality policy

- Weights live in `ranking.py` and fall in two groups. **Quality**, what the release is:
  match 2, media preference 2, encoding preference 1.5, indexer preference 1, CD log+cue
  0.25, and edition penalties from `quality.edition_penalties` (deluxe, anniversary and
  expanded 1.0; remaster 0.1; other reissue labels 0.8; an unknown edition flag counts as a
  reissue). **Tie-breakers**, what the release costs and how fast it arrives: seeders at
  most 0.02 (capped at ten seeders), freeleech 0.01, smaller size at most 0.004.
- **A tie-breaker must never decide between releases of different quality.** The three
  together reach 0.034, under half the smallest quality difference the ranking can express
  (the remaster penalty, 0.1). `test_ranking.py::test_tiebreaks_cannot_outweigh_quality`
  fails if any weight moves far enough to break that, in either direction. Seeders were
  worth up to 0.2 until 2026-09-15 and decided two of the twenty captured scenarios, both
  times in favour of the more heavily reissued release. `quality.min_seeders` is what keeps
  a dead swarm out, not the ranking -- no seeder count can make up a quality difference, so
  that filter is the whole seeder policy.
- A preference list spends its weight across its entries, so the gap between neighbours is
  the weight over the length of the list: `media_preference` with five entries steps by 0.4,
  `encoding_preference` with two by 0.75. Shortening a list widens its gaps -- an entry that
  is not on the list scores zero, so `encoding_preference: [lossless24]` is worth the full
  1.5 over everything else rather than 0.75.
- The log/cue bonus must stay below the media preference step or a logged CD outranks a
  preferred WEB release. The deluxe-class penalties must stay above that step, so a plain CD
  rip beats a deluxe WEB release but not a plain one.
- `quality.encoding_preference` is 24-bit first and `quality.max_sample_rate_khz` (96) keeps
  192 kHz out; these two go together. All three knobs are the user's taste, in config.
- Sample rate is inferred, not read. `ranking.estimate_rate` divides size by the target's
  MusicBrainz playing time (`AlbumTarget.track_lengths`, cached only when the list is
  complete) at search time, with a 10% margin, and is trusted for the plain album only
  (editions use file count times mean track length). `ranking.torrent_rate_verdict` checks
  the per-file median from the fetched torrent at submit time and skips the candidate.
  Bands: up to 1900 kbps is 48 kHz, up to 3700 is 96, up to 7500 is 192, above is more.
- `tests/integration/test_ranking_scenarios.py` ranks twenty captured tracker A searches
  (`tests/fixtures/prowlarr/gazelle/`, download links redacted). Add a case there whenever a
  real result set ranks wrongly rather than adjusting a weight by hand.

## Matching

- `matching.title_similarity` splits tails (soundtrack, alternate title, subtitle) off both
  titles. A subtitle only the tracker title carries caps the score at 0.8; when both carry
  one the score is the lower of the head and tail similarities, with numbers compared
  exactly ("Vol. 2" is not "Vol. 3").
- `artist_variants` accepts "performed by" and "feat." tails so a credited guest does not
  fail the artist check.
- Add odd real titles to the corpus (`tests/fixtures/titles/`) rather than special-casing
  them in the parser. `uv run hermes parse-title "<title>"` shows how a title is read.

## Discovery

- `tick()` lists the user's generated playlists plus `listenbrainz.extra_playlists`;
  `ingest_playlist()` records a `Playlist` row and one `Signal` per track, resolves each
  recording with one MusicBrainz call (`resolution.resolve_recording`: Album > EP > Single,
  earliest first, the artist credit must match) and either attaches to the acquisition in
  flight or creates one and runs `requests.continue_with_target`.
- Ignored playlists fetch no tracks. Unacquirable recordings are `ignored` on the Signal,
  never `NEEDS_REVIEW`: automated signals get no review page.
- `research_tick()` re-searches `NO_MATCH` rows after `search.retry_days`, gives up
  (`REJECTED`) at `search.max_retries`, finishes `RESOLVED` rows through the library check,
  and retries transiently `FAILED` rows that have no download. Both ticks share one lock.

## Importer

- Success is verified by asking the library for albums carrying `hermes_acquisition=<id>`,
  never by the exit code.
- The `--search-id` release comes from `pick_release(rg, hint_year, shape)`: official first,
  then matching track count, disc layout and medium against `GrabAttempt.download_shape`,
  then year, country, and a plain release over a disambiguated variant.
- After `IMPORTED` the importer calls the agent's `POST /history` so a later manual
  `beet import -I` on the downloads directory skips the folder; a failure there is a
  warning event, not a state change.

## UI

- Jinja2 pages (request form at `/`, queue at `/queue`, history at `/history`, acquisition
  detail, error page) using plain forms and POST-redirect-GET; the redirect carries a
  one-line `?notice=` that the base template shows. The page design is `docs/ui-plan.md`.
- No JavaScript beyond `confirm()` on Reject/Cancel and the one inline submit handler in
  `base.html` that disables the button and says "Working…" (and leaves it alone when a
  confirm() was cancelled: `e.defaultPrevented`).
- Mobile is CSS only: below 640px the nav becomes a tab row, columns marked `opt` are
  hidden (the stacked album cell repeats what they held), and the detail page repeats its
  primary action in a `position: fixed` bottom bar. Tests check the classes and the rule.
- The queue's order is one SQL expression (`QUEUE_ORDER` in `ui/routes.py`: attention
  states first, oldest `updated_at` first within a state, then id). The detail page's
  prev/next use it with bounded queries, never by loading the table.
- A detail page opened from the queue carries `walk=1` plus the queue filter (`state=`,
  `origin=`) on its links and form actions. An action then advances to the next item only
  when it moved this one out of that filter and not into `FAILED` (`_finish`), so a
  dry-run approval and a failed submit stay on the page that explains them; the next item
  is computed at POST time from the acted row's sort key. Inline queue actions carry
  `back=queue` and return to the queue. Without either, actions redirect to the same page.
- The header's Queue link carries the needs-you count on every page with a session; the
  error page has none and shows no badge.
- Button availability is derived from `can_transition`, never hard-coded.
- `HTTPException`s on non-API paths render `error.html` (see `create_app`); API paths stay
  JSON. Times render via the `dt` filter with a UTC label.
- An acquisition links to its originating `Signal`, which is how unresolved requests are
  named on the page (`requested_label` in `api/schemas.py`).
- `NEEDS_REVIEW` pages show the resolution candidates from the event data with a "Use this"
  form that calls `services.requests.resolve_manually` ("Use anyway" on rows the resolution
  policy rejects: a human pick overrides the policy for that request).
- Every page, error pages included, carries the mode badges from `_mode()` (dry run or live,
  timid or auto-approve).
- The approval page states what Approve fetches and where Deluge puts it
  (`_approval_preview`, built from `submit.next_candidate` and `policy.deluge.instance_for`)
  and shows a routing miss before the click; a dry-run approval stays visible afterwards
  (`approved_by`, "Approve again").
- Failed requests: "Retry request" only when the FAILED event carried `retryable: true`
  (`services.requests.can_retry_request`); a "no such album" verdict gets an MBID form
  instead. A retried or superseded acquisition is closed as `CANCELLED` with `continued_as`
  in the event data, rendered as "Continued as #N". Repeat requests redirect with "Already
  requested as #N" (`ui_request` compares the acquisition's `signal_id` with the newest
  signal before the call).

## Test fixtures

- `tests/fixtures/` holds captured real responses: MusicBrainz searches, lookups and
  recordings, the "busy" error, a 404, ListenBrainz listings and a playlist anonymised to
  `lbuser`, a PandaCD search, twenty tracker A searches with download links redacted, and a
  public-domain LibriVox torrent with web seeds under `torrents/`. Prefer adding a captured
  payload over hand-writing one.
- `tests/fixtures/titles/` is the title-parser corpus: `gazelle.txt` (captured, 804 titles)
  is the reference for the Gazelle family, `gazelle-handwritten.txt` is a hand-written set in
  the Gazelle definition's format, `pandacd.txt` is captured.
- A fixture captured from a private tracker carries no site URL and nothing tied to an
  account: download links are redacted, torrent-page links point at `tracker.example`, and
  the indexer is named "Gazelle". Tracker names in prose are "tracker A" and "tracker B".
- Tests never read `.env`: fixtures pass `_env_file=None`. Tests call
  `observer.tick()` / `importer.tick()` / `discovery.tick()` directly and build apps with
  `scheduler=False`.

## Working on Windows and Docker Desktop

- On Windows/Git Bash, SQLite URLs must be relative or use a `C:/...` path, never `/c/...`.
- When passing container paths to `docker run -e` from Git Bash, prefix the command with
  `MSYS_NO_PATHCONV=1` or `/app/...` gets rewritten to a `C:/Program Files/Git/...` path.
- Never publish Deluge's UDP port in compose, and expect UDP tracker announces from the
  Deluge container to be flaky under Docker Desktop's userspace network stack (every torrent
  shows "tracker status: timed out" while the same announce from a plain socket in the
  container succeeds). Switching Docker Desktop to kernel networking for UDP (Settings >
  Resources > Network) is the fix; a torrent that did get one good announce still downloads.
  Prefer web-seeded fixtures for anything that must be reliable.
- The Windows console is cp1252: write non-ASCII output to a file rather than printing it.
