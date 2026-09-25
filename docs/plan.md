# Hermes — Development Plan

Working plan, last brought in line with the code on 2026-09-07 (M0–M5 done). The "Why"
column in section 3 records the rationale for each decision, including the API facts that
were verified. Items marked **[Q#]** depend on an open question in section 12. Section 13 is
the decision log; the status note at the end of section 11 is where to resume.

## 1. Purpose and design rules

Hermes turns ListenBrainz recommendations and manual album requests into album-level
torrent acquisitions, and hands completed downloads to beets. It orchestrates; it does not
own the library, the trackers, the downloads, or playback.

Rules that every design choice must satisfy:

1. **beets is the library.** Hermes never writes to `/music` and never edits `library.db`.
2. **Album is the unit.** Identity is the MusicBrainz release group. Owned means any release in the group.
3. **Seeding is sacred.** Hermes never deletes, pauses or modifies a torrent or its files. beets copies; it never moves, links or retags source files.
4. **Nothing spends ratio without a policy saying so.** Dry-run by default, a human approves every grab by default (timid mode), and the indexer's own grab limit bounds the blast radius.
5. **Every decision is explainable.** Each acquisition has an event log a human can read to see why a torrent was chosen or rejected.
6. **Restart-safe.** All state is in SQLite; on startup Hermes reconciles against Deluge and beets, never against memory.
7. **Designed for sharing, built for one cluster.** Hermes is developed against the home cluster but should be usable by others later, possibly with a different torrent client, tracker, or music source. Three habits keep that door open without building for it now:
   - Cluster facts (paths, URLs, instance names, tracker names) live in config, never in code. Comments and docstrings describe them as examples, not as the design.
   - Services depend on small interfaces, not concrete clients: a torrent client is "add with location and label, report status by hash"; a release source is "search artist/album, return a torrent file"; a discovery source is "yield recording MBIDs with provenance". Each interface has one implementation until a second is needed.
   - No plugin system, registries, or abstract hierarchies. A second implementation is a new file in `integrations/` selected by a config key.
   `deploy/` and the default quality policy are legitimately cluster- and taste-specific and stay that way.

## 2. Scope

### v1 (this plan)

- ListenBrainz playlist ingestion (Weekly Exploration; every generated playlist configurable as acquire / ignore, plus arbitrary playlist MBIDs) **[Q5]**.
- Manual album request by artist + title, or by release group / release MBID.
- Recording → release group resolution with an explicit policy.
- beets library check: missing / owned.
- Prowlarr search, title parsing, ranking, candidate list.
- Approval queue: timid mode (a human approves everything) or auto-approve rules; no budget (A8).
- Hermes-driven grab: fetch `.torrent` via Prowlarr, add to Deluge with label and location.
- Download observation, completion detection, stalled fallback to next candidate.
- Serialized `beet import` with `--search-id`, verification, needs-review state.
- Navidrome scan trigger after import.
- Minimal server-rendered UI: queue with filters, acquisition detail/timeline with the approval preview, approve/reject, manual request and review, mode badges on every page.
- Dry-run mode.

### v1.5

- Upgrade path: lossy → lossless replacement (uses beets `duplicate_action`).
- ListenBrainz raw collaborative-filtering recommendations endpoint as a second source.
- Per-tracker account guard via the Gazelle API (ratio, required ratio, token count): refuse grabs that would drop the ratio under required plus a margin unless freeleech or tokened; real token accounting. Optional per-acquisition token decisions.

### Explicitly out of scope

- Playlist sync into Navidrome. Use `navidrome-listenbrainz-daily-playlist` (Navidrome ≥ 0.63, matches by MBID) **[Q7]**.
- 16-bit → 24-bit upgrades.
- Classical music.
- Multi-user, multi-replica, non-SQLite storage.
- Direct tracker (Gazelle) integration in v1.

## 3. Architecture decisions

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| A1 | Grab mechanism | Hermes fetches `.torrent` bytes from the Prowlarr-proxied `downloadUrl`, computes infohash, adds to Deluge via JSON-RPC with `download_location` and label `hermes`. Prowlarr has no download client configured. | Prowlarr grab returns no hash; private trackers expose no infoHash. Exact correlation, per-torrent location, and Prowlarr token logic is preserved because the proxied URL carries it. |
| A2 | beets runtime | beets stays a **separate, standalone deployment**. Hermes talks to it only over HTTP through one custom plugin, `beets-hermes`, whose `hermes-agent` command serves both library reads and import jobs from inside the beets pod. Hermes mounts no music, download, or beets volumes. The beets pod runs the agent as its only process; the built-in `web` plugin is not used (decided 2026-09-07). | Keeps beets usable by hand, keeps one pod owning `library.db`, and gives Hermes one purpose-built contract on one port instead of beets' browser-oriented JSON. |
| A3 | beets library queries | `beets-hermes` read endpoints backed by beets' public `Library` query API: `GET /library/release-group/<mbid>`, `GET /library/release/<mbid>`, `GET /library/search?artist=&album=`, and `GET /library/acquisition/<id>` (albums carrying the `hermes_acquisition` flexible field). Responses are a small typed shape: album MBIDs, artist, title, year, path, and a per-album quality summary (formats, min bit depth, min sample rate, lossy item count). | The `web` plugin's JSON is an internal shape (all fields, base64 paths, path-segment queries) and a second unauthenticated process with delete/patch a flag away. One plugin, one port, a response shape Hermes owns. Decided 2026-09-06. |
| A3b | beets import interface | Same plugin: `POST /import` queues a job, runs `beet import -q -I --search-id <mbid> --set hermes_acquisition=<id> -l <log> <path>` as a subprocess, one at a time; `GET /jobs/<id>` returns status, exit code and log tail. The same CLI path the user runs by hand; no use of beets' internal importer API. | No official import endpoint exists. Subprocess keeps behaviour identical to manual imports and survives beets upgrades. |
| A12 | Development environment | Docker Compose runs beets (the `beets-hermes` image with a library seeded by `scripts/seed-dev-library.py`: Creative Commons albums with real MBIDs, no audio files), Deluge (one instance), **Prowlarr with PandaCD** (a public tracker for Creative Commons and artist-permitted music, native Prowlarr definition, Torznab artist/album search), and Hermes from source. Production Prowlarr is used only to capture private-tracker title fixtures and in the M6 live weeks. | Submit and observe code will run hundreds of times while being written; a bug against production Deluge costs ratio, a bug against compose costs nothing. PandaCD lets the whole acquisition path (search → grab → download → import) run on legal, downloadable, MusicBrainz-covered albums with no production credentials, so anyone can recreate the environment. Decided 2026-09-06; PandaCD added 2026-09-07. |
| A4 | Resolution source | One MusicBrainz `recording/<mbid>?inc=releases+release-groups+artist-credits` lookup per new recording; a recording seen before reuses its earlier resolution, and the chosen group is fetched once in full for the library check. The ListenBrainz metadata endpoint was planned as a first pass and dropped. | The recording lookup already lists every release group with credits and dates, which is the whole decision; at 1 req/s a 50-track playlist takes about a minute, which the daily job absorbs. Revised 2026-09-07. |
| A5 | Prowlarr search | `GET /api/v1/search?query=<artist> <album>&type=music&categories=3000` (JSON) across all enabled indexers; matching and ranking do the precision work. The per-indexer Torznab endpoint (`t=music&artist=&album=`) stays an option if free text proves too noisy on private-tracker. | One typed code path that also returns indexer flags (freeleech) and proxied download URLs. Revised 2026-09-07 from "Torznab first". |
| A6 | Scheduling | In-process asyncio scheduler (APScheduler or a small loop) inside the FastAPI lifespan; jobs also exposed as `POST /api/jobs/...`. No CronJobs in v1. | The download observer already requires a resident loop; fewer manifests. |
| A7 | Persistence | SQLite + SQLAlchemy 2.0 + Alembic. WAL mode. | Single replica; SQLModel dropped because it lags upstream. |
| A8 | Approval | `approval.timid` (default on): everything waits for a human. Timid off: manual requests approve themselves, automated ones need an auto-approve rule. No Hermes budget; Prowlarr's per-indexer Grab Limit bounds blast radius, and a per-tracker ratio/token guard is the v1.5 economic control. | A budget cannot see tokens, freeleech application or ratio, so with token-only downloading it measures the wrong thing. Revised 2026-09-07. |
| A9 | UI | Jinja2 server-rendered pages inside the same FastAPI app: plain forms with POST-redirect-GET, no JavaScript beyond `confirm()` (HTMX was planned, not needed so far). | Approval needs a screen; no SPA build pipeline. |
| A10 | Config | YAML file (policies) + environment variables (secrets, URLs) via pydantic-settings. | Policies are documents; secrets are K8s Secrets. |
| A11 | Python | 3.13 in the container unless beets and pydantic-core publish 3.14 wheels; local dev may use 3.14. | Reduce wheel risk. |

## 4. Domain model

```text
Signal
  id, kind (listenbrainz | manual), recording_mbid?, source_playlist_mbid?, source_playlist_name?,
  position?, seen_at, album_target_id?, resolution_status, resolution_note

AlbumTarget
  id, release_group_mbid (unique), artist_name, artist_mbid, title, primary_type, secondary_types,
  first_release_year, release_mbids, preferred_release_mbid?, library_status
  (unknown|missing|owned|owned_lossy), library_checked_at, track_lengths (one release's track
  lengths in ms, for the sample-rate estimate), genres (MusicBrainz's release-group genres,
  top five with vote counts; NULL until looked up)

Acquisition
  id, album_target_id? (null until resolved), signal_id, state, origin (auto|manual), created_at,
  updated_at, approved_by?, approved_at?, active_grab_id?, search_retries, error?
  (partial unique index: one non-terminal acquisition per target)

Candidate
  id, acquisition_id, prowlarr_guid, indexer_id, indexer_name, title, size_bytes, seeders,
  leechers, freeleech, parsed_quality (json), match_score, rank, rejected_reason?

GrabAttempt
  id, acquisition_id, candidate_id, deluge_instance, infohash, torrent_name, download_location,
  completed_location, added_at, completed_at?, completed_path?, outcome
  (active|completed|stalled|failed|removed), import_job_id?, import_started_at?,
  download_shape (audio files, files per directory, log/cue present; read once at submit)

Event
  id, acquisition_id?, signal_id?, at, level, message, data (json)

Playlist
  id, mbid (unique), title, source (troi patch name or "extra"), created_for, mode,
  status (ingested|ignored|partial|failed), seen_at, ingested_at, track_count, summary, note
```

### Acquisition state machine

```text
DISCOVERED ─┐
            ├─→ RESOLVED ─→ ALREADY_OWNED (terminal)
MANUAL ─────┘      │
                   ├─→ NEEDS_REVIEW (resolution ambiguous; human picks target or rejects)
                   ↓
               SEARCHING ─→ NO_MATCH (re-searched daily once search.retry_days have passed,
                                     up to search.max_retries searches, then REJECTED)
                   ↓
               CANDIDATES_READY
                   ↓
               AWAITING_APPROVAL ─→ REJECTED (terminal)
                   ↓ (approve / auto-approve)
               SUBMITTED  (torrent added to Deluge)
                   ↓
               DOWNLOADING ─→ STALLED ─→ (next candidate → SUBMITTED) | FAILED
                   ↓
               READY_FOR_BEETS
                   ↓
               IMPORTING ─→ IMPORT_NEEDS_REVIEW (beets skipped; human runs it or retries)
                   ↓
               IMPORTED (terminal; Navidrome scan requested once the import queue drains)

Any state → FAILED with error; FAILED is retryable from the UI when the cause was transient (MusicBrainz or a tracker down: "Retry request", "Search again", "Retry import"); a "no such album" verdict offers the MBID field instead.
Any pre-SUBMITTED state → CANCELLED from the UI.
```

Transitions are implemented in one place (`domain/state.py`) and every transition writes
an Event. Nothing else mutates `Acquisition.state`.

### Deduplication rules

- One `AlbumTarget` per release group. A new Signal for a known target attaches to it.
- One non-terminal `Acquisition` per target. A manual request for a target already in flight attaches to the existing acquisition, and the UI says so ("Already requested as #N"). If that acquisition is `RESOLVED` it continues to the search; if `CANDIDATES_READY` the approval gate runs again; if `AWAITING_APPROVAL` it keeps waiting, because a repeated form submit is not the human approval timid mode promises. A reviewed request resolved onto an album already in flight is closed as `CANCELLED` ("superseded by acquisition N") and the page links to N.
- Terminal `ALREADY_OWNED`, `IMPORTED`, `REJECTED` targets are not re-acquired; `CANCELLED` ones are (it meant "not now"). `NO_MATCH` is re-searched by the daily job after `search.retry_days`, up to `search.max_retries` searches (`Acquisition.search_retries` counts every search, manual clicks included), then `REJECTED`.

## 5. Pipeline stages

### 5.1 Discovery (ListenBrainz)

- `GET /1/user/{user}/playlists/createdfor` → list of generated playlists; `GET /1/playlist/{mbid}` → JSPF tracks. Track `identifier` is a recording MBID URL.
- Per-playlist config: `acquire | ignore`. An `acquire` playlist records every track as a Signal with its position; an `ignore` playlist is recorded as a `Playlist` row only, its tracks are not fetched (Daily Jams would otherwise add fifty rows a day for nothing). A patch name the policy does not list is ignored.
- `listenbrainz.extra_playlists`: arbitrary ListenBrainz playlist MBIDs to ingest as if they were generated ones (curated lists, and in dev a hand-built playlist of CC recordings that exist on PandaCD). Each entry carries its own `acquire | ignore` mode.
- Idempotent per playlist MBID (`Playlist` table): an ingested or ignored playlist is skipped unless its mode changed; an interrupted ingest (`partial`) resumes at the first unresolved track. The same recording seen again reuses the earlier resolution and joins the acquisition in flight ("discovered again in ..."); an album already `ALREADY_OWNED`, `IMPORTED` or `REJECTED` is not re-acquired (`CANCELLED` is, it meant "not now").
- Schedule: `listenbrainz.poll_hours` (24) on the in-process scheduler, with one run at startup as the reconcile; `hermes discover` and `POST /api/jobs/discover` run one tick. Public playlists need no token; `LISTENBRAINZ_TOKEN` only adds private ones.

### 5.2 Resolution (recording → release group)

Policy, applied in order, with the reason recorded on the Signal:

1. One MusicBrainz recording lookup (`inc=releases+release-groups+artist-credits`) lists every release group the recording is on; the ListenBrainz release hint is not needed (implemented 2026-09-07, folding the planned two steps into one call).
2. Among the groups the resolution policy accepts (`allow_ep`, `allow_single`, `allow_secondary_types`) and whose artist credit is the recording's (a various-artists compilation or DJ mix is not the artist's album), take Album before EP before Single, earliest first release first. The chosen group is then fetched in full (all releases) for the library check unless the target already exists.
3. Otherwise the Signal is `ignored` with every group's rejection reason in its note and no acquisition is created. Automated signals never go to `NEEDS_REVIEW`: a human cannot fix "only a remix EP and a compilation carry this track", and fifty such rows a week would bury the queue. `not_found` (no release at all, or an unknown MBID) is `failed` on the Signal.

