# Hermes

Hermes is the messenger app for your music stack. It reads the playlists
ListenBrainz generates for you (Weekly Exploration by default) and album requests you type
in, works out which album each track belongs to, checks whether beets already has it,
searches your trackers through Prowlarr, ranks what it finds against a quality policy, and,
once a human or a rule approves, hands the torrent to Deluge and the finished download to
beets. Every step is recorded on the acquisition so you can see why it did what it did.

## Components

- **Hermes** (`hermes/`): the service. A FastAPI app with a SQLite database, an in-process
  scheduler for the discovery, observer, re-search and import jobs, a web UI for the queue
  and each acquisition, and a small JSON API. It holds the state machine and the audit log.
- **beets-hermes** (`beets-hermes/`): a beets plugin that runs an HTTP agent inside the
  beets container. Hermes reaches beets only through it: library lookups, `beet import`
  jobs with your own beets config, and marking imported folders in beets' incremental
  history. Hermes never opens beets' database or writes under the music directory.
- **ListenBrainz**: the discovery source. Hermes reads the playlists generated for your
  user (Weekly Exploration by default) plus any playlist MBIDs you list; a token is optional.
- **MusicBrainz**: resolution. Each recording or typed request becomes one release group,
  and the importer picks the release whose track count and disc layout match the download;
  its release-group genres are what the queue shows beside each album. One rate limiter,
  one identifying User-Agent.
- **beets**: owns the library. Imports copy files; the agent refuses a config that would
  move, link or retag in place, so seeding is never disturbed.
- **Prowlarr**: search only. Hermes fetches the `.torrent` itself, computes the infohash
  locally, and never writes the torrent bytes to disk or the database, because on a private
  tracker they carry your passkey.
- **Deluge**: the torrent client, one or more instances routed per indexer with per-torrent
  download paths and a label. Hermes adds torrents and polls them; it never pauses, removes
  or modifies one.
- **Navidrome** (optional): pinged, and asked to rescan once a batch of imports has
  finished; imports wait while it is scanning, so a scan never reads a half-written album.
- **Development stack** (`docker-compose.yaml`, `dev/`): beets with the plugin, one Deluge,
  and Prowlarr with PandaCD, a Creative Commons tracker, so the whole path runs locally on
  legal downloads with no production credentials.

## How an album moves

```
signal (ListenBrainz track or manual request)
  -> resolve      MusicBrainz recording -> release group (Album > EP > Single, artist credit must match)
  -> library      beets: owned, owned lossy, or missing
  -> search       Prowlarr, every enabled indexer; every result kept as a candidate with a reason
  -> rank         parse the tracker title; policy filters; weighted score; sample rate inferred from
                  size and playing time
  -> approve      a human (timid) or an auto-approve rule; dry run records what would happen
  -> submit       fetch through Prowlarr, add to the Deluge instance routed for that indexer
  -> observe      poll Deluge until the download finishes and moves; fall back to the next candidate
                  if it stalls
  -> import       the agent runs beet import with the release Hermes chose from the files' layout;
                  verified by asking the library, never by the exit code
```

States and the transitions between them live in one module, `hermes/domain/state.py`, and
every transition writes an event. The web UI is a queue and a per-acquisition page built
from those events, with the mode badges (dry run or live, timid or auto-approve) on every
page.

## Running it locally

Two `uv` projects: the Hermes service at the repo root and the beets plugin under
`beets-hermes/`. A Docker Compose stack provides beets with the plugin, one Deluge, and
Prowlarr with PandaCD, a Creative Commons tracker, so the whole path runs on legal
downloads with no production credentials.

```
uv sync
scripts/dev-init.sh                 # seeds Deluge and Prowlarr config, copies .env
docker compose up --build -d        # hermes :8000, beets agent :8338, deluge :8112, prowlarr :9696
uv run python scripts/dev-prowlarr-setup.py
docker compose exec beets beets-python /scripts/seed-dev-library.py
curl -s localhost:8000/healthz
```

Then open `http://localhost:8000`, request an album, and follow it. `uv run pytest -q`
runs the Hermes suite; the plugin has its own under `beets-hermes/`. `CLAUDE.md` lists
every command; `docs/conventions.md` the rules the code follows.

URLs, paths and secrets such as the optional ListenBrainz token come from `.env` (copy
`.env.example`), which is gitignored and never opened by tooling. The quality and approval
policy is `config.yaml`
(copy `config.example.yaml`; every key is documented there and unknown keys are rejected).

## Deploying it

`deploy/` holds the Kubernetes manifests: `hermes.yaml` for the service and, under
`deploy/beets/`, the image that adds the plugin to the linuxserver beets image, a
StatefulSet patch that runs the agent as the pod's process, a Service, and a production
beets config with each Hermes change and library-quality suggestion marked. The GitHub
Actions workflow builds `<namespace>/hermes` on every push to `main`, tagged `latest`,
`<version>` and `<version>-<sha>`, and `<namespace>/beets-hermes` whenever the plugin or its
Dockerfile changed, tagged with the beets release inside it (`2.14.0`, `2.14.0-<sha>`) after
the plugin's tests have passed inside that image. Cluster manifests pin the commit tags.
Dependabot proposes new linuxserver beets releases. The agent reports an API version that
Hermes checks at startup.

## Status

Milestones 0 to 5 are done and verified live in the compose stack: manual requests,
search and ranking against real tracker data, approval, download, import, and ListenBrainz
discovery running the user's real Weekly Exploration in dry run. Next is M6, two live weeks
against the production trackers. `docs/plan.md` section 11 carries the current status note.

## License

MIT, for Hermes and the beets-hermes plugin alike; see `LICENSE`. The captured test fixtures
are MusicBrainz and ListenBrainz data (CC0), a public-domain LibriVox recording's torrent
metadata, and factual release titles.

## Layout

```
hermes/            the service: config, domain model and state machine, integrations (one thin
                   client per external system), services (one module per pipeline stage), API, UI
beets-hermes/      the beets plugin and agent (its own uv project and tests)
deploy/            Kubernetes manifests and the beets image
dev/               the compose stack's config templates and (gitignored) data
scripts/           dev stack setup and fixture builders
tests/             Hermes tests; fixtures are captured real responses
docs/              plan.md (design, milestones, open questions, decision log) and
                   conventions.md (review lessons, thresholds, UI and fixture rules)
CLAUDE.md          commands, architecture map, invariants
```
