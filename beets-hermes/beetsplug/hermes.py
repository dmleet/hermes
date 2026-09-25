"""beets-hermes: the HTTP interface between Hermes and beets.

Reads answer library questions (is this release group owned, at what quality) through
beets' public ``Library`` query API. Writes run the ordinary ``beet import`` command as a
subprocess, one job at a time, and expose job status. Hermes never opens the library
database itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import beets
import confuse
from beets import config
from beets.dbcore import query as dbq
from beets.library import Album, Library
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand
from beets.util import displayable_path

# The HTTP contract between Hermes and this agent. Bump when a route, its request body or
# its response shape changes in a way an older Hermes would misread; Hermes refuses to
# import against an agent whose agent_api differs from the one it was built for.
AGENT_API = 1


def _plugin_version() -> str:
    try:
        from importlib.metadata import version

        return version("beets-hermes")
    except Exception:  # noqa: BLE001 - a source checkout without metadata
        return "0"


LOG_TAIL_LINES = 50
LOSSLESS_FORMATS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WavPack", "DSF"}
ACQUISITION_FIELD = "hermes_acquisition"


# -- library reads ----------------------------------------------------------------


def _field_query(field: str, value: str, *, exact: bool = True) -> dbq.FieldQuery:
    """Build a query object instead of a query string, so values with spaces, colons or
    slashes are matched literally. Flexible attributes must use the slow (Python) path."""
    fast = field in Album._fields
    cls = dbq.MatchQuery if exact else dbq.SubstringQuery
    return cls(field, value, fast=fast)


def quality_summary(album: Album) -> dict[str, Any]:
    items = list(album.items())
    formats = sorted({str(i.format) for i in items if i.format})
    bitdepths = [int(i.bitdepth) for i in items if i.bitdepth]
    samplerates = [int(i.samplerate) for i in items if i.samplerate]
    lossy = sum(1 for i in items if i.format and str(i.format) not in LOSSLESS_FORMATS)
    return {
        "items": len(items),
        "formats": formats,
        "min_bitdepth": min(bitdepths) if bitdepths else None,
        "min_samplerate": min(samplerates) if samplerates else None,
        "lossy_items": lossy,
    }


def _album_path(album: Album) -> str | None:
    try:
        return displayable_path(album.item_dir()) if album.id is not None else None
    except ValueError:  # an album row with no items
        return None


def album_to_dict(album: Album) -> dict[str, Any]:
    return {
        "id": album.id,
        "mb_albumid": album.mb_albumid or None,
        "mb_releasegroupid": album.mb_releasegroupid or None,
        "albumartist": album.albumartist,
        "album": album.album,
        "year": album.year or None,
        "original_year": album.original_year or None,
        "path": _album_path(album),
        "added": album.added,
        ACQUISITION_FIELD: album.get(ACQUISITION_FIELD),
        "quality": quality_summary(album),
    }


def _music_dir(lib: Library) -> AbstractContextManager[Any]:
    """beets 2.14 stores item paths relative to the library directory and resolves them
    through a context variable that `Library()` binds on the thread that opened it. The
    agent answers on worker threads, which start without it, and would hand Hermes the
    stored relative path ("Tool/Fear Inoculum"), which Hermes then reads as outside the
    library. Bind it for the duration of each read. Older beets have no such helper and
    store absolute paths, so nothing is needed there."""
    bind = getattr(lib, "music_dir_context", None)
    return bind() if bind is not None else contextlib.nullcontext()


class LibraryReader:
    """Read-only questions Hermes asks about the library."""

    def __init__(self, lib: Library) -> None:
        self._lib = lib

    def _albums(self, query: dbq.Query) -> list[dict[str, Any]]:
        with _music_dir(self._lib):
            return [album_to_dict(a) for a in self._lib.albums(query)]

    def release_group(self, mbid: str) -> list[dict[str, Any]]:
        return self._albums(_field_query("mb_releasegroupid", mbid))

    def release(self, mbid: str) -> list[dict[str, Any]]:
        return self._albums(_field_query("mb_albumid", mbid))

    def acquisition(self, acquisition_id: str) -> list[dict[str, Any]]:
        return self._albums(_field_query(ACQUISITION_FIELD, acquisition_id))

    def search(self, artist: str | None, album: str | None) -> list[dict[str, Any]]:
        parts: list[dbq.Query] = []
        if artist:
            parts.append(_field_query("albumartist", artist, exact=False))
        if album:
            parts.append(_field_query("album", album, exact=False))
        if not parts:
            return []
        return self._albums(dbq.AndQuery(parts))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flag(name: str, problems: list[str]) -> bool:
    """Read import.<name> as a bool. A value confuse cannot read as a bool is reported as a
    problem rather than crashing the health check."""
    try:
        return bool(config["import"][name].get(bool))
    except confuse.NotFoundError:
        return False
    except confuse.ConfigError as exc:
        problems.append(f"import.{name} is unreadable: {exc}")
        return False


def config_problems() -> list[str]:
    """Reasons the current beets config would alter or remove the seeded files on import."""
    problems: list[str] = []
    if not _flag("copy", problems):
        problems.append(
            "import.copy is false: beets would import in place and retag the seeded files"
        )
    if _flag("move", problems):
        problems.append("import.move is true: beets would move files out of the seeding location")
    if _flag("link", problems):
        problems.append("import.link is true: symlinked imports are retagged in place")
    if _flag("hardlink", problems):
        problems.append("import.hardlink is true: hardlinked imports share the seeded inode")
    if _flag("delete", problems):
        problems.append("import.delete is true: beets would delete the seeded files after copying")
    return problems


# Passed to every Hermes import with `beet -c`, on top of the user's config. A Hermes
# import carries a release id chosen from the files (track count, disc layout, medium),
# so there is one candidate and `match.preferred` cannot order anything; it can only
# lower that candidate's score. A correct CD from a country not in the list scored
# "medium" on those two penalties alone and quiet mode skipped it. The same goes for the
# fields read from the uploader's tags that describe the edition or the packaging (album
# title, disc count, each file's disc number, year, label, catalogue number, country,
# media, disambiguation, and the MusicBrainz album and track ids): they say how the
# files were tagged, not what is on them, and `from_scratch` discards them. A 2-CD set
# tagged as two albums, "... (CD 1)" and "... (CD 2)", every file disc 1 of 1, scored
# 90.6% on its correct release, mostly on the 17 second-disc files' disc number; a CD
# tagged with another country's release id of the same tracks scored 82.1% on the id
# alone. Against a given release an id can only cost: a match adds nothing. What still
# decides the match: the artist, every track's title, length and index (per disc or per
# release), and `max_rec` on missing or unmatched tracks. Manual imports do not use this
# file and keep the preferences and weights, which is where they order candidates.
IMPORT_OVERLAY = """\
# Written by hermes-agent at start; do not edit. Hermes imports run with `beet -c` this
# file: the release id is already chosen, so candidate preferences only cost confidence,
# and edition, disc and id tags describe the uploader's tagging, not the audio.
match:
  preferred:
    media: []
    countries: []
  distance_weights:
    album: 0.0
    mediums: 0.0
    medium: 0.0
    year: 0.0
    label: 0.0
    catalognum: 0.0
    country: 0.0
    media: 0.0
    albumdisambig: 0.0
    album_id: 0.0
    track_id: 0.0