Manual requests: artist + title → MB `release-group` search with `type:album`; top result above a score threshold, else `NEEDS_REVIEW` showing the top five for the user to pick. MBID input skips search. Release MBID input pins `preferred_release_mbid`.

MusicBrainz client: global 1 req/s token bucket, mandatory User-Agent `hermes/<ver> (<contact>)`, follow MBID redirects, cache release-group lookups in SQLite.

### 5.3 Library check (beets-hermes read endpoints)

- `GET /library/release-group/<rg>`; fallback `GET /library/release/<mbid>` for each release MBID in the group; fallback `GET /library/search?artist=&album=` filtered to year ±1 (flag as fuzzy in the event).
- Each returned album carries a quality summary computed by the plugin from its items: set of formats, minimum bit depth, minimum sample rate, and how many items are lossy. `owned_lossy` if any item format is not FLAC/ALAC/WAV; otherwise `owned`.
- The agent is unauthenticated and stays ClusterIP-only. Reads that land mid-import may see a transient SQLite lock; the client retries briefly when the agent reports `worker_busy`.
- v1 outcome: `owned` → `ALREADY_OWNED`; `owned_lossy` → `ALREADY_OWNED` with a flag (v1.5 turns this into an upgrade path); `missing` → continue.

### 5.4 Search (Prowlarr)

- One JSON search across every enabled indexer: `GET /api/v1/search?query=<artist> <album>&type=music&categories=3000` (A5). The query is `text.search_form(artist + title)`: every punctuation mark becomes a space and the noise words a release matcher ignores (a, an, the, and, or, of; Lidarr's list) are dropped, accents kept, so *Deserter’s Songs* is queried as `Mercury Rev Deserter s Songs` and *Belle and Sebastian* as `Belle Sebastian`. A Sphinx index (Gazelle) requires every query word and splits a title at punctuation, so the pieces are the only safe tokens (measured 2026-09-19, see the decision log). An empty answer is checked against Prowlarr's indexer status first: a searched indexer Prowlarr has disabled after failures makes the search `FAILED` (retryable) naming it and the time it returns, because Prowlarr answers an empty list for a skipped indexer. Otherwise, when the title carries a subtitle, soundtrack label or parenthesised alternative (`matching.title_head`, the split `title_similarity` judges by), one search with the artist and the title's head (`CAN Anthology` for *Anthology: 25 Years*, which the tracker lists as `Can - Anthology`; measured 2026-09-24), then one with the artist alone (limit 500, not for Various Artists), each recorded as an event, and matching picks the album: the title is what uploaders spell differently, the artist rarely. The artist-only search is one page of groups on a Gazelle indexer whatever the limit (163 rows for `CAN`, newest first, none by Can), so it finds a small catalogue whole and a large one's newest uploads; the head query is what finds an older album with a subtitle. Matching and ranking do the precision work. `search.indexers` (Prowlarr indexer names) restricts the query with `indexerIds`, for a Prowlarr shared with other apps whose audio-capable indexers are not worth Hermes's queries; a name Prowlarr lacks or has disabled is a warning event, and a list that resolves to nothing fails the search (retryable) rather than widening it.
- Indexer rate limits are Prowlarr's (per-indexer query and grab limits); discovery is paced by the MusicBrainz limiter anyway, and the daily re-search is bounded by `search.max_retries`.
- Before ranking, the target's playing time (one release's track lengths from MusicBrainz, fetched once and kept on the target) is looked up so sample rates can be estimated from size.
- Persist every row as a Candidate, including rejected ones with the reason; a re-search replaces the candidates unless a grab attempt references them, which are superseded instead.

### 5.5 Parse and rank

Title parser (pure function, heavily unit-tested against captured titles):

```text
Artist - Title (Year) [Release Type] [Remaster Title Year] [Format Encoding / Media / Log (100%) / Cue]
```

Normalised quality model (`ParsedTitle`): `format`, `encoding` (lossless | lossless24 | lossy), `encoding_detail`, `sample_rate_khz` (only when the title states it), `media` (WEB | CD | Vinyl | SACD | DVD | Blu-Ray | Cassette | ...), `log_score?`, `cue`, `release_type`, `remaster_year?`, `remaster_title?`, `edition_flags` (deluxe, anniversary, japan, mono, ...). The corpus is 804 captured tracker A titles plus the PandaCD family.

Hard filters (reject with reason): not FLAC; media in `excluded_media` (Vinyl by default); seeders below `min_seeders`; release type not in `allowed_release_types` or not agreeing with the target's MusicBrainz type; size outside the per-encoding caps; match score below `match_threshold`; a 24-bit release whose estimated sample rate (size over playing time, or the title's own figure) exceeds `max_sample_rate_khz`, with ten percent allowed for artwork and editions estimated from the tracker's file count.

