# beets-hermes

A beets plugin that runs `beet import` jobs on request over HTTP, so Hermes can hand
completed downloads to beets without touching `library.db` or the beets pod's filesystem.

**Install** into the beets image: `pip install /path/to/beets-hermes` (or `pip install .`).

**Enable** in the beets config: `plugins: [web, hermes]`.

**Run**: `beet hermes-agent [--host 0.0.0.0] [--port 8338]`, or the equivalent console script
`hermes-agent [...]` that pip installs next to `beet`. Set `BEETSDIR` (or run from the same
environment as your manual `beet` use) so the agent and its subprocesses see the same config.

**Endpoints** (JSON):
- `GET /healthz` → `ok`, `beets_version`, `config_ok`, `config_problems`, `worker_busy`. Refuses imports if `import.copy` is off or `import.move`/`link`/`hardlink`/`delete` is on (those would alter or remove seeded files).
- `POST /import` `{"path", "acquisition_id", "search_id"?}` → `202 {"job_id"}`; `409` on bad config, `404` if path missing.
- `GET /jobs/<id>`, `GET /jobs` → job records with `status` (queued|running|finished), `exit_code`, `error`, `log_tail` (stdout/stderr) and `import_log_tail` (beets' `-l` log: skips and as-is imports).

**Config keys** under `hermes:` — `beet_command` (list, default `[<python>, -m, beets]`) and
`jobs_dir` (default `<beets config dir>/hermes-jobs`, holds per-job logs and `jobs.json`).
Jobs run one at a time as `beet import -q -I --set hermes_acquisition=<id> [--search-id <mbid>] -l <log> <path>`
(`-I` bypasses `incremental: yes` so a retried path is not silently skipped).