"""


class JobStore:
    """Runs import jobs sequentially on one worker thread; persists outcomes to jobs.json."""

    def __init__(self, jobs_dir: Path, beet_command: list[str], log: Any = None) -> None:
        self.jobs_dir = jobs_dir
        self.beet_command = beet_command
        self._log = log
        self._jobs: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self.busy = False
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.overlay = self.jobs_dir / "import-overlay.yaml"
        self.overlay.write_text(IMPORT_OVERLAY, encoding="utf-8")
        self._load()
        threading.Thread(target=self._worker, name="hermes-import-worker", daemon=True).start()

    # -- persistence -------------------------------------------------------
    @property
    def _state_path(self) -> Path:
        return self.jobs_dir / "jobs.json"

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            records = json.loads(self._state_path.read_text())
        except ValueError:
            # A crash mid-write can leave a truncated file; keep it for inspection and go on.
            self._state_path.replace(self._state_path.with_suffix(".corrupt"))
            if self._log:
                self._log.warning("hermes-agent: jobs.json was unreadable; moved aside")
            return
        for job in records:
            if job["status"] != "finished":
                job.update(
                    status="finished", finished_at=_now(), error="interrupted by agent restart"
                )
            self._jobs[job["job_id"]] = job
            self._order.appendleft(job["job_id"])

    def _save(self) -> None:
        with self._lock:
            records = [self._jobs[j] for j in reversed(self._order)]
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(records, indent=1))
            os.replace(tmp, self._state_path)  # atomic: never a half-written jobs.json

    def wait_idle(self, timeout: float) -> bool:
        """Block until no job is running (used on shutdown). True if idle."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.busy and self._queue.empty():
                return True
            time.sleep(0.2)
        return not self.busy

    # -- public API ----------------------------------------------------------
    def submit(self, path: str, acquisition_id: str, search_id: str | None) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "acquisition_id": acquisition_id,
            "path": path,
            "search_id": search_id,
            "status": "queued",
            "exit_code": None,
            "error": None,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "log_path": str(self.jobs_dir / f"{job_id}.out.log"),
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.appendleft(job_id)
        self._save()
        self._queue.put(job_id)
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(self._jobs[j]) for j in self._order]

    def _view(self, job: dict[str, Any]) -> dict[str, Any]:
        view = dict(job)
        view["log_tail"] = self._tail(job["log_path"])
        # The -l log is where beets records skips and as-is imports; the reason a quiet
        # import did nothing is there, not on stdout.
        view["import_log_tail"] = self._tail(str(self.jobs_dir / f"{job['job_id']}.import.log"))
        return view

    @staticmethod
    def _tail(path: str) -> list[str]:
        try:
            return Path(path).read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
        except OSError:
            return []

    # -- worker --------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            self.busy = True
            try:
                self._run(job_id)
            finally:
                self.busy = False
                self._queue.task_done()

    def _command(self, job: dict[str, Any]) -> list[str]:
        # -I (noincremental): the user's config has `incremental: yes`, which would make a
        # retried import of the same path a silent no-op.
        cmd = [
            *self.beet_command,
            "-c",
            str(self.overlay),
            "import",
            "-q",
            "-I",
            "--set",
            f"hermes_acquisition={job['acquisition_id']}",
        ]
        if job["search_id"]:
            cmd += ["--search-id", job["search_id"]]
        # "--" so a path can never be read as an option, whatever it starts with.
        cmd += ["-l", str(self.jobs_dir / f"{job['job_id']}.import.log"), "--", job["path"]]
        return cmd

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(status="running", started_at=_now())
        self._save()
        cmd = self._command(job)
        if self._log:
            self._log.info("hermes job {} starting: {}", job_id, " ".join(cmd))
        try:
            with open(job["log_path"], "w", encoding="utf-8") as out:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,  # a prompt (resume: ask) must fail, not hang
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            exit_code, error = result.returncode, None
        except OSError as exc:
            exit_code, error = None, f"could not start beets: {exc}"
        with self._lock:
            job.update(status="finished", exit_code=exit_code, error=error, finished_at=_now())
        self._save()
        if self._log:
            self._log.info("hermes job {} finished with exit code {}", job_id, exit_code)


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentServer

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        store = self.server.store
        url = urlsplit(self.path)
        path, params = url.path, parse_qs(url.query)
        if path == "/healthz":
            problems = config_problems()
            self._json(
                200,
                {
                    "ok": not problems,
                    "beets_version": beets.__version__,
                    "agent_api": AGENT_API,
                    "agent_version": _plugin_version(),
                    "config_ok": not problems,
                    "config_problems": problems,
                    "worker_busy": store.busy,
                },
            )
        elif path == "/jobs":
            self._json(200, store.list())
        elif path.startswith("/jobs/"):
            job = store.get(path[len("/jobs/") :])
            self._json(200, job) if job else self._json(404, {"error": "unknown job"})
        elif path.startswith("/library/"):
            self._library(path[len("/library/") :], params)
        else:
            self._json(404, {"error": "not found"})

    def _library(self, route: str, params: dict[str, list[str]]) -> None:
        reader = self.server.reader
        if reader is None:
            return self._json(503, {"error": "library not available", "retry": True})
        kind, _, arg = route.partition("/")
        try:
            if kind == "release-group" and arg:
                albums = reader.release_group(arg)
            elif kind == "release" and arg:
                albums = reader.release(arg)
            elif kind == "acquisition" and arg:
                albums = reader.acquisition(arg)
            elif kind == "search" and not arg:
                artist = (params.get("artist") or [None])[0]
                album = (params.get("album") or [None])[0]
                if not artist and not album:
                    return self._json(400, {"error": "search needs artist and/or album"})
                albums = reader.search(artist, album)
            else:
                return self._json(404, {"error": "not found"})
        except sqlite3.OperationalError as exc:
            # Typically "database is locked" while an import commits; Hermes retries.
            return self._json(
                503, {"error": str(exc), "retry": True, "worker_busy": self.server.store.busy}
            )
        return self._json(200, {"albums": albums})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/history":
            return self._mark_history()
        if self.path != "/import":
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}"))
            path, acquisition_id = str(body["path"]), str(body["acquisition_id"])
            search_id = body.get("search_id") or None
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": f"bad request: {exc}"})
        problems = config_problems()
        if problems:
            return self._json(
                409, {"error": "beets import config unsafe", "config_problems": problems}
            )
        if not Path(path).exists():
            return self._json(404, {"error": f"path does not exist: {path}"})
        return self._json(
            202, {"job_id": self.server.store.submit(path, acquisition_id, search_id)}
        )

    def _mark_history(self) -> None:
        """Record an imported folder in beets' incremental history (the state file's
        ``taghistory``), so a later manual ``beet import`` over the downloads directory
        skips it instead of stopping at a duplicate prompt. Hermes imports run with ``-I``
        and so never record themselves; Hermes calls this once an import is verified.
        The path tuples come from beets' own directory grouping, so they are the keys a
        manual run would compute, multi-disc folders included."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}"))
            path = str(body["path"])
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": f"bad request: {exc}"})
        if not Path(path).is_dir():
            return self._json(404, {"error": f"path is not a directory: {path}"})
        recorded = record_in_history(path)
        return self._json(200, {"recorded": len(recorded), "paths": recorded})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # http.server passes printf-style args; the beets logger formats with str.format.
        if self.server.log:
            self.server.log.debug("hermes-agent {}", format % args)


def record_in_history(path: str) -> list[list[str]]:
    """Add every album directory group under ``path`` to beets' incremental history.
    Idempotent: the history is a set. Returns the recorded groups as displayable paths."""
    from beets.importer.state import ImportState
    from beets.importer.tasks import albums_in_dir
    from beets.util import bytestring_path

    groups = [list(paths) for paths, items in albums_in_dir(bytestring_path(path)) if items]
    with ImportState() as state:
        for paths in groups:
            state.history_add(paths)
    return [[displayable_path(p) for p in paths] for paths in groups]


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: JobStore,
        log: Any = None,
        lib: Library | None = None,
    ) -> None:
        super().__init__(address, AgentHandler)
        self.store = store
        self.log = log
        self.reader = LibraryReader(lib) if lib is not None else None


class HermesPlugin(BeetsPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.config.add(
            {
                "beet_command": [sys.executable, "-m", "beets"],
                "jobs_dir": str(Path(config.config_dir()) / "hermes-jobs"),
            }
        )

    def commands(self) -> list[Subcommand]:
        cmd = Subcommand("hermes-agent", help="serve the Hermes library/import agent over HTTP")
        cmd.parser.add_option("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
        cmd.parser.add_option("--port", type="int", default=8338, help="bind port (default 8338)")
        cmd.func = self._serve
        return [cmd]

    def _serve(self, lib: Library, opts: Any, args: list[str]) -> None:
        store = JobStore(
            Path(self.config["jobs_dir"].as_str()),
            self.config["beet_command"].as_str_seq(),
            self._log,
        )
        server = AgentServer((opts.host, opts.port), store, self._log, lib=lib)
        self._log.info("hermes-agent listening on {}:{}", opts.host, opts.port)
        for problem in config_problems():
            self._log.warning("hermes-agent will refuse imports: {}", problem)

        # As PID 1 in a container the agent gets SIGTERM on pod shutdown. Stop accepting
        # work, then give a running `beet import` most of the grace period to finish so the
        # library is not left pointing at half-copied files.
        def _terminate(signum: int, _frame: Any) -> None:
            self._log.info("hermes-agent: signal {}, shutting down", signum)
            threading.Thread(target=server.shutdown, daemon=True).start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _terminate)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            if not store.wait_idle(timeout=25.0):
                self._log.warning("hermes-agent: exiting with an import still running")


def main(argv: list[str] | None = None) -> None:
    """Console entry point ``hermes-agent``: equivalent to ``beet hermes-agent ...``.

    Installed next to ``beet`` by pip, so an image only has to put it on PATH; the
    manifest never needs to know where the interpreter lives.
    """
    from beets import ui

    ui.main(["hermes-agent", *(sys.argv[1:] if argv is None else argv)])