Match score (target ↔ title): artist similarity, trying the credit before a "performed by" or "feat." tail; title similarity (rapidfuzz) after stripping an edition suffix, with subtitles handled asymmetrically (MusicBrainz's soundtrack tail or a parenthesised alternative title may be dropped, or the listing may keep only the subtitle; a subtitle only the tracker has is a different release; when both sides carry one the tails must agree), and numbers compared exactly wherever titles are compared, Roman numerals and number words read as digits ("Album II" is not "Album", "Volume II" is "Volume 2"). The score is the title similarity bounded above and the mean of artist and title below: `min(title, (artist + title) / 2)`, so the artist can only lower it. Year within ±1 unless a remaster year explains it.

Rank (weights in `ranking.py`): quality decides the order -- match, media preference (WEB > CD by default), encoding preference (24-bit first by default, safe because of the sample-rate limit), edition penalties (deluxe-class 1.0, remaster 0.1, other reissue labels 0.8, so the plain album wins ties), log 100% + cue for CD, indexer preference -- and seeders (capped), freeleech and smaller size only break ties between releases of equal quality, being together worth less than half the smallest quality difference (see docs/conventions.md). Top N (`keep_candidates`, default 3) kept as ordered fallbacks. `tests/integration/test_ranking_scenarios.py` pins the winner and rejection reasons for twenty captured searches.

### 5.6 Approval

- `approval.timid: true` (default, after beets' timid mode): every acquisition waits in `AWAITING_APPROVAL` for a human, whatever its origin.
- Timid off: origin `manual` is approved; origin `auto` waits unless an auto-approve rule matches the top candidate (e.g. `freeleech and size < 1 GiB`).
- `dry_run: true` stops here for everything, logging what would have been grabbed.
- There is no Hermes budget. Blast radius is Prowlarr's per-indexer Grab Limit; ratio and token supply are the tracker's own numbers and arrive as a per-tracker account guard in v1.5. A torrent fetch that fails (tokens exhausted on a "Required" indexer, tracker down) defers that indexer for the run and leaves the candidates ranked for a later retry.

### 5.7 Submit (Deluge)

The cluster runs **one Deluge instance per tracker** (`deluge-0` ops, `deluge-1` red,
`deluge-2` other, each with its own Service). `policy.deluge.instances` maps Prowlarr
indexer names to an instance; a candidate whose indexer maps to nothing is rejected at
submit time unless `default_instance` is set. The chosen instance is stored on the
GrabAttempt so the observer polls the right daemon.

1. `GET` the candidate's Prowlarr `downloadUrl` (proxied; carries `apikey`; Prowlarr applies indexer token settings). Reject magnet-only results.
2. Decode bencode, compute SHA-1 of the `info` dict → infohash, and read the files' shape (audio count, files per directory, log/cue present) for the importer's release choice. The `.torrent` bytes are not persisted: a private tracker's torrent carries the account passkey in its announce URL, Deluge keeps the file for seeding, and Hermes tracks the download by infohash. A submit interrupted after the Deluge add is recovered on the next submit by finding the torrent at the acquisition's own download path (decision 2026-09-07).
3. Deluge Web JSON-RPC on the routed instance: `auth.login`, `web.connected` / `web.connect` if needed, then `core.add_torrent_file(filename, base64, {download_location: <pending_root>/<acq-id>, move_completed: true, move_completed_path: <completed_root>/<acq-id>})`, then `label.set_torrent(hash, "hermes")`. This follows the cluster's pending → complete convention explicitly per torrent, so the global Deluge setting does not matter.
4. If Deluge reports the hash already exists, adopt it.
5. Between steps 2 and 3, the torrent's per-file sizes against the target's track lengths give the median per-track bitrate; a 24-bit release over `max_sample_rate_khz` is skipped for the next candidate. A candidate whose indexer routes to no Deluge instance, or that has no torrent file, is skipped; a torrent fetch failure defers the whole indexer for the run.

### 5.8 Observe

- Loop every 30–60 s, per Deluge instance that has active attempts: `core.get_torrents_status({"id": [active hashes]}, [progress, state, is_finished, save_path, name, total_done, download_payload_rate, move_completed_path])`.
- Complete when `is_finished`, state is `Seeding`, and `save_path` equals the attempt's `move_completed_path` (the move has happened). Completed path = `save_path/name`; if `name` is a file (single-file torrent) pass the file path to beets.
- Stalled when no `total_done` change for `stall_hours` and no seeders → attempt `stalled`; promote next candidate → `SUBMITTED`; none left → `FAILED`.
- Missing hash (user removed it in Deluge) → attempt `removed` → `FAILED` with event.
- On startup: reconcile every `SUBMITTED`/`DOWNLOADING` acquisition; every `IMPORTING` one re-verifies against beets.

### 5.9 Import (beets, via `beets-hermes` agent)

- Path mapping: Deluge's completed path is rewritten from the Deluge mount prefix to the beets mount prefix (`paths.deluge_root` → `paths.beets_root`). In the cluster Deluge mounts `nas:/mnt/hunger/downloads` at `/downloads` while beets mounts `nas:/mnt/hunger/downloads/complete` at `/downloads`, so `/downloads/complete/hermes/12` becomes `/downloads/hermes/12`.
- The cluster's beets config has `incremental: yes`; the agent passes `-I` so a retried path is not silently skipped, and once Hermes has verified the import it calls `POST /history` on the agent, which adds the folder's album groups (as beets' own `albums_in_dir` computes them) to the state file's incremental history, so the user's manual `beet import` over the downloads directory skips it instead of stopping at a duplicate prompt (decision 2026-09-07). Reviewed and failed imports are not recorded. Under quiet mode `duplicate_action: ask` becomes a skip (the row goes to review) and `resume: ask` would fail wanting input, so the production config draft sets `resume: no`; v1 imports only missing albums so duplicates should not arise, and v1.5 (upgrades) must pass an explicit `duplicate_action`.
- `POST /import` to the agent with `{path, search_id?, release_group_id?, acquisition_id}`. The agent answers a job id immediately. Before running beets it reads the files' `mb_releasegroupid` tags: when every tagged file names the same group and it is not `release_group_id` (the target's), the job finishes `refused` with the reason and beets never runs (0.11.0). `search_id` is `preferred_release_mbid` if set, else the release in the group that fits the downloaded files (`importer.pick_release`: official, the download's track count, its disc layout, its medium when a log or cue sheet says CD or the title names it, the year, a worldwide/major country, a plain release over a disambiguated variant), else omitted.
- The agent runs, one job at a time:

```text
beet import -q -I --search-id <release_mbid> --set hermes_acquisition=<id> -l <log> -- <path>
```

  It refuses to start if the loaded beets config has `import.copy` false, `import.move` true, or `link`/`hardlink` on, and reports `config_rejected` so Hermes goes `FAILED` with a config event.
- Hermes polls `GET /jobs/<id>` every 30 s until `finished`; timeout default 30 min (fetchart/lyrics plugins make imports slow).
- Verify independently of the exit code: `GET /library/acquisition/<id>` on the agent, and re-run the release-group check. Found → `IMPORTED`; not found → `IMPORT_NEEDS_REVIEW` with the log tail from the job.
- Post-import: Navidrome Subsonic `startScan` (optional, config), once per drained queue rather than per album, and no import starts while `getScanStatus` says a scan is running (decision 2026-09-22).

`beets-hermes` plugin scope (kept deliberately small, one file):
- `beet hermes-agent [--host] [--port]` (also the `hermes-agent` console script): stdlib `ThreadingHTTPServer`; endpoints `GET /healthz` (beets version, `agent_api` and plugin version, config precondition check, worker state), the `/library/...` reads from A3, `POST /import`, `GET /jobs/<id>`, `GET /jobs`, `POST /history`.
- Reads use the `Library` object beets hands the subcommand; queries are built with beets' query classes, never string-concatenated, so artist names with slashes or colons are safe.
- Single worker thread; job records kept in memory plus a JSON file next to the beets config so a restart can report the last outcome.
- Optional: listens to `album_imported` and POSTs `{acquisition_id, album_id, mb_releasegroupid}` to `HERMES_CALLBACK_URL`. Polling makes this unnecessary in v1.
- Packaged in this repo under `beets-hermes/` (its own `pyproject.toml`, module `beetsplug/hermes.py`), installed into the beets image by `deploy/beets/Dockerfile`, enabled with `hermes` in `plugins:` (the `web` plugin is not needed). Run with `BEETSDIR` set so the agent and its `beet import` subprocesses read the same config. Versioned with Hermes; `agent_api` in `/healthz` is the compatibility contract.

## 6. Configuration (YAML) — shape

`config.example.yaml` is the canonical document: every key with its default and a comment,
and a test keeps it equal to `Policy()`. The shape, with the cluster-specific parts filled
in as an example:

```yaml
dry_run: true                      # HERMES_DRY_RUN in the environment overrides it
listenbrainz:
  user: ...
  playlists: {weekly-exploration: acquire, weekly-jams: ignore, daily-jams: ignore}
  extra_playlists: {}              # playlist MBID -> acquire | ignore
  poll_hours: 24
resolution:                        # allow_ep, allow_single, allow_secondary_types, min_similarity, search_limit
search:                            # max_retries, retry_days (the daily re-search of NO_MATCH)
quality:                           # require_format, excluded_media, media_preference,
                                   # encoding_preference (24-bit first), indexer_preference, size caps,
                                   # min_seeders, allowed_release_types, match_threshold, keep_candidates,
                                   # max_sample_rate_khz (96), edition_penalties
approval:
  timid: true                      # every acquisition waits for a human
  auto_approve: []                 # with timid off: rules such as {freeleech: true, max_bytes: 1000000000}
deluge:
  instances:
    a: {url: http://deluge-a-service.deluge.svc.cluster.local:8112, indexers: ["Tracker A"]}
    b: {url: http://deluge-b-service.deluge.svc.cluster.local:8112, indexers: ["Tracker B"]}
  default_instance: null
  pending_root: /downloads/pending/hermes
  completed_root: /downloads/complete/hermes
  label: hermes
  poll_seconds: 45
  stall_hours: 48
beets:
  agent_url: http://beets-web.beets.svc.cluster.local:8338
  import_timeout_seconds: 1800
  library_root: /music
paths:
  deluge_root: /downloads/complete   # as Deluge reports it
  beets_root: /downloads             # as the beets pod mounts it
navidrome:
  trigger_scan: true
```

Environment (`.env`, gitignored, secrets included): `HERMES_CONFIG_PATH`,
`HERMES_DATA_DIR`, `HERMES_PORT`, `HERMES_DRY_RUN`, `PROWLARR_URL`, `PROWLARR_API_KEY`,
`DELUGE_PASSWORD` (one Web UI password shared by all instances), `NAVIDROME_URL`,
`NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `MUSICBRAINZ_CONTACT`, and the optional
`LISTENBRAINZ_TOKEN` (public playlists need none).

## 7. Application structure

```text
hermes/
├── app.py                 FastAPI factory, lifespan (clients, scheduler jobs, torrent-file cleanup)
├── cli.py                 serve, check, request, approve, observe, import-tick, discover,
│                          ingest-playlist, research, parse-title, validate-config, db upgrade|revision
├── config.py              Policy (YAML, strict) and Settings (environment)
├── bencode.py             decoder/encoder, TorrentInfo (infohash, paths, sizes, audio layout)
├── db/                    engine (SQLite WAL), sessions, Alembic migrations/
├── domain/
│   ├── models.py          SQLAlchemy models (section 4)
│   └── state.py           state machine + transition() that writes Events
├── integrations/          thin async httpx clients, one file each, no business logic
│   ├── listenbrainz.py    generated playlists and JSPF tracks (public reads)
│   ├── musicbrainz.py     search, release group, release, recording, track lengths; one limiter
│   ├── prowlarr.py        JSON search, torrent fetch through the proxy
│   ├── deluge.py          JSON-RPC session, add, label, status, find by path
│   ├── beets.py           library reads, import jobs, history marking via the agent
│   └── navidrome.py       Subsonic startScan
├── services/              one stage each, pure where possible
│   ├── text.py            normalise + similarity (rapidfuzz)
│   ├── title_parser.py    tracker title -> ParsedTitle (pure)
│   ├── matching.py        does a parsed title describe the target (pure)
│   ├── ranking.py         filters with reasons, weighted rank, sample-rate estimate (pure)
│   ├── resolution.py      request or recording -> release group
│   ├── library_check.py   release group -> release -> fuzzy, via the agent
│   ├── requests.py        manual requests, dedupe, review picks, retries
│   ├── discovery.py       playlists -> signals -> acquisitions; the daily re-search
│   ├── genres.py          release-group genres on the target; the page rule; the backfill job
│   ├── search.py          SEARCHING -> CANDIDATES_READY | NO_MATCH
│   ├── approval.py        timid mode and auto-approve rules
│   ├── submit.py          fetch, adopt or add to the routed Deluge, GrabAttempt
│   ├── observer.py        poll Deluge, completion, stall fallback
│   ├── importer.py        release choice from the files' shape, agent job, verification
│   ├── pipeline.py        search then decide
│   └── context.py         policy + clients, what every stage receives
├── api/                   JSON routes (requests, acquisitions and their actions, playlists, jobs)
└── ui/                    Jinja2 templates + form routes (queue, acquisition, error)
beets-hermes/              separate uv project, installed in the beets image
├── pyproject.toml
├── beetsplug/hermes.py    plugin: `beet hermes-agent` (health, library reads, import jobs, history)
└── tests/
tests/
├── fixtures/              captured payloads: MusicBrainz, ListenBrainz, Prowlarr (PandaCD, tracker A), titles
├── fakes.py               FakeDeluge, FakeBeetsAgent
├── unit/                  parser, matching, ranking, resolution, state machine, config, bencode
└── integration/           respx-mocked clients against a temp SQLite database; review regressions
deploy/                    Kubernetes manifests, the beets image, the production beets config draft
.github/workflows/         images.yml: test, then build and push both images on a push to main
```

Stack: Python 3.13, FastAPI, uvicorn, httpx, pydantic v2, pydantic-settings, SQLAlchemy 2,
Alembic, APScheduler, Jinja2, rapidfuzz, an own bencode module, pytest, pytest-asyncio,
respx, ruff, mypy.

## 8. Kubernetes topology

```text
Deployment/hermes            replicas: 1, strategy: Recreate (deploy/hermes.yaml)
  container hermes           image <namespace>/hermes:<version>-<sha>; no beets, no shared media volumes
  volumes:
    hermes-data (RWO PVC)    /data     SQLite only (no torrent files are kept)
Service/hermes               ClusterIP :8000
Secret/hermes                env vars from section 6
(optional) Ingress with basic auth for the UI

StatefulSet/beets (existing, namespace beets; flux repo clusters/home/beets)
  image                      <namespace>/beets-hermes:<beets>-<sha>: the pinned
                             lscr.io/linuxserver/beets tag+digest plus the plugin (deploy/beets/Dockerfile);
                             tagged with the beets release inside it
  container beets            runs `hermes-agent` :8338 as the pod's only process (the image's
                             s6 init and web UI are bypassed; deploy/beets/statefulset-patch.yaml),
                             BEETSDIR=/config, uid 1000, readiness on /healthz.
                             Image contract: `beet` works with BEETSDIR and `hermes-agent` is on
                             PATH; the base image is otherwise free to change.
  volumes                    unchanged: /config (Longhorn RWO, library, state file, agent jobs),
                             /music (nfs nas:/mnt/hunger/music),
                             /downloads (nfs nas:/mnt/hunger/downloads/complete)
  ad-hoc use                 `kubectl -n beets exec -it beets-0 -- beet import /downloads`, as today
Service/beets-web            NEW, ClusterIP :8338 agent only (deploy/beets/service.yaml; the name
                             matches the StatefulSet's serviceName)

StatefulSet/deluge (existing, namespace deluge; 3 instances, unchanged)
  deluge-0 (tracker A)   → Service deluge-a-service   :8112 web, :58846 daemon
  deluge-1 (tracker B)   → Service deluge-b-service
  deluge-2 (other) → Service deluge-c-service
  version 2.2.0, Label plugin enabled, move_completed /downloads/pending → /downloads/complete,
  /downloads = nfs nas:/mnt/hunger/downloads, one shared Web UI password
```

The agent and any interactive `beet` share one container, so one process tree holds the
SQLite file and locking behaves as it does on a workstation. Prowlarr, Deluge, Navidrome and
the trackers stay as they are. Prowlarr needs **no** download client entry for Hermes.
The flux repo is the source of truth for the cluster and is read-only for this project;
`deploy/` holds the changes to port into it, and the images come from the GitHub Actions
workflow (tags `latest`, `<version>`, `<version>-<sha>`; the cluster pins the last).

## 9. Operations

- `/healthz` (and `hermes check`) reports the database, Prowlarr, every Deluge instance, the beets agent (config preconditions and the agent API version), MusicBrainz and ListenBrainz (configuration only, no network), and Navidrome; 503 when any configured dependency is unhealthy.
- Every state transition is an Event row shown on the acquisition page; the queue page shows the last playlist ingested.
- Startup runs every scheduled job once (observer, importer, discovery, re-search); each is idempotent, so that is the reconcile. `hermes observe`, `import-tick`, `discover`, `research` and the `POST /api/jobs/...` routes run one tick by hand.
- Backups: the RWO PVC holds only SQLite (no torrent files); a nightly copy is enough.

## 10. Testing strategy

- **Title parser and ranker**: table-driven tests from `tests/fixtures/titles/` (804 captured tracker A titles, the captured PandaCD family, and a hand-written Gazelle set) and twenty captured tracker A search responses with pinned winners and rejection reasons (`test_ranking_scenarios.py`). Add a case whenever a real result set ranks wrongly.
- **Matching corpus**: `tests/fixtures/matching/pairs.yaml`, a target against a listing with the verdict and the query sequence, real rows from approved and imported acquisitions plus one invented row per mechanism (subtitles either side, volumes and parts, soundtrack labels, native-script alternatives, ampersands, apostrophes, accents, articles, featured credits, years and remasters). A verdict the matcher does not reach yet is kept as a `gap:` row, a strict expected failure. A miss becomes a row before it becomes a fix.
- **Resolution policy**: fixtures of MB recording responses with compilations, live albums, singles, VA soundtracks.
- **State machine**: every allowed and forbidden transition.
- **Integrations**: respx-mocked HTTP for Prowlarr, ListenBrainz, MusicBrainz and the beets agent; Deluge JSON-RPC session/auth flow.
- **beets-hermes plugin**: run the agent against a real beets install with a temp config and library. Job lifecycle with a stub `beet` on PATH; read endpoints against a temp library seeded through beets' own `Library.add()` with albums in known release groups and formats; one real `beet import` of a tiny FLAC fixture to prove `--set hermes_acquisition` is queryable through `/library/acquisition/<id>`.

### Development environment (A12)

`docker-compose.yaml` at the repo root:

- **beets**: the `beets-hermes` image, `/config` bind-mounted to `dev/beets/` holding a dev `config.yaml` derived from the production draft `deploy/beets/config.yaml` with one difference: the slow on-import plugins are commented out (swap the two `plugins:` lines for a rollout rehearsal). The library starts empty; `docker compose exec beets beets-python /scripts/seed-dev-library.py` adds Creative Commons albums that also exist on PandaCD, with real MusicBrainz IDs, through beets' Library API: Josh Woodward *Dirty Wings* (owned FLAC), Chris Zabriskie *Stunt Island* (owned MP3), Chris Zabriskie *Vendaface* (owned 24-bit FLAC); Nine Inch Nails *The Slip* and Josh Woodward *Addressed to the Stars* are left missing so search and import have live targets. No audio files exist; paths point at `/music/...`. No cluster access or personal data is needed to recreate the environment.
- **deluge**: one `linuxserver/deluge` instance seeded from `dev/deluge/core.conf.template` (Label plugin, `pending/` → `complete/` moves), `/downloads` shared with beets at the same relative layout as production. `tests/fixtures/torrents/gettysburg-audio.torrent` (public-domain LibriVox audio, archive.org web seeds, built by `scripts/make-fixture-torrent.py`) exercised add, progress, completion and move-on-complete without peers before PandaCD grabs took that over; it remains the suite's real multi-file torrent.
- **prowlarr**: `linuxserver/prowlarr` pinned to the cluster's tag, `config.xml` seeded from `dev/prowlarr/config.xml.template` (fixed dev API key, external auth so no login prompt). `scripts/dev-prowlarr-setup.py` adds the PandaCD indexer through the API and runs a smoke search. PandaCD facts (verified 2026-09-07): public, no login, Torznab `artist`/`album`/`genre` search, titles like `Artist - Album [2008] [FLAC 24bit Lossless]` (Gazelle-style, but no media segment and no log/cue), through Prowlarr results carry size and categories 3040/3010 (the raw Torznab feed reports only 3000 and no size), one or two seeds per torrent, downloads burst then throttle (a 270 MB FLAC paused at 38% for several minutes and then completed). Keep dev search volume modest; it is a small community tracker.
- **hermes**: built from source with reload, `config.yaml` from `dev/hermes/`, SQLite under `dev/data/`. `dry_run: true`; the compose Deluge is the only grab target.
- Not in compose: MusicBrainz and ListenBrainz (public APIs, rate-limited, hit directly), Navidrome (covered by tests only until the cluster rollout).

What the dev stack proves and what it does not: every stage runs for real, but a recommendation from the user's real Weekly Exploration will almost never exist on PandaCD, so in dev the automated path mostly ends at `NO_MATCH` (a real path, exercised constantly). Manual requests for CC albums run the acquisition path end to end. A hand-built ListenBrainz playlist of CC recordings, ingested via `listenbrainz.extra_playlists`, runs the automated entry point end to end. Recommendation quality against private-tracker is judged only in the M6 live weeks; a green dev run is not proof of that.

Everything the compose stack needs is gitignored under `dev/` except the templates and the dev configs.
- **End-to-end (manual)**: dry-run against real services in the cluster for at least two weekly cycles before `dry_run: false`.

## 11. Milestones

Each milestone ends with something runnable.

| M | Deliverable | Acceptance |
|---|-------------|------------|
| M0 | Skeleton: package layout, config loading, SQLite + Alembic, `/healthz`, Dockerfile, K8s manifests. `beets-hermes` plugin with `/healthz` and `/import`, added to the beets deployment. **Done locally 2026-09-04:** both images build; the Hermes container migrates and serves `/healthz`; the beets image runs `hermes-agent` as uid 1000 with a green health check. Cluster rollout (port `deploy/beets/` into flux, apply `deploy/hermes.yaml`) is the user's step. | Hermes pod runs in cluster; health shows Prowlarr, Deluge and the beets agent green, and the agent reports the import config preconditions. |
| M0b | Follow-ups decided 2026-09-06: move library reads into `beets-hermes` and drop the web-plugin client (A3); add the compose development environment with a seeded synthetic library (A12). **Done 2026-09-06:** `/healthz` green against the compose stack (Deluge 2.2.0 with Label, beets 2.5.1 agent), `/library/...` endpoints answer from the seeded synthetic library, and `dev/fixtures/gettysburg-audio.torrent` downloads from web seeds and moves to `complete/hermes/<id>` in seconds. | `hermes check` against the compose stack is green; `/library/release-group/<mbid>` answers from the seeded library; the fixture torrent completes in compose Deluge. |
| M1 | Manual request → resolution → library check. CLI + JSON API (UI form comes with M3's approval screen). MB client with limiter. **Done 2026-09-06:** 21-album live run against MusicBrainz and the seeded dev library; owned, owned-lossy, missing, EP, dedupe, review (single, soundtrack, short title) and nonsense all behaved as intended. The one miss ("Lift Your..." vs MusicBrainz's "Lift Yr. ...!") led to a loose-search fallback; it and the release-MBID case were then verified live. | `hermes request "Artist" "Album"` yields `ALREADY_OWNED` or `RESOLVED` with correct release group for 20 hand-picked albums. |
| M2 | Prowlarr search, title parser, matching, ranking, candidates in UI. Dry-run only. Starts with Prowlarr + PandaCD in compose and the CC library reseed (A12). **Done 2026-09-07:** missing albums are searched automatically after the library check; every Prowlarr row is persisted as a Candidate with rank or rejection reason; `POST /api/acquisitions/{id}/search` re-runs; `hermes request` prints the list. Live: The Slip and Addressed to the Stars each rank the FLAC first and reject the lossy encodes with reasons. Parser corpus: 29 captured PandaCD titles plus 122 hand-written Gazelle-format titles (from the Gazelle definition's format); captured private-tracker titles still to be added once a production API key is available. | Candidate lists for the CC albums come back from PandaCD through Hermes and look right by eye; parser corpus ≥ 100 titles green, including captured private-tracker titles from production Prowlarr and PandaCD titles from dev. |
| M3 | Approval queue, budget, submit to Deluge, observer, stalled fallback. **Done 2026-09-07:** approval gate (timid by default: every grab waits for a human; otherwise manual requests self-approve and automated ones need a rule; the weekly budget was removed the same day, see the decision log; dry-run records the would-be grab), submit (torrent fetched through Prowlarr, infohash computed and cross-checked, per-torrent pending/completed paths, `hermes` label, routing refusal), observer on APScheduler with startup reconcile, stall fallback to the next candidate, vanished/error handling, server-rendered UI. Live: *The Slip* went request → approved by policy → added to the compose Deluge → `DOWNLOADING` → (Deluge `Moving`) → `READY_FOR_BEETS` in 30 s, with the completed path and beets-side path on the event. Also learned: PandaCD seeder counts fluctuate (a FLAC that showed 1 seed later showed 0 and was correctly rejected), and Deluge reports a `Moving` state between finish and the completed path, which the observer waits out. | A manually approved album downloads into `/downloads/hermes/<id>/` and reaches `READY_FOR_BEETS`. |
| M4 | Import via the agent with `--search-id` and `--set hermes_acquisition`, verification through `/library/acquisition/<id>`, `IMPORT_NEEDS_REVIEW`, Navidrome scan. **Done 2026-09-07:** importer stage on the scheduler (also the startup reconcile), release picked from the MusicBrainz release group (official, year match, worldwide/major country) unless pinned, agent preconditions checked before submitting, verification independent of the exit code, review states for quiet skips, timeouts, lost jobs, invisible paths and unsafe configs, retry from the UI/API, Navidrome scan when configured. Live: the M3 download of *The Slip* was imported by the real beets agent in 25 s, ten FLAC tracks at `/music/Nine Inch Nails/The Slip` tagged with the chosen release, target now owned; the seeded copy under `complete/hermes/23` is intact and Deluge still seeds it. Navidrome is not in the compose stack, so the scan trigger is covered by tests only until the cluster rollout. | Album appears in beets and Navidrome; seeded files untouched (verify torrent recheck passes); a hand-run `beet import` in the pod still works. |
| M5 | ListenBrainz ingestion, LB metadata resolution, per-playlist policy, scheduler, reconcile. Also carries the UI work deferred from the reviews. **Built 2026-09-07:** `integrations/listenbrainz.py` (public reads, optional token, no network in `health()`), `services/discovery.py` (`tick`, `ingest_playlist`, `research_tick`), `Playlist` table, recording → release group resolution on one MusicBrainz call, daily discovery and re-search jobs on the scheduler, `hermes discover` / `ingest-playlist` / `research`, `GET /api/playlists`, `POST /api/jobs/{discover,research}`, queue filters (state, needs-you, origin) and Finished paging, submit feedback, `search.max_retries`/`retry_days`. Verified: the client against the real public API (a 46-playlist listing, a 50-track Weekly Exploration), the pipeline on captured fixtures (Juno resolves and searches; a remix EP, a soundtrack album, a single and a VA compilation are rejected with reasons). **Run live 2026-09-07:** the hand-built CC playlist (6 tracks: The Slip not re-acquired, Ghosts and Addressed to the Stars attached to their in-flight rows) and two real Weekly Explorations for the user's account (100 tracks in about 7 minutes at the MusicBrainz rate: 65 new acquisitions, all `NO_MATCH` on PandaCD as expected, 16 attached to an album already seen, 19 `nothing_acquirable` for soundtracks, singles and various-artists compilations, 34 other generated playlists recorded as ignored). Acceptance met in dry run. | A Weekly Exploration run produces targets, owned/missing split, and an approval queue, all in dry-run. |
| M6 | Two live weeks with auto-approve rules on, then tighten defaults from what was learned. Also verifies that Prowlarr's download proxy counts against its Grab Limit and that token-only indexers behave. | Grab Limit respected; no wrong-album grabs; no `IMPORT_NEEDS_REVIEW` without a clear cause. |

M0b precedes M1 so that M1 develops against the compose stack from the start. M1–M2 are
pure-logic heavy; M3–M4 exercise Deluge and beets in compose before anything touches the
cluster.

**Status 2026-09-07 (end of session):** M0–M5 are done and verified live in the compose
stack. Reviews so far: a four-lens code review after M4, two UX reviews, and a second
four-lens code review after M5 (all fixes committed; see the decision log). The day's
follow-on work on real tracker A data is in: captured fixtures and an 804-title corpus,
edition penalties, 24-bit first, the sample-rate limit inferred from size and playing
time, subtitle- and alternative-title-aware matching, no torrent file on disk, and the
recovery paths (resumable ingests, retrying re-search, one active acquisition per target).
1179 tests.

**Dev stack as left:** dry run and timid mode on; the user's ListenBrainz name and the CC
test playlist in the gitignored `dev/hermes/config.yaml` (tracked as `config.example.yaml`); tracker A configured in the dev Prowlarr (query
limit 100, grab limit 2) for search-only work, to be removed and its key rotated when no
longer needed; `MUSICBRAINZ_CONTACT` set to a real address; about eighty `NO_MATCH` rows
from two Weekly Explorations waiting out `search.retry_days`, plus three rows awaiting
approval from today's searches. Lowering `search.max_retries` in the dev policy keeps the
daily re-search from spending private-tracker/PandaCD queries on albums that will never appear.

**Next:** M6, two live weeks against the production trackers with auto-approve rules on,
which needs the cluster rollout (the user's step) or a production key in the compose
stack; a first week in dry run costs nothing and records "would submit X because rule Y".
Smaller items that need no key: property tests for the ranking invariants, an `explain`
view for a queue row (score breakdown, which rule would fire), a version column on
`Acquisition` for optimistic concurrency, and a background `POST /api/jobs/discover`.
Live checks still owed: the Deluge `download_location` filter's exactness on the real
daemon (`/hermes/1` vs `/hermes/10`), and MusicBrainz behaviour with the server and a CLI
tick running at once. The dev database keeps everything (no retention; roughly 5 KB for a
request that stops early and 20–60 KB for one that searches and downloads, so tens of MB a
year at Weekly Exploration volume); pruning terminal acquisitions' candidate rows is a
possible later knob, not v1 work.

## 12. Open questions

Q3, Q5 and Q6 are config defaults and can change later. Q7 decides whether playlist sync
stays out of scope. The rest refine details.

1. **beets deployment:** answered. The pinned linuxserver image ships beets 2.5.1 on Python 3.12; `deploy/beets/Dockerfile` builds on it and the agent's health check passes inside that image as uid 1000 (verified 2026-09-04 with Docker locally).
2. **Deluge:** answered from the flux repo. Remaining: the exact Prowlarr indexer names for the two private trackers (for `deluge.instances[*].indexers`), and whether the third instance (other trackers) should ever receive Hermes torrents.
3. **Approval default:** answered 2026-09-07. `approval.timid` defaults to true (a human approves every grab); auto-approve rules are opt-in. There is no Hermes budget; blast radius is Prowlarr's per-indexer Grab Limit.
4. **Trackers:** which private trackers, and is there one where the token policy should differ (the Prowlarr setting is per indexer, so that is fine)?
5. **Playlists:** defaults set (Weekly Exploration acquires, the Jams and every other generated list are recorded and ignored); whether Weekly Jams should acquire is judged after M6.
6. **EP / single / compilation policy:** answered by defaults: `allow_ep` on, `allow_single` off, no secondary types; an automated track with no acceptable group is ignored with the reason on its signal (decision 2026-09-07).
7. **Navidrome:** version ≥ 0.63 and willing to run the ListenBrainz playlist plugin? Is scanning scheduled or on-demand?
8. **beets config:** answered from the flux repo; the production draft with the Hermes changes and quality additions is `deploy/beets/config.yaml`, rehearsed with every plugin on (about a minute per album).
9. **Upgrade scope:** confirmed, v1 is missing-only; `owned_lossy` is recorded for v1.5.
10. **UI:** answered. A minimal server-rendered UI (queue, detail, request form) is in place and has been through two UX reviews; no external dashboard.
11. **Cross-seeding (future feature, not v1):** the same files seeded on several trackers
    from one folder. Most of what it needs is in Hermes already: the Prowlarr search across
    indexers, the torrent parser's file sizes (`download_shape`), per-tracker Deluge routing,
    and grab attempts as the audit record. A pass would take a finished download, search the
    other configured indexers for the same album, keep only results whose file list matches
    the existing files byte for byte, and add each to its tracker's Deluge instance pointed
    at the existing folder with a recheck instead of a download, renaming the torrent's root
    to the on-disk name where the two trackers differ. It must sit behind the same gates as a
    grab (dry run, timid, events). `deluge.layout: flat` (0.9.0) keeps the download pool the
    shape such a tool, or the user's existing manual cross-seeds, expects.

## 13. Decision log

- **2026-09-24** 0.11.0: the files' own release-group tag guards a Hermes import. 0.10.9 and 0.10.10 stopped the overlay from scoring any of the uploader's edition and id tags, which were only ever penalties against a pinned release; but a correctly MusicBrainz-tagged upload's ids are also the one positive evidence of what the files are, and without them a different recording under the same track list (mono vs stereo, a re-recording in another group, classical with the composer as artist and close timings) could pass on titles and lengths alone. Now Hermes sends the target's release group with the import (`release_group_id`), and the agent reads each audio file's `mb_releasegroupid` before running beets: when every file that has one names the same group and it is not the target's, the job finishes `refused` without running beets and the row goes to review ("beets import refused: ... import them by hand"). Untagged files, files tagged in the target's group (another release of it, as with *Talkie Walkie*), and files whose tags disagree pass as before. A group merged in MusicBrainz after tagging refuses too: that errs toward review. `AGENT_API` 2 (a new request field and a job field an older Hermes would misread); minor version.
- **2026-09-24** 0.10.10: the overlay also zeroes `album_id` and `track_id`. *Talkie Walkie* (Air, acquisition 35), skipped before 0.10.9 and again after it, scored 82.1% interactively with one penalty, `id`: the files carried the MusicBrainz album id of another release of the same ten tracks, not the US CD Hermes pinned. The album id weighs 5 against 28 for everything else still scored, so 5/28 alone kept it off the strong line. Against a given release an id in the tags can only cost (a match adds nothing), and it is the uploader's edition like the fields 0.10.9 dropped. A second overlay test rebuilds the case: above 0.04 without the overlay, 0.0 with it. Patch version.
- **2026-09-24** 0.10.9: Hermes imports stop scoring the uploader's edition and disc tags. *Anthology: 25 Years* (Can, acquisition 74) downloaded as the right 12+17 CD rip, the agent gave beets the right release, and quiet mode skipped it: an interactive re-run scored 90.6%. The uploader had tagged the set as two albums, "Anthology - 25 Years (CD 1)" and "(CD 2)", every file disc 1 of 1, so beets charged `mediums` once and the per-track `medium` on all 17 second-disc files, which was most of the 9.4%; the track titles and lengths matched. A Hermes import has its release pinned from the files' shape, and `from_scratch` discards those tags anyway, so the import overlay now sets `match.distance_weights` to 0 for `album`, `mediums`, `medium`, `year`, `label`, `catalognum`, `country`, `media` and `albumdisambig` (as 0.9.1 did for `match.preferred`). The artist, each track's title, length and index, and `max_rec` on missing or unmatched tracks still decide; `strong_rec_thresh` is unchanged. `beets-hermes/tests/test_overlay.py` rebuilds the case against real beets scoring: 0.096 and not strong without the overlay, 0.004 and strong with it, and unrelated track titles on the same shape stay above the medium line. Manual imports keep the full weights. Agent image change, no route change: patch version.
- **2026-09-24** 0.10.8: the score rule and the number comparison, the two corpus gaps that were defences the tests assumed. (1) The score was half artist, half title, so a matching artist carried any title similarity of 0.7 over the 0.85 threshold: the 0.8 cap on a tracker-only subtitle ("Rival Dealer: Remixes", "Homogenic - Live") never rejected at score level, and "Silent Alarm Remixed" (0.75) and "Interstellar Overdrive" (0.78) went through on the artist. Now `score = min(title, (artist + title) / 2)`: the title decides, the artist can only lower it. Every real accepted row is unchanged (their titles score 1.0; "bôa (1)" stays at 0.875 because the artist still lowers), and the 20 captured ranking scenarios still pick the same winners. (2) Numbers were compared exactly only inside a tail, so "Album" accepted "Album II" in the same year and "Title, Pt. 1" accepted "Pt. 2"; conversely "(Volume II)" rejected "(Volume 2)". Now every title comparison goes through `_similar`, which reads Roman numerals and number words up to ten as digits (`as_digits`) on both sides and returns 0 when the numbers differ; the pronoun "I" becomes "1" on both sides alike, which changes nothing between them. With numbers checked everywhere, a target's subtitle may also match the listing alone (`t_kind` subtitle, not only alt): "Music for Airports" for "Ambient 1: Music for Airports", which the old formula reached through the artist. Eight corpus rows lost their `gap:`; three remain (a variant in parentheses, "VA", a noise-word title's query). The Can sibling "Anthology 1968-93" now scores 0 (its title carries years the target lacks) instead of 0.846. Patch version.
- **2026-09-24** 0.10.7: a matching corpus. Every search miss so far (apostrophe, ampersand, accents, subtitle) was found live, one a week, and fixed with a one-off test in whichever file was nearest; nothing asserted a whole pair (target as MusicBrainz names it, listing as the tracker titles it, verdict, queries). `tests/fixtures/matching/pairs.yaml` now holds those pairs: real rows from the acquisitions a human approved or imported (read back from the running instance's API, indexer names left out), one invented row per mechanism, and the query sequence from a new pure `search.search_queries` (the full query, the title's head, the artist alone) that `run_search` now iterates instead of inlining. Building it surfaced verdicts the matcher gets wrong, kept as `gap:` rows (strict xfail) rather than fixed in the same change: (1) the combined score is half artist, half title, so with a matching artist a title similarity of 0.7 clears the 0.85 threshold, and the 0.8 cap on a tracker-only subtitle ("Rival Dealer: Remixes", "Homogenic - Live") does not reject at score level, though the unit tests that guard it assert the title similarity alone; on the tracker the Remix and Live album types catch most of these, a plain-typed one would go through. (2) Numbers are compared exactly only inside a tail, so "Title, Pt. 1" accepts "Title, Pt. 2", "Album" accepts "Album II" the same year (Led Zeppelin's first two are both 1969), and "Album, Volume 1" accepts "Album Vol. 2"; conversely "(Volume II)" rejects "(Volume 2)". (3) Any parenthesis on a listing reads as an alternative title, so "Album (Instrumentals)" accepts. (4) "VA" is not read as Various Artists. (5) A one-word title that is a noise word ("A") leaves the first query as the artist alone. The thin real margins are on record too: "Anthology 1968-93" at 0.846 against the 1994 target, and "bôa (1)", a tracker's disambiguated artist name, accepted at 0.875. Patch version: tests and a refactor, no config, migration or route.
- **2026-09-24** 0.10.6: the title's head as a query before the artist alone. *Anthology: 25 Years* (Can, 1994) ended `NO_MATCH`: the tracker lists it as `Can - Anthology`, so `CAN Anthology 25 Years` found nothing, and the artist-only fallback `CAN` answered 163 rows of "Can't Buy a Thrill" and "I Can't Breathe" (the index matches the token in any field, newest first) with nothing by Can older than 2026. Measured through the dev Prowlarr against the tracker: `CAN Anthology` 6 rows including the album; `CAN` at limit 500, 163 rows; `{Artist:Can}` 188 rows, all by Can, still without the 1994 album; `{Artist:Can}{Album:Anthology}` 3 rows including it. Two corrections to the 2026-09-19 entries: Prowlarr does not page a Gazelle indexer, whatever `limit` says, so the artist-only fallback is the artist's newest page of groups (fine for a small catalogue, a lottery for a large one); and Prowlarr's structured album field does filter (the 0.9.6 measurement carried an apostrophe in the album, which is what failed). Decision: when the title has a tail by `matching._split_tail` (subtitle, soundtrack label, parenthesised alternative), an empty full query is followed by the artist plus the title's head, as its own event, before the artist alone; matching already scores the head listing 1.0 against the subtitled target (the sibling `Anthology 1968-93 (1993)` scores 0.846, under the 0.85 threshold, a thin margin worth knowing). One Prowlarr call more, only for titles with a tail. Not adopted: the structured `{Artist:}{Album:}` query, which found this case too but is a second query grammar to keep measured, and the plain head query does the same work; Gazelle's `artistname` field for the artist-only fallback, which removes the noise but not the one-page cap.
- **2026-09-23** 0.10.4: suggestions on the request form, so a request carries a release-group MBID and never meets fuzzy resolution. Two-stage: the artist as you type (MusicBrainz artist search on the bare text; its n-gram index ranks "sigur ro" and "ac dc" correctly from three characters, so no wildcard, no fuzzy term, nothing to escape), then that artist's official albums and EPs in one request (a search with `status:official`; browse cannot filter status and Radiohead has 415 album/EP groups by browse against 39 official), filtered locally as you type. Designed with an independent review and a survey of prior art (Headphones' artist-then-browse flow, ListenBrainz's throttled frontend search, Picard's escaping; MusicBrainz's own site uses an unstable internal endpoint). The review removed a limiter priority scheme (the limiter's lock is not held during requests, so nothing queues for long) and request coalescing, and added the real guard: unauthenticated suggest routes could flood MusicBrainz under Hermes's User-Agent, so suggestions run one at a time behind a slot that answers empty when busy, with one attempt and a five-second timeout (new `retries`/`timeout` arguments on the client's request method) and TTL caches (cachetools). Widget: autoComplete.js vendored (9 KB, Apache-2.0, ARIA combobox, built-in debounce) over a hand-written listbox, since the JavaScript is untested in CI either way and the library's combobox handling is; over GOV.UK's (54 KB with Preact), Awesomplete (Android keyboard and TalkBack bugs) and `<datalist>` (cannot carry an MBID). No config key: one user, a bounded budget. Versioning: routes and a dependency are not in the minor list, so a patch. Not done: a fuzzy fallback in the resolver for requests by API or without JavaScript (one line, separate change).
- **2026-09-22** 0.10.0: genres on the queue and detail pages, from MusicBrainz's release-group genres (the moderated subset of its tags; the folksonomy tags are what made `musicbrainz.genres` unusable in beets). Measured on the 19 albums then awaiting approval: 18 have release-group genres; the one without has none on the artist either, so no artist fallback. Zero extra requests for new targets: the release-group lookup that creates a target now includes `genres`, and `upsert_target` stores the top five with their vote counts on `AlbumTarget.genres`; targets from before are NULL and `services/genres.py` fills them in from a five-minute job (active rows first, twenty a tick, stops for the tick when MusicBrainz is unreachable), also `hermes genres`. Shown as plain muted text on a line of its own under the title, two on a queue row (the status line wraps at about 44 characters on a phone), three in the detail header; not pills, which are the state. Raw counts would show "rock · indie rock" for most indie albums, so a genre whose words all appear in a more specific one is dropped when that one has at least half its votes ("indie rock · post-punk revival" for Interpol, "industrial rock · industrial metal" for Nine Inch Nails; a heavily voted "rock" survives one stray "math rock" vote). Stored more than shown so the rule, or a filter chip, can change without another MusicBrainz pass. Same change: the row's playlist name drops the user and the full date ("from Weekly Exploration, 09-14"), so the extra line costs no row height on desktop; on a phone, where the state pill and the time share the status line, the genres join the pill's line instead (0.10.1: the first cut gave them their own line there and every row grew by one). A real phone then showed both lines still wrapping ("UTC" alone on a line in every row, genre pairs broken mid-name) and a second, independent review of the screenshot agreed with the first on the fix (0.10.2): the row is what the album is, then where it stands; every phrase a no-wrap unit; the phone row loses the timestamp (the order encodes age, the detail page has the history) and "missing", which is every queued album's status, so an owned lossy copy stands out by being rare. Then (0.10.3) the pill and the playlist still would not share a phone line, because `AWAITING_APPROVAL` was the widest thing on the row: the pill's text became a short human label (`STATE_LABELS`, "needs approval", "import review", "ready to import"...), the same everywhere a state is shown to a person, with the raw name kept as the CSS class and the API value. Version 0.10.0 (migration). Not done: refreshing genres as votes change (the queue is short-lived), a genre filter, passing the genre to beets (lastgenre owns the library's).
- **2026-09-22** 0.9.8: the Navidrome scan is gated both ways. Five imports ran back to back on 2026-09-18 and *Dear Science* came out of Navidrome as ten tracks plus one on "[Unknown Album]": each verified import asked for a scan at once, the agent's queue was already writing the next album, and the scan for *Sound Awake* walked the *Dear Science* folder 26 s before beets finished it. beets touches every copied file several times (tag write, scrub, embedded art), so the scan read track 5 between scrub and rewrite, with no tags. The scan Hermes then requested for *Dear Science* itself re-read nothing: Navidrome's quick scan re-reads a known file only when its mtime is newer than the last scan, and `importadded.preserve_mtimes` sets the copies back to the source's years-old mtimes. Same mechanism as the "invalid file" Navidrome logged for a *Pink Moon* track mid-write two minutes earlier, which healed only because that file was not yet in its database. Fix: a verified import puts its id on `Context.navidrome_scan_owed`; the importer tick asks for one scan, after its own starts, only when nothing is `IMPORTING`, and puts the event on every acquisition covered; before starting a ready import it asks `getScanStatus` and holds the row for a tick with one note while a scan (ours, the nightly one, a manual one) runs. A human's retry from the UI or API meets the same gate (409 while a scan runs). Navidrome unreachable never holds an import; a failed request warns once per covered import and drops the debt (the nightly scan picks the albums up). The tick is serialised with a lock, since the gate reasons about "nothing is importing"; a fresh process derives the debt from the events. Not covered: a manual `beet import` in the pod under an external scan, the same exposure as before. Kept in reserve if the nightly scan ever lands on an import: an empty `.ndignore` written by the agent plugin into the album folder from beets' apply hook and removed at `album_imported` (Navidrome skips the folder, and deleting the marker bumps the folder mtime so the next quick scan imports it fresh), at the cost of a marker that outlives a crashed import and hides the album. Existing damage needs one full scan (`startScan` with `fullScan=true`, or the activity panel); quick scans will keep skipping those files.
- **2026-09-16** 0.9.2: the agent binds the library's music directory on every read. beets 2.14 stores item paths relative to `directory` and resolves them through a context variable set on the thread that opened the `Library`; the agent's HTTP worker threads start without it and returned `Tool/Fear Inoculum`, which Hermes read as outside `/music` and sent to review. `LibraryReader` now wraps each read in `Library.music_dir_context()` (a no-op on older beets, which store absolute paths); the plugin tests' library keeps its items under its own directory so the relative storage is exercised; Hermes names a relative path from the agent as an agent version problem rather than an interrupted copy. No `AGENT_API` bump: the response shape is unchanged.
- **2026-09-16** First real grab in the cluster: the download went through and beets skipped the import. Interactive re-run: a 95.7% match on the release Hermes chose (the 7-track European CD, right for a 7-file rip) with only `media` and `country` penalised. Those come from `match.preferred` (Digital Media before CD; XW, US, GB), which exists to order candidates; a Hermes import has one candidate, so the preferences could only pull it below the 4% strong line and `quiet_fallback: skip` did the rest. Fix (0.9.1): the agent runs every Hermes import with `beet -c <overlay>` that clears `match.preferred` (written to its jobs dir at start; manual imports untouched), and Hermes's own country order becomes beets' (XW, US, GB; XE dropped) so the two pick alike. Not changed: the thresholds, which keep track count and titles tight. Noted for later: parsing the catalogue number out of Gazelle titles would pin the exact release for most CD rips.
- **2026-09-16** `deluge.layout` (0.9.0): `per_acquisition` (the default, the old behaviour: `<root>/<acquisition id>`) or `flat` (torrents land straight in the roots beside every other download). Reason: the user's download pool is one flat directory of thousands of folders that several Deluge instances share for cross-seeding, and a future cross-seed tool (§12 Q11) will be written against that pool; the per-id folder's one benefit, adopting an interrupted submit by directory, is minor and has Deluge's "already in session" reply as the fallback. In flat mode the directory adoption is skipped (it would scan the whole pool and write a warning per stranger). The observer already records the finished path as Deluge's save path plus the torrent name, so the importer is unchanged; multi-file torrents always carry their own root folder, so nothing lands loose in the pool. Same-named torrents collide as they do for any manual add. Existing downloads stay where they are.
- **2026-09-16** UI phase B of `docs/ui-plan.md`: album art. `integrations/coverart.py` fetches the Cover Art Archive's `front-500` (preferred release first, then the release group) and `services/art.py` stores it as served under `<data dir>/art`, keyed by release group; no image library. Three columns on `AlbumTarget` (`art_status`, `art_checked_at`, `art_failures`; existing rows backfilled as pending, active ones fetched first, twenty a tick), a five-minute scheduler job kicked by a manual request, `GET /art/{mbid}.jpg` with a year-long cache header, `hermes art`, and one config key `art.enabled`. Cosmetic by construction: never inline, never on `/healthz`, session committed around every fetch. Version 0.8.0 (migration and config key).
- **2026-09-15** UI phase C of `docs/ui-plan.md`: a human can put a candidate in front of the ranker's choice (`approval.prefer`, `POST /acquisitions/{id}/prefer` in the UI and `POST /api/acquisitions/{id}/prefer` with `{candidate_id}`, `hermes prefer`). It only renumbers ranks and records an event; the grab still goes through Approve and its preview, so nothing spends ratio in one tap. Rows offered: ranked ones and `keep_candidates` cuts ("Use anyway"); refused: torrents already attempted (Approve would silently take another) and quality/match/type rejections (`submit()` re-checks the sample rate on the torrent; the others are a different album). Version 0.7.3.
- **2026-09-15** UI phase A of `docs/ui-plan.md` (revision 2, after an adversarial review): the request form is the landing page, the queue moves to `/queue` and the finished list to `/history` (old `/?state=` and `/?page=` links redirect), a tab row and hidden `opt` columns make the pages usable on a phone without JavaScript, the detail page gets a target card built from the same approval preview and a prev/next walk in the queue's order (now oldest first within a state, as SQL), and an action advances to the next item only when it left the filter and not into `FAILED`. The rules are in `docs/conventions.md` "UI". A9 stands: the one script gained a `defaultPrevented` check so a cancelled confirm() no longer leaves a button on "Working…".
- **2026-09-03** Hermes fetches the `.torrent` and adds it to Deluge itself; Prowlarr is search-only (A1). Reason: the Prowlarr grab endpoint returns no torrent hash and private trackers expose no infohash.
- **2026-09-03** beets stays a separate, standalone deployment; Hermes integrates only through plugins running in the beets pod (A2, A3, A3b). Reason: beets must remain usable by hand and one pod should own `library.db`. (Originally the `web` plugin for reads plus `beets-hermes` for writes; superseded 2026-09-06.)
- **2026-09-03** Navidrome playlist sync is out of scope; the `navidrome-listenbrainz-daily-playlist` plugin covers it. Pending Q7.
- **2026-09-03** Automated grabs require approval by default, with opt-in auto-approve rules and a weekly budget (A8). Reason: Weekly Exploration is 50 unowned tracks by construction.
- **2026-09-04** Cluster facts adopted from the flux repo (`clusters/home/{beets,deluge}`, read-only): beets is a linuxserver StatefulSet with the web plugin already on and `copy: yes / move: no / incremental: yes`; Deluge is three 2.2.0 instances (ops, red, other) with Label enabled and pending → complete moves. Consequences: Hermes routes each torrent to a Deluge instance by Prowlarr indexer name and records it on the GrabAttempt; torrents download to `pending/hermes/<id>` and move to `complete/hermes/<id>`; the beets path mapping is `/downloads/complete` → `/downloads`; the agent passes `-I`; a `beets-web` Service and an agent container must be added to the flux repo (`deploy/beets/`).
- **2026-09-06** `beets-hermes` owns the whole beets interface, reads included; the `web` plugin is no longer a Hermes dependency (A3). Reason: the web plugin's JSON is an internal shape, it is a second unauthenticated process with delete/patch a flag away, and one plugin on one port is the cleaner contract for anyone else running Hermes.
- **2026-09-06** Development happens in a Docker Compose stack (beets with a synthetic seeded library, one Deluge with a fixture torrent, Hermes from source, production Prowlarr for search only); the cluster is reserved for the M6 live weeks (A12). Reason: submit/observe bugs against production Deluge cost ratio; against compose they cost nothing. The library is synthetic rather than a copy of production so the environment needs no cluster access or personal data to recreate.
- **2026-09-07** Images and versions. Both images build on every push to `main` (GitHub Actions, Docker Hub, the same account as `<namespace>/deluge`): `hermes` from the root Dockerfile and `beets-hermes` from `deploy/beets/Dockerfile`, tagged `latest`, `<version>` and `<version>-<sha>`; the cluster pins the commit tag, never the other two (the flux repo's own lesson about cached version tags). One version for the repo, starting at 0.5.0 because M5 is done; pre-1.0 the minor is the milestone or any change that adds a config key, migration or agent route, the patch is fixes; 1.0.0 after M6. Compatibility is an integer agent API version: the plugin reports `agent_api` in `/healthz`, Hermes requires the one it was built for, and a mismatch is a red health check that blocks imports with the fix named. The first build of a version also creates the git tag `v<version>`; later pushes with the same version rebuild it (the commit tag tells them apart).
- **2026-09-07** Library-quality additions to the beets config (`deploy/beets/config.yaml`, mirrored in dev): `badfiles` (`flac -t` at import), `match.max_rec` medium for missing or unmatched tracks, `from_scratch`, `languages: [en]`, `ftintitle`, `musicbrainz.extra_tags`, `lyrics` from lrclib, `lastgenre` count 2 / prefer_specific, `fetchart` minwidth 600 / enforced ratio / Cover Art Archive before scans, `art_filename: cover` for Navidrome, `importadded`, `unimported`; `discogs`, `hook` and `smartplaylist` left commented. Two things the rehearsal caught and reversed. (1) `chroma`: merely loading the plugin moved beets' distance for the correct release-id match on the Ghosts CD rip from 0.017 to 0.143 (medium), so quiet imports skip; `auto: no` changes nothing. It is out, with a one-off `beet -c chroma.yaml` recipe for untagged manual rips. Found by bisecting plugin lists with beets' own `tag_album`. (2) `musicbrainz.genres: yes` fills the genre with every folksonomy tag ("creative commons", "os:ok", twenty more) and `lastgenre` does not overwrite an existing genre; off, and lastgenre alone gives "Ambient, Electronic". Rehearsal with the final set: Ghosts I–IV through the agent in 66 s, correct release, 36 tracks, `cover.jpg`, art embedded, lyrics on every track, R128 gains, source timestamps kept. Also fixed: the dev config had carried two `plugins:` lines (the derivation script had anchored on the word in the header comment), so YAML's last-key-wins had kept the full set active; it now has one.
- **2026-09-07** Production beets config drafted and rehearsed. `deploy/beets/config.yaml` is the cluster's config with the Hermes changes marked at the top (`hermes` in, `web` out, `resume: no` because a quiet import with stdin closed cannot answer the resume prompt, `%aunique{}` and the common track format on the soundtrack path, `match.preferred` so manual imports pick the releases Hermes prefers). `dev/beets/config.yaml` is that file with one difference: the slow on-import plugins commented out, swappable for a rehearsal. Rehearsal: with fetchart, embedart, scrub, replaygain (ffmpeg, R128) and lastgenre all on, the agent imported Ghosts I–IV (36 FLAC tracks, two discs, 605 MB) in 36 s in the compose stack: correct release, genre from Last.fm, R128 gain on every track, album art fetched and embedded, `hermes_acquisition` set, files at `/music/Nine Inch Nails/Ghosts I–IV/`. So the image has the toolchain the cluster config needs and the 30-minute import timeout has a wide margin on this hardware.
- **2026-09-07** The beets pod runs the agent as its only container. The draft had added `hermes-agent` as a sidecar beside the linuxserver image's s6 init and web UI; the web plugin is unused, so the patch now replaces the container's process instead: same image (linuxserver base plus the plugin, so its ffmpeg/chromaprint toolchain and update cadence stay), `hermes-agent` as the entrypoint, `/healthz` as the readiness probe, port 8337 gone, `beet` still on PATH for manual imports in the same container. A custom beets image was weighed and deferred: it would trade linuxserver's maintained toolchain for a rebuild-and-retest on every beets upgrade; the Dockerfile's `FROM` line is the one place to change if that trade ever becomes worth it. Consequence to check at rollout: with the init bypassed nothing chowns `/config`, so it must already belong to uid 1000.
- **2026-09-07** beets' incremental history. Hermes imports run with `-I` (a retry must not be a silent no-op), so nothing Hermes imported was recorded in `state.pickle`'s tag history and the user's manual `beet import /downloads` would meet every Hermes album as a `duplicate_action: ask` prompt. Now, after verification, Hermes asks the agent to record the folder; the agent uses beets' own directory grouping so the keys match what a manual run computes (multi-disc folders are one group of disc directories). Best effort: a failure leaves a warning event and the import stands. Not recorded: imports in review or failed, which the user should still meet by hand. Known limit: the state file is rewritten whole without a lock, so a manual import finishing at the same second can lose one entry (one duplicate prompt later, nothing worse).
- **2026-09-07** Second code review (M5 and the day's follow-ons, four lenses, independent reviewer). Fixed: an upstream failure mid-playlist (MusicBrainz, beets, Prowlarr) now stops the playlist with its remaining tracks still pending and the next tick resumes it, instead of marking tracks failed and the playlist ingested; a signal's resolution and its acquisition are committed together; the daily re-search also finishes `RESOLVED` rows through the library check and retries transiently `FAILED` rows with no download, refreshes each row before acting so a cancel in the UI meanwhile is honoured, checks the retry delay before the give-up, and runs under the discovery lock; "one non-terminal acquisition per target" is a partial unique index, so a CLI tick and the server cannot both create one; adoption of an interrupted submit requires a candidate whose reported infohash is the torrent's, otherwise the fetch proceeds and Deluge's "already in session" records the attempt; the startup cleanup removes only `<id>-<infohash>.torrent` files and never the directory; edition flags the policy does not name count as reissues; a partial MusicBrainz track list yields no playing time and is not cached; size-based rate rejections at search time allow ten percent for artwork; title matching is asymmetric: a subtitle only the tracker has ("Rival Dealer: Remixes", "Homogenic - Live") is a different release, and when both sides carry one the tails must agree ("Day One" is not "Day Two"). Deferred: a version column on `Acquisition` for optimistic concurrency; classical and split-credit release groups still resolve to `nothing_acquirable`; `POST /api/jobs/discover` runs inline. Tests added for each fix, including the reviewer's must-not-match table.
- **2026-09-07** 24-bit first. `quality.encoding_preference` now defaults to `[lossless24, lossless]`: with the sample-rate limit keeping 192 kHz out, a 24-bit copy within the limit is the better copy of the same album. Two consequences were tuned on the captured searches: the reissue penalty rose to 0.8, above the encoding gap (0.75), so a 24-bit unnamed reissue ("Definitive Edition", "OKNOTOK") does not displace the plain 16-bit album; and editions get a rate estimate too, from the tracker's file count times the album's mean track length (extras only lower the estimate), so a 192 kHz reissue is refused at search time rather than only at submit.
- **2026-09-07** Sample-rate limit without metadata (`quality.max_sample_rate_khz`, default 96). Tracker titles say "24bit Lossless" and nothing more, but FLAC bitrate bands are a factor of two apart (24/48 about 1100-1700 kbps, 24/96 about 2200-3300, 24/192 about 4000-6000), so size over playing time places a release reliably. The playing time is one release's track lengths from MusicBrainz, fetched once per target (`AlbumTarget.track_lengths`, two calls the first time). At search time the estimate is shown on every lossless candidate ("~96 kHz, 2667 kbps") and rejects a clear overshoot, but only for the plain album: an edition's playing time differs (OKNOTOK is two discs), so its estimate runs high and the decision waits. At submit time, with the torrent's per-file sizes in hand, the median per-track bitrate is the real test and a candidate over the limit is skipped for the next one. A rate written into the title ("24/96", "192kHz") is believed over any estimate. Checked against twelve captured tracker A searches with real playing times: the 24-bit uploads split into 48/96/192 bands as their media suggest, and the rejections were the 192 kHz hi-res WEB versions, 5.1 SACD rips and Blu-ray audio.
- **2026-09-07** Search-only pass against tracker A (a temporary key in the compose Prowlarr, query limit 100, grab limit 2, dry run on). Twenty searches for well-known albums were captured as fixtures with the download links redacted (`tests/fixtures/prowlarr/gazelle/`), and their 804 distinct titles became the Gazelle corpus (`tests/fixtures/titles/gazelle.txt`, replacing the hand-written guesses as the reference). The parser read every music title; the one miss was an audiobook listed under music. Replaying the twenty result sets through matching and ranking found two things. (1) Editions: deluxe, anniversary and box-set uploads tied with the plain album and won on seeders (a 1.4 GB Rumours box, an 831 MB Abbey Road Super Deluxe). New `quality.edition_penalties`: 1.0 for deluxe/expanded/anniversary/bonus/special edition, 0.1 for a remaster, 0.25 for any other reissue label; a plain CD rip now beats a deluxe WEB release but not a plain one, and a popular reissue cannot outrank the plain album on seeders. (2) Subtitles: MusicBrainz's "Interstellar: Original Motion Picture Soundtrack" matched nothing because the tracker says "Interstellar"; matching now also compares titles before a subtitle or soundtrack tail, and artist credits before a "performed by" or "feat." tail. `tests/integration/test_ranking_scenarios.py` pins the expected winner and rejection reasons for all twenty; add a case there whenever a real result set ranks wrongly. The tracker A indexer should be removed from the dev Prowlarr and the key rotated once this pass is over.
- **2026-09-07** Hermes keeps no torrent file. Until now every submitted `.torrent` was written under the data directory "for re-adds": on a private tracker that file carries the account's passkey in its announce URL, and nothing ever re-added one (Deluge holds the file for seeding; Hermes tracks by infohash, as Lidarr does). Now the bytes live only inside the submit call; the files' shape the importer needs is read there and stored on the `GrabAttempt` (`download_shape`); leftover files are removed at startup with a warning; and the crash window between the Deluge add and the attempt commit is closed by adoption: before fetching, submit looks in every configured Deluge for a torrent at this acquisition's pending or completed directory and records it as the attempt. Prowlarr's download URL keeps its API key on candidate rows, as the arr apps do in their own databases: that service has no ingress, and the key is added by Prowlarr for its own proxy.
- **2026-09-07** First live grab through the discovery-attached path (Ghosts I–IV, 605 MB FLAC from PandaCD, dry run off). Two findings. (1) The importer picked the wrong release: the release group holds a 9-track "Ghosts I" beside the 36-track album, both official, 2008, worldwide, and the tie went to list order; beets in quiet mode skipped 36 files against a 9-track release (`IMPORT_NEEDS_REVIEW`, the right outcome for a wrong hint). First fix (track count) picked the 36-track digital release, which beets still skipped at distance 0.093: the files are a two-directory CD rip and beets scores the single-medium digital release as a track-order mismatch, while the two-CD US release scores 0.017 (measured with beets' own `tag_album`). Fix: the release-group lookup carries per-medium track counts and formats (`inc=media`), the importer reads the torrent it kept for the files' shape (`DownloadShape`: audio count, files per directory, CD when a log or cue sheet travels with them or the title names the medium), and `pick_release` prefers the matching count, then the matching disc layout, then the matching medium, then year, country and a plain release over a disambiguated variant. Retried: `IMPORTED`, 36 items, seeded files intact. (2) Deluge's UDP tracker announces time out under Docker Desktop although the tracker, the network and repeated announces from a plain socket in the container all work; a port change gets one good announce and the download then completes. Environment, not Hermes: the compose file no longer publishes the UDP port and docs/conventions.md records the Docker Desktop setting to change. Hermes behaved correctly throughout (approve → submit → observer → import → review with the beets log tail).
- **2026-09-07** M5 discovery decisions. (a) Automated signals whose recording is on no policy-acceptable album are `ignored` on the Signal, not `NEEDS_REVIEW`: review is for questions a human can answer, and the queue must stay readable at fifty tracks a week. (b) An `ignore` playlist records the playlist, not its tracks. (c) `NO_MATCH` is re-searched by the daily job once `search.retry_days` have passed, up to `search.max_retries` searches, then `REJECTED` ("gave up") so a later signal does not revive it while a manual request still can; `RESOLVED` rows that never got a search are picked up by the same job. (d) A patch name the policy does not list (ListenBrainz also generates `lb-radio`, `top-discoveries-of-<year>`, ...) is ignored, never acquired by surprise. (e) Secrets live in `.env` (gitignored, denied to tooling in `.claude/settings.json`; a separate `.env.secrets` was tried and dropped as unused on 2026-09-08); the ListenBrainz token is optional because the reads Hermes needs are public. (f) One inline script on the site: a submit disables its button and says "Working…", because a request waits seconds on MusicBrainz and the tracker; A9 now reads "plain forms, `confirm()`, and that".
- **2026-09-07** Second, independent UX review (a fresh reviewer walking the dev stack against sections 4 and 5). Fixed: the `NO_MATCH` copy no longer promises a scheduled re-search that M5 has yet to add; a dry-run approval is visible afterwards ("Approved by ui at … while dry run was on", button reads "Approve again") and its event is no longer a warning; a retried request is closed as `CANCELLED` with a "Continued as #N" link, and only a transient failure offers Retry (a "no such album" verdict offers the MBID field and a MusicBrainz search link); a repeat request says "Already requested as #N" and a reviewed request folded into an in-flight acquisition is `CANCELLED`, not `REJECTED`, with a notice naming the survivor; a repeat request for a `RESOLVED` row continues it to the search; the approval page states what Approve fetches and where Deluge puts it, and shows a routing miss before the click; every page, error pages included, carries the mode badges (dry run or live, timid or auto-approve); "Search again" is keyed to the search counter; the dry-run note is only written when there is something to grab. Deferred to M5 with the ingestion work: submit-time feedback on the request form and the queue state filter. Nice-to-haves left open: "Use anyway" wording on policy-rejected review rows, a hint for lossy-owned albums, a result block on `IMPORTED`, trimming event JSON, CLI parity for reject/cancel/search.
- **2026-09-07** Screenshot follow-ups: the UI has a native dark palette keyed to `prefers-color-scheme` (the browser's forced-dark rendering made state badges unreadable); the resolution table and queue action cells no longer wrap; a `FAILED` request that never resolved offers "Retry request" (`POST /api/acquisitions/{id}/retry`, `services.requests.retry_request`), which re-submits the original signal as a new acquisition and links the two by event. Reason: the first Godspeed request failed before the loose-search fallback existed and had no way forward except retyping it.
- **2026-09-07** UI/UX review after M4 (from rendered pages and real responses; no visual pass since the browser extension was offline). Fixed: unresolved requests are named by what was asked for (acquisition → originating signal link, backfilled from event data); `NEEDS_REVIEW` pages show the resolution candidates with a "Use this" action (`POST /api/acquisitions/{id}/resolve`, `services.requests.resolve_manually`); browser errors render an HTML page with a way back instead of JSON; every form action redirects with a one-line notice ("Approved. Dry run is on, so nothing was grabbed."); the queue offers Search on `RESOLVED`/`NO_MATCH`/`FAILED` rows and a "review" link on review states; state hints explain `NO_MATCH`, `STALLED`, `NEEDS_REVIEW` and `IMPORT_NEEDS_REVIEW`; Reject and Cancel confirm; timestamps carry a UTC label; in-progress pages refresh themselves; tables scroll on narrow screens; form fields have labels; the `STALLED` action reads "Try next candidate"; the resolution candidate dump no longer clutters event details. Deferred to M5: a state filter on the queue (needed once Weekly Exploration adds fifty rows a week).
- **2026-09-07** Full code review after M4 (four independent lenses: state/concurrency, failure handling and safety, the beets plugin and test fidelity, domain logic). Fixed: the observer's stall fallback now respects `dry_run`/`timid` (a human approves the next candidate from `STALLED`); the Prowlarr API key and Navidrome token can no longer reach events via exception text (proxied downloads use a header-less client and sanitised errors); re-search after a grab attempt supersedes referenced candidates instead of deleting them (foreign key) and never strands `SEARCHING`; write transactions are committed before external calls so a scheduler tick cannot block the event loop on the SQLite busy timeout; finished torrents in Deluge's `Queued`/`Paused` states count as done, an unmoved finished torrent is accepted at its actual location after a 10-minute grace, and "waiting" events are deduplicated; a grab attempt is recorded before labelling; a missing hash must be missing on two ticks before it counts as removed; imports with a non-zero exit, an agent error, or an album path outside `beets.library_root` go to review rather than `IMPORTED` (beets registers the album before copying, so an interrupted copy leaves the library pointing at the seeded files); the agent rejects `import.delete`, tolerates a corrupt `jobs.json`, writes it atomically, terminates options with `--`, closes stdin, exposes the beets `-l` log, and handles SIGTERM by finishing the running import; ranking rejects a release type that does not match the target (a title-track Single cannot stand in for the Album) and a year mismatch without a remaster year, enforces `keep_candidates`, adds `indexer_preference`, and weights seeders below log/cue; resolution no longer hides a confident EP behind a weak Album; punctuation-only titles compare raw; policy validators catch a missing `default_instance`, equal pending/completed roots and bad label names; the path mapping respects segment boundaries; a repeated manual request no longer counts as approval in timid mode. Deferred: an import timeout does not cancel the agent job (no cancel API yet); the `_missing_once` marker is in-memory, so a restart between the two ticks costs one extra tick. Regression tests live in `tests/integration/test_review_fixes.py`.
- **2026-09-07** The weekly budget is removed in favour of `approval.timid` (A8). Reason: the budget was two things in one, economics and blast radius. Economics needs ratio and token data Hermes cannot see through Prowlarr (v1.5 account guard); blast radius is Prowlarr's per-indexer Grab Limit. What remains is a single switch: timid means a human approves every grab. Torrent-fetch failures now defer the indexer instead of discarding candidates, so an exhausted token supply on a "Required" indexer does not burn through the list. Verify once that Prowlarr's download proxy counts against its Grab Limit.
- **2026-09-07** `HERMES_DRY_RUN` (env) overrides `policy.dry_run` so a deployment or the compose stack can flip it without editing YAML; the compose default is `true`. A repeated manual request for an album already sitting in `CANDIDATES_READY` or `AWAITING_APPROVAL` re-runs the approval gate, treating the repeat as "go". `CANDIDATES_READY → SEARCHING` is a legal edge (re-search).
- **2026-09-07** Prowlarr joins the compose stack with PandaCD (Creative Commons / artist-permitted music, public, native Prowlarr definition) as the dev indexer, and the dev library is reseeded from CC albums that exist on both PandaCD and MusicBrainz (A12). Reason: the full acquisition path can then run on legal, downloadable albums without production credentials or the home CA. Acknowledged limit: real Weekly Exploration recommendations will rarely be on PandaCD, so the automated path is exercised in dev through `listenbrainz.extra_playlists` with a hand-built CC playlist, and recommendation quality is judged only in M6.
- **2026-09-08** beets image versioning and updates (0.5.1). The `beets-hermes` image is beets with the plugin, so its tag is the beets release inside it (`2.14.0`, `2.14.0-<sha>`, read from the built image with `beets-python`), not Hermes's version; sharing Hermes's number said nothing about the beets you got and republished an identical image on every push. The image is only rebuilt when `beets-hermes/` or its Dockerfile changed. The base image is pinned by tag and digest and moved from 2.5.1 (the digest the cluster still runs) to 2.14.0; Dependabot proposes later releases, and the workflow runs the plugin's tests inside the built image on the PR and before any push, because the plugin imports beets internals (`beets.importer.state`, `beets.importer.tasks`) that a release can move. The plugin's own lock follows the same beets version for fast local tests. Compatibility stays the `AGENT_API` integer. Rehearsed in the compose stack: agent healthy on 2.14.0, 14 plugin tests pass inside the image.
- **2026-09-08** `search.indexers` (0.6.0). Prowlarr decides which indexers a search reaches (enabled, audio category, not backed off, under its query limit); on a Prowlarr shared with Lidarr that includes general trackers whose audio sections are not worth a query per acquisition, and disabling them there would take them from the other apps. Hermes now passes `indexerIds` for the names listed in `search.indexers`, resolved through `/api/v1/indexer` at search time (names, not ids: ids do not survive rebuilding Prowlarr, and names are what `deluge.instances` already uses). Empty keeps the old behaviour. Unknown or disabled names are warning events on the acquisition so a renamed indexer is noticed; a list resolving to nothing is a retryable failure, never a silent search of everything.
- **2026-09-08** badfiles at import. The beets config had `check_on_import: yes` and claimed a corrupt file sends a Hermes import to review; the plugin's source (beets 2.14.0) says otherwise: both import actions default to `ask`, and in a quiet import `ask` means print and continue. Now `import_action_on_error: skip` (a corrupt file refuses the album, the importer sees no album and sends it to review) and `import_action_on_warning: continue` (an unset MD5 in STREAMINFO, common on WEB releases, and MP3 Xing header counts are warnings, not damage; `skip` there would refuse most of the library). Found while reading a `beet bad` run over the production library: 393 flagged files, 307 of them those two warnings, 15 rows with no path, 71 genuinely broken across 13 albums.
- **2026-09-08** beets config after the first cluster session on 2.14.0. `lyrics.auto: no`: a Genius 429 storm (beets' shared built-in token) held an interactive import for minutes, and a quota on a lyrics site must never hold or fail a Hermes import; `beet lyrics` on demand instead, with `lrcmux` added as a third source and a note on the undocumented `genius_api_key`. `unimported.ignore_extensions` so the command reports audio, not the hundred-odd orphaned `albumart.1.jpg` files from earlier fetchart runs; `fetchart.cover_names` gains `albumart` so `beet fetchart` adopts those files instead of them being deleted.
- **2026-09-14** A candidate's listing is linkable (0.7.0). The invariant said Hermes keeps no tracker URL at all; the passkey it exists to protect lives in a torrent's announce URL, and the database already held links that carry none -- Prowlarr's proxied `download_url` since 2026-09-07, and the indexer's own URL as `prowlarr_guid`. A human deciding whether to approve a grab wants to open the listing, so `Candidate.info_url` now stores Prowlarr's `infoUrl` and the UI links the candidate title to it (`target="_blank"`, `rel="noopener noreferrer"`, so the tracker gets no referrer). `prowlarr_guid` is not that link: Gazelle reuses the details URL as the GUID, but on PandaCD the GUID is a download action, and linking a title to it would start a download instead of opening a page. The link is rendered only for an `http(s)` value, so an indexer that reports none, or something like a magnet or `javascript:`, leaves the title as plain text. Nothing changes about the grab: the bytes still come from `download_url` inside `submit()`.
- **2026-09-19** Search queries built the way the field matches (0.9.7). Before extending the apostrophe rule, how Lidarr and Headphones identify releases was read and their exact query forms were measured on the tracker through the dev Prowlarr. Neither uses a third-party source: trackers carry no MusicBrainz ids, so both search by name, parse the indexer's title (Gazelle indexers synthesise it from the JSON artist/group/year fields; Prowlarr's API exposes only that title) and fuzzy-match, leaving the exact release to the files afterwards, as Hermes leaves it to beets. Lidarr sends one structured artist/album query per album (leading "The" stripped, curly apostrophes folded, other punctuation as separators, accents removed, disambiguation appended) with no retry, and matches after removing punctuation, accents and a/an/the/and/or/of. Its forms here: `Deserter's Songs` 0, `Deserter s Songs` 11, `Belle and Sebastian` 0, `Belle Sebastian` 15, `Sigur Ros Agaetis byrjun` (accents stripped) 0 where the accented spelling gave 26. Headphones replaces ` & ` with a space, keeps accents on the Gazelle path and uses Gazelle's release-type and format filters; it falls back across providers, not query forms, and leaves `and` alone. Slashes, hyphens, a leading ellipsis, exclamation marks and curly double quotes were fine either way. Decision: (1) the query is the token set the matcher ignores nothing of: all punctuation to spaces, the six noise words dropped, accents kept, the text kept whole if only noise words remain (`search_form`); (2) an empty answer first asks Prowlarr's indexer status, and a searched indexer disabled after failures makes the search `FAILED` (retryable) naming it and its return time, since Prowlarr answers a skipped indexer with the same empty list; (3) otherwise one artist-only search (limit 500, never for Various Artists) as its own event, matching doing the precision: no tool does this, and it would have found every case in this thread, including spellings nobody has listed. Not adopted: Lidarr's accent stripping (fails here), structured artist/album fields (tokenised the same, and Prowlarr's API ignores them), Gazelle's server-side format filters (ranking already records every row with a reason).
- **2026-09-19** Apostrophes out of the search query (0.9.6). 0.9.5 changed nothing on the tracker: Prowlarr 2.x already folds U+2019 to `'` in `SanitizedSearchTerm` before building the request, so the tracker had received `Deserter's Songs` all along, and "Search again" still found nothing. Measured instead of guessed, through the dev Prowlarr against the tracker's indexer (the tracker's own History showed 114 results for `Mercury Rev` and 0 for `Deserter's Songs`, so the indexer was fine and the title was the problem): 0 results for `Deserter's Songs`, `Deserter’s Songs` and `Deserters Songs`; all 11 editions for `Deserter Songs`, `Deserter s Songs` and `Mercury Rev Deserter Songs`; Prowlarr's structured `artist`/`album` search returned the artist's first 50 torrents and ignored the album. The tracker lists the title with U+2019, which its Sphinx index treats as a separator, so the indexed tokens are `deserter` and `s`; a straight `'` is a blend character there, so a straight-spelled title is indexed both whole and as pieces. A query made of the pieces therefore matches either spelling, and `search_form` now replaces apostrophes with spaces. Lesson for the log: an empty Prowlarr result is not evidence about the query until the indexer is shown to answer something else (0.9.7 checks the indexer status).
- **2026-09-19** Search queries in ASCII punctuation (0.9.5). *Deserter’s Songs* was `NO_MATCH` with zero results on a tracker that lists six editions of it: MusicBrainz spells the apostrophe U+2019 (its style guide) and the query carried it verbatim. Gazelle's `sphinx.conf` puts the straight apostrophe in `blend_chars`, so "Deserter's" is indexed both as one word and as two, but U+2019 is in no table and splits the word; the tracker's own users type the straight form. `text.search_form` folds typographic apostrophes, quotes, dashes, ellipses and no-break spaces to ASCII before the Prowlarr call (case, accents and the punctuation itself are kept: how the search treats those is the tracker's business). Matching is unaffected: `normalize` already drops all punctuation.
- **2026-09-17** Queue filters by who has the ball (0.9.4). The queue had one chip per non-terminal state with rows in it, so an approved album's chip migrated through SUBMITTED, DOWNLOADING, READY_FOR_BEETS and IMPORTING and the bar changed shape as the pipeline ran, which made the albums in progress the hardest ones to follow. The chips are now seven and stable: `needs you` (a person), `in flight` (Hermes, Deluge or beets: the states the page already reloaded for), `not found` (waiting on the re-search schedule), and origin. `FAILED` joins `needs you` and moves up the order: only a person can move it (retry, search again, cancel), yet it sat near the bottom of the queue, in no filter and outside the header badge. Per-state counts stay as a muted text line. Exact states are still accepted in the query string so a chip can come back if it is missed; the row's state pill was considered as that control and rejected as too close to the title to be a click target. After the first phone render: the row-number column is gone (it went stale every tick, looked like the acquisition id in notices, and the walk bar computes its own position); the state and origin chips are two wrapping groups with no separator; each state-group chip has its group colour as outline and tint when selected (a dot was tried and dropped: it cost width on the phone), a zero count is muted. The row's state pill keeps its own colour: the chip says whose turn it is, the pill says what happened.
