# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hermes turns ListenBrainz recommendations and manual album requests into album-level torrent
acquisitions (Prowlarr search, Deluge download) and hands finished downloads to beets.

- `docs/plan.md` is the working design: architecture decisions (section 3), domain model and
  state machine (4), per-stage pipeline behaviour (5), milestones and the current status
  note (11), open questions (12) and a decision log (13). Read the relevant section before
  changing behaviour; update the plan when a decision changes.
- `docs/conventions.md` holds the rules that are not visible from the code: the concurrency
  and safety lessons from the reviews, ranking and matching thresholds, UI conventions,
  fixture rules, and Windows/Docker Desktop notes. Read it before touching those areas.
- `README.md` is the public introduction: components, the pipeline, running and deploying.

## Commands

Two independent `uv` projects live here. Run each from its own directory.

Hermes service (repo root):

```
uv sync                          # create .venv and install (editable)
uv run pytest -q                 # all tests
uv run pytest tests/unit/test_state.py -q -k stalled   # one file / one test
uv run ruff check . && uv run ruff format --check .
uv run mypy hermes
uv run hermes validate-config    # load the policy YAML and print it
uv run hermes check              # same report as GET /healthz, exit 1 if unhealthy
uv run hermes serve              # applies migrations, then uvicorn on HERMES_PORT
uv run hermes request "Artist" "Album"   # manual request: resolve + library check, prints events
uv run hermes request --mbid <release-group-or-release-mbid>
uv run hermes parse-title "Artist - Album (1997) [Album] [FLAC Lossless / WEB]"
uv run hermes approve <id>       # human approval (respects dry_run)
uv run hermes prefer <id> <candidate-id>   # put a candidate in front; the next approval fetches it
uv run hermes observe            # one observer tick against Deluge
uv run hermes import-tick        # one importer tick against the beets agent
uv run hermes discover           # one discovery tick: new ListenBrainz playlists -> signals -> queue
uv run hermes ingest-playlist <mbid> [--mode ignore]   # one playlist by MBID, as if in extra_playlists
uv run hermes research           # one re-search tick (NO_MATCH rows past search.retry_days)
uv run hermes art                # one album-art tick against the Cover Art Archive
uv run hermes genres             # one genres tick: MusicBrainz genres for targets that have none
uv run hermes db revision "msg"  # autogenerate an Alembic migration after model changes
```

Migrations: `hermes db revision` autogenerates against the configured database, so point
`HERMES_DATABASE_URL` at a scratch SQLite file, run `hermes db upgrade` first, then
`revision`, or the diff will be the whole schema.

beets plugin (`beets-hermes/`): `uv sync`, then `uv run pytest -q`. It has its own venv and a
real beets install.

Local settings and secrets (tokens, API keys) come from `.env` (copy `.env.example`),
which is gitignored and denied to tooling in `.claude/settings.json`: **never read, print
or copy it.** The policy comes from `config.yaml` (copy `config.example.yaml`).
`HERMES_DRY_RUN` (env) overrides `policy.dry_run`; the compose default is `true`. Windows
and Docker Desktop quirks (SQLite URL form, `MSYS_NO_PATHCONV`, UDP announces) are in
`docs/conventions.md`.

Images: `docker build -t hermes:dev .` and
`docker build -f deploy/beets/Dockerfile -t beets-hermes:dev .` (both from the repo root);
`.github/workflows/images.yml` builds and pushes both on a push to `main`.

Development stack (beets agent, one Deluge, Prowlarr with the PandaCD public indexer,
Hermes with reload; `dry_run` on):

```
scripts/dev-init.sh              # creates dev/ dirs, seeds Deluge core.conf + Prowlarr config.xml, copies .env
docker compose up --build -d     # hermes :8000, beets agent :8338, deluge :8112 (pw "deluge"), prowlarr :9696
uv run python scripts/dev-prowlarr-setup.py                            # adds PandaCD, smoke search
docker compose exec beets beets-python /scripts/seed-dev-library.py   # CC albums, real MBIDs
curl -s localhost:8000/healthz
```

