"""Test doubles that stand in for network services behind respx."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import respx

from hermes.bencode import decode, encode


class FakeDeluge:
    """Enough of the Deluge Web JSON-RPC surface for Hermes: login, plugins, add, labels,
    and a status table tests mutate to simulate progress."""

    def __init__(self, router: respx.Router, url: str, *, plugins: list[str] | None = None) -> None:
        self.torrents: dict[str, dict[str, Any]] = {}
        self.labels: set[str] = set()
        self.added: list[dict[str, Any]] = []
        self.plugins = plugins if plugins is not None else ["Label"]
        self.hidden: set[str] = set()
        self.fail_add: str | None = None
        router.post(f"{url}/json").mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        try:
            result = self._dispatch(method, params)
        except Exception as exc:  # noqa: BLE001 - mimic Deluge's error envelope
            return httpx.Response(
                200, json={"id": body["id"], "result": None, "error": {"message": str(exc)}}
            )
        return httpx.Response(200, json={"id": body["id"], "result": result, "error": None})

    def _dispatch(self, method: str, params: list[Any]) -> Any:
        if method == "auth.login":
            return params[0] == "pw"
        if method == "web.connected":
            return True
        if method == "daemon.get_version":
            return "2.2.0"
        if method == "core.get_enabled_plugins":
            return self.plugins
        if method == "label.get_labels":
            return sorted(self.labels)
        if method == "label.add":
            self.labels.add(params[0])
            return None
        if method == "label.set_torrent":
            self.torrents[params[0]]["label"] = params[1]
            return None
        if method == "core.add_torrent_file":
            if self.fail_add:
                raise RuntimeError(self.fail_add)
            import base64

            data = base64.b64decode(params[1])
            meta = decode(data)
            infohash = hashlib.sha1(encode(meta[b"info"])).hexdigest()
            if infohash in self.torrents:
                raise RuntimeError(f"Torrent already in session ({infohash}).")
            info = meta[b"info"]
            total = sum(f[b"length"] for f in info.get(b"files", [])) or info.get(b"length", 0)
            options = params[2]
            self.torrents[infohash] = {
                "name": info[b"name"].decode(),
                "state": "Downloading",
                "progress": 0.0,
                "is_finished": False,
                "save_path": options["download_location"],
                "move_completed_path": options.get("move_completed_path"),
                "move_completed": options.get("move_completed", False),
                "total_done": 0,
                "total_wanted": total,
                "num_seeds": 1,
                "num_peers": 0,
                "download_payload_rate": 0,
                "label": "",
            }
            self.added.append({"hash": infohash, "options": options, "filename": params[0]})
            return infohash
        if method == "core.get_torrents_status":
            filters = dict(params[0]) if isinstance(params[0], dict) else {}
            ids = filters.pop("id", None)
            keys = params[1]

            def value(t: dict[str, Any], k: str) -> Any:
                return t.get("save_path") if k == "download_location" else t.get(k)

            out = {}
            for h, t in self.torrents.items():
                if h in self.hidden or (ids is not None and h not in ids):
                    continue
                if any(value(t, k) != v for k, v in filters.items()):
                    continue
                out[h] = {k: value(t, k) for k in keys}
            return out
        raise RuntimeError(f"unknown method {method}")

    # -- helpers for tests ----------------------------------------------------
    def progress(self, infohash: str, done: int) -> None:
        t = self.torrents[infohash]
        t["total_done"] = done
        t["progress"] = 100.0 * done / max(t["total_wanted"], 1)

    def finish(self, infohash: str, *, moved: bool = True) -> None:
        t = self.torrents[infohash]
        t.update(total_done=t["total_wanted"], progress=100.0, is_finished=True, state="Seeding")
        if moved:
            t["save_path"] = t["move_completed_path"]

    def error(self, infohash: str) -> None:
        self.torrents[infohash]["state"] = "Error"

    def remove(self, infohash: str) -> None:
        del self.torrents[infohash]

    def hide(self, infohash: str, hidden: bool = True) -> None:
        """Leave the torrent out of status replies (a daemon still loading its session)."""
        (self.hidden.add if hidden else self.hidden.discard)(infohash)

    def queue(self, infohash: str) -> None:
        """Deluge holds the torrent back (its active-download limit): no bytes, Queued."""
        self.torrents[infohash]["state"] = "Queued"


class FakeBeetsAgent:
    """The beets-hermes agent: health, import jobs, and library reads keyed by acquisition."""

    def __init__(self, router: respx.Router, url: str) -> None:
        self.config_problems: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.library: dict[str, list[dict[str, Any]]] = {}  # acquisition id -> albums
        self.missing_paths: set[str] = set()
        self.history: list[str] = []  # folders recorded in beets' incremental history
        self.agent_api = 2
        self.history_down = False
        self.next_job = 1
        router.get(f"{url}/healthz").mock(side_effect=self._health)
        router.post(f"{url}/import").mock(side_effect=self._import)
        router.post(f"{url}/history").mock(side_effect=self._history)
        router.get(url__regex=rf"{url}/jobs/(?P<job>[^/]+)$").mock(side_effect=self._job)
        router.get(url__regex=rf"{url}/library/acquisition/(?P<acq>[^/]+)$").mock(
            side_effect=self._acquisition
        )
        router.get(url__regex=rf"{url}/library/.*").respond(json={"albums": []})

    def _health(self, request: httpx.Request) -> httpx.Response:
        ok = not self.config_problems
        return httpx.Response(
            200,
            json={
                "ok": ok,
                "beets_version": "2.5.1",
                "agent_api": self.agent_api,
                "agent_version": "0.5.0",
                "config_ok": ok,
                "config_problems": self.config_problems,
                "worker_busy": False,
            },
        )

    def _import(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.config_problems:
            return httpx.Response(
                409, json={"error": "unsafe", "config_problems": self.config_problems}
            )
        if body["path"] in self.missing_paths:
            return httpx.Response(404, json={"error": f"path does not exist: {body['path']}"})
        job_id = f"job{self.next_job}"
        self.next_job += 1
        self.jobs[job_id] = {
            "job_id": job_id,
            "acquisition_id": body["acquisition_id"],
            "path": body["path"],
            "search_id": body.get("search_id"),
            "release_group_id": body.get("release_group_id"),
            "status": "queued",
            "refused": False,
            "exit_code": None,
            "error": None,
            "log_tail": [],
        }
        self.submitted.append(body)
        return httpx.Response(202, json={"job_id": job_id})

    def _history(self, request: httpx.Request) -> httpx.Response:
        if self.history_down:
            return httpx.Response(503, json={"error": "state file busy"})
        path = json.loads(request.content)["path"]
        self.history.append(path)
        return httpx.Response(200, json={"recorded": 1, "paths": [[path]]})

    def _job(self, request: httpx.Request, job: str) -> httpx.Response:
        if job not in self.jobs:
            return httpx.Response(404, json={"error": "unknown job"})
        return httpx.Response(200, json=self.jobs[job])

    def _acquisition(self, request: httpx.Request, acq: str) -> httpx.Response:
        return httpx.Response(200, json={"albums": self.library.get(acq, [])})

    # -- helpers for tests ----------------------------------------------------
    def finish(
        self, job_id: str, *, exit_code: int = 0, imported_album: dict[str, Any] | None = None
    ) -> None:
        job = self.jobs[job_id]
        job.update(status="finished", exit_code=exit_code, log_tail=["import log line"])
        if imported_album is not None:
            self.library.setdefault(job["acquisition_id"], []).append(
                {**imported_album, "hermes_acquisition": job["acquisition_id"]}
            )

    def start(self, job_id: str) -> None:
        """The agent's worker picked the job up (it was queued until now)."""
        from datetime import UTC, datetime

        self.jobs[job_id].update(
            status="running", started_at=datetime.now(UTC).isoformat(timespec="seconds")
        )

    def refuse(self, job_id: str, reason: str) -> None:
        """The agent's release-group check refused the job before beets ran."""
        self.jobs[job_id].update(status="finished", refused=True, error=reason)