The dev library and tracker are Creative Commons on purpose: no personal data, no production
credentials, no cluster access needed to recreate the environment. PandaCD
(https://pandacd.io/) is a small community tracker for CC and artist-permitted music: keep
search volume modest. Seeded albums: Dirty Wings (FLAC, owned), Vendaface (24-bit FLAC,
owned), Stunt Island (MP3, owned_lossy); The Slip and Addressed to the Stars are the standing
"missing" examples and exist on PandaCD in FLAC. Prowlarr's dev API key is fixed in
`dev/prowlarr/config.xml.template`. Real Weekly Exploration recommendations will rarely be on
PandaCD; the automated path is exercised via `listenbrainz.extra_playlists` with a hand-built
CC playlist, and recommendation quality is only judged in the M6 live weeks. Develop against
this stack, not the cluster (docs/plan.md A12).

## Architecture

- `hermes/config.py`: `Policy` is the YAML document (strict, unknown keys rejected;
  `config.example.yaml` must always equal `Policy()` defaults, a test enforces it).
  `Settings` is env-only: URLs, secrets, DB path.
- `hermes/domain/models.py`: Signal → AlbumTarget → Acquisition → Candidate / GrabAttempt, plus
  Event (audit log) and Playlist (discovery's idempotence record). `hermes/domain/state.py`
  is the **only** code allowed to change `Acquisition.state`; every transition writes an
  Event. Add edges there, not ad hoc.
- `hermes/db/`: SQLAlchemy 2 engine (SQLite WAL) and Alembic migrations. A test diffs
  `upgrade head` against the models, so a model change without a migration fails CI.
- `hermes/integrations/`: one thin async httpx client per external system, each with
  `health()`. No business logic. Deluge is JSON-RPC over `/json` with a login cookie.
  `beets.py` is the only path to beets: typed library reads, import jobs and history
  marking, all served by the beets-hermes agent (reads retry on a 503 with `retry: true`,
  sent while an import holds the SQLite lock). `musicbrainz.py` has one shared 1 req/s
  limiter, an identifying User-Agent from `MUSICBRAINZ_CONTACT`, and retries the 503 "busy"
  reply, 502/504 and timeouts; its health check makes no network call. `listenbrainz.py`
  is public reads with an optional token, built when `listenbrainz.user` or
  `extra_playlists` is set. `navidrome.py` (Subsonic ping and startScan) is optional and
  not in the compose stack. `coverart.py` fetches a front cover from the Cover Art Archive
  (own limiter, same User-Agent, health never red: art is cosmetic); built when
  `policy.art.enabled`.
- `hermes/services/`: one module per pipeline stage, pure where possible. `text.py`
  (normalise + similarity), `resolution.py` (search results or one recording → one release
  group, or a needs-review candidate list; type policy from `policy.resolution`),
  `library_check.py` (release group → release → fuzzy, via the agent), `requests.py`
  (manual request wiring, dedupe onto an in-flight acquisition), `title_parser.py` (pure:
  Gazelle and PandaCD title families → `ParsedTitle`), `matching.py` (does a parsed title
  describe the target), `ranking.py` (policy filters with reasons, weighted rank, inferred
  sample rate), `search.py` (SEARCHING → CANDIDATES_READY | NO_MATCH, every Prowlarr row
  persisted as a Candidate; `search.indexers` narrows the query to named indexers), `approval.py` (the gate: `approval.timid` waits for a human on
  everything, otherwise manual requests self-approve and automated ones need a rule),
  `submit.py` (torrent via Prowlarr → `hermes.bencode` infohash → routed Deluge with
  per-torrent paths and label → GrabAttempt; no torrent bytes leave the function),
  `observer.py` (`tick()` polls Deluge for active attempts; also the startup reconcile),
  `discovery.py` (ListenBrainz playlists → Signals → acquisitions; `research_tick()` for
  retries), `importer.py` (READY_FOR_BEETS → IMPORTING → IMPORTED | IMPORT_NEEDS_REVIEW via
  the agent, release chosen from the download's shape), `pipeline.py` (search then
  decide), `art.py` (album art per target into `<data dir>/art`, active rows first, backoff
  on failure; the only files Hermes keeps), `genres.py` (MusicBrainz release-group
  genres on the target, stored with the lookup that creates it, backfilled by a job;
  the page rule: two on a row, three on the detail page, a general genre dropped for a
  specific one), `suggest.py` (the request form's suggestions: artists for typed text, then an
  artist's official albums, one at a time, one MusicBrainz attempt, cached),
  `context.py` (policy + clients + `art_dir`, what
  every stage receives).
- Observer, importer, discovery, re-search, art and genres run on an in-process APScheduler
  started in the app lifespan (`policy.deluge.poll_seconds`, `listenbrainz.poll_hours`,
  daily, five minutes, five minutes), each with one immediate run as the reconcile; a
  manual request also kicks the art job (`kick_art`).
- `hermes/app.py`: FastAPI factory; `create_app(settings, policy, clients)` takes injected
  clients so tests mock HTTP with respx. `/healthz` returns 503 when any configured
  dependency is unhealthy. Startup removes torrent files an older version kept.
- `hermes/api/`: JSON routes (`POST /api/requests`, `GET /api/acquisitions/{id}`,
  `GET /api/suggest/...`) with pydantic schemas. `hermes/ui/`: Jinja2 pages, plain forms, POST-redirect-GET; button
  availability derives from `can_transition`; `docs/ui-plan.md` is the page design.
- `beets-hermes/beetsplug/hermes.py`: a beets plugin adding `beet hermes-agent` (also the
  `hermes-agent` console script): `/library/...` reads built on beets' `Library` query
  objects (never query strings), a job runner that shells out to `beet import` (with an
  overlay config that clears `match.preferred`: the release id is given), and
  `POST /history` for beets' incremental history. Hermes talks to beets **only** through
  this agent; it never opens `library.db` and does not use the `web` plugin.

## Versions, images and compatibility

One version for the whole repo, in `pyproject.toml`, `beets-hermes/pyproject.toml` and
`hermes/__init__.py`; the images workflow fails if the two pyprojects disagree. Pre-1.0 it is
`0.<milestone>.<patch>`: bump the minor when a milestone completes or when a change adds a
config key, a migration or an agent route; bump the patch for fixes that need none of those;
`1.0.0` when M6's live weeks pass. A push to `main` builds `<namespace>/hermes` with tags
`latest`, `<version>` and `<version>-<sha>`. `<namespace>/beets-hermes` is beets plus the
plugin, so it is tagged with the beets release it carries (`<beets>`, `<beets>-<sha>`, read
from the built image) and only rebuilt when `beets-hermes/` or its Dockerfile changed; the
plugin's tests run inside the image before it is pushed. The base image is pinned by tag
and digest in `deploy/beets/Dockerfile` and bumped by Dependabot PRs. Cluster manifests
pin commit tags only (a reused version tag is served from the node cache). Compatibility
between Hermes and the plugin is the integer `AGENT_API` in
`beetsplug/hermes.py` against `REQUIRED_AGENT_API` in `hermes/integrations/beets.py`: bump
both when an agent route, body or response changes in a way an older Hermes would misread;
a mismatch turns the beets health check red and `start_import` waits, naming the fix.

## Cluster context

The home cluster is defined in a separate flux repo (its local path is in `CLAUDE.local.md`,
which is gitignored). **Read it for facts; never write to it.** Changes the cluster needs for
Hermes are drafted under `deploy/` in this repo for the user to port. Facts that shape the
code: Deluge is three separate instances (one per tracker plus one for the rest) selected per
tracker via `policy.deluge.instances`; Deluge sees `/downloads/complete/...` where beets sees
`/downloads/...`; beets runs the linuxserver image with the plugin, the agent is the pod's
only process, and Hermes reaches it only via that agent on 8338.

## Invariants that must survive any change

- beets owns the library: Hermes never writes under `/music` or touches `library.db`.
- Seeding is sacred: never delete, pause or modify torrents or their files. beets imports must
  copy, never move, link or retag in place (the agent refuses such configs).
- Nothing spends ratio without policy: `dry_run` defaults to true and `approval.timid`
  defaults to true, so by default a human approves every grab. Ratio and token limits are
  the tracker's numbers; blast radius is Prowlarr's per-indexer Grab Limit.
- Prowlarr is search-only. Hermes fetches the `.torrent` and adds it to Deluge itself.
- Hermes keeps no torrent file and no announce URL, on disk or in the database: a private
  tracker's torrent carries the account passkey in its announce URL. The bytes live only
  inside `submit()`; the download is tracked by infohash; what the importer needs from the
  file is `GrabAttempt.download_shape`. Deluge holds the file for seeding. What a candidate
  row may carry is a link that is not a credential: Prowlarr's proxied `download_url` (its
  own API key, on a service with no ingress) and `info_url`, the indexer's page for that
  listing, which the UI links the title to so a human can go and look at it.
- MusicBrainz calls go through one rate limiter (1 req/s) with an identifying User-Agent.
- The tree carries no tracker names, account names, site URLs or machine paths: fixtures
  are scrubbed, prose says "tracker A"/"tracker B", local facts live in gitignored files.

## Designed for sharing

Hermes is built against one home cluster but is meant to be shareable later, possibly with a
different torrent client, tracker, or music source. Keep that possible without building for it:

- Cluster facts (paths, URLs, instance names, tracker names) go in config, never in code.
  Write comments and docstrings as examples ("e.g. in a setup where...") rather than as facts
  about the design.
- Services take small Protocol-style interfaces (torrent client, release source, discovery
  source), not concrete clients. One implementation each until a second is needed.
- No plugin system, registries, or abstract base class hierarchies. A second implementation is
  a new file in `integrations/` chosen by a config key.
- `deploy/` and the default quality policy are allowed to be specific to this cluster and the
  user's taste.
