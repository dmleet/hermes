"""Deluge Web UI JSON-RPC client (``/json``). Session cookie is kept on the httpx client."""

from __future__ import annotations

import base64
import hashlib
import itertools
from typing import Any

import httpx

from hermes.bencode import decode, encode
from hermes.integrations import HealthResult

STATUS_KEYS = [
    "name",
    "state",
    "progress",
    "is_finished",
    "save_path",
    "move_completed_path",
    "move_completed",
    "total_done",
    "total_wanted",
    "num_seeds",
    "num_peers",
    "download_payload_rate",
    "label",
]


class DelugeError(Exception):
    pass


def _info_bytes(torrent: bytes) -> bytes:
    return encode(decode(torrent)[b"info"])


class DelugeClient:
    def __init__(
        self, base_url: str, password: str, *, name: str = "deluge", timeout: float = 30.0
    ) -> None:
        self.name = name
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._password = password
        self._ids = itertools.count(1)
        self._authenticated = False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, *params: Any) -> Any:
        payload = {"method": method, "params": list(params), "id": next(self._ids)}
        resp = await self._http.post("/json", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise DelugeError(f"{method}: {body['error']}")
        return body.get("result")

    async def login(self) -> None:
        if not await self._call("auth.login", self._password):
            raise DelugeError("auth.login rejected the password")
        self._authenticated = True
        if not await self._call("web.connected"):
            hosts = await self._call("web.get_hosts")
            if not hosts:
                raise DelugeError("web UI is not connected to a daemon and has no hosts")
            await self._call("web.connect", hosts[0][0])

    async def call(self, method: str, *params: Any) -> Any:
        if not self._authenticated:
            await self.login()
        try:
            return await self._call(method, *params)
        except DelugeError:
            # Session expired, or the daemon restarted under a still-valid web session:
            # log in (which also reconnects the web UI to a daemon) and try once more.
            self._authenticated = False
            await self.login()
            return await self._call(method, *params)

    # -- torrent operations -------------------------------------------------------

    async def add_torrent(self, filename: str, data: bytes, options: dict[str, Any]) -> str:
        """Add a .torrent; returns its infohash. If Deluge already has it, adopt it."""
        try:
            result = await self.call(
                "core.add_torrent_file", filename, base64.b64encode(data).decode(), options
            )
        except DelugeError as exc:
            if "already" not in str(exc).lower():
                raise
            return hashlib.sha1(_info_bytes(data)).hexdigest()
        if not result:
            raise DelugeError("core.add_torrent_file returned no torrent id")
        return str(result).lower()

    async def ensure_label(self, label: str) -> None:
        labels = await self.call("label.get_labels")
        if label not in (labels or []):
            await self.call("label.add", label)

    async def set_label(self, infohash: str, label: str) -> None:
        await self.call("label.set_torrent", infohash, label)

    async def find_at(self, locations: list[str]) -> dict[str, dict[str, Any]]:
        """Torrents whose download location is one of ``locations`` (Hermes gives every
        acquisition its own directory, so this attributes a torrent to an acquisition
        without a torrent file). Keyed by infohash."""
        found: dict[str, dict[str, Any]] = {}
        for location in locations:
            result = await self.call(
                "core.get_torrents_status",
                {"download_location": location},
                ["name", "download_location", "save_path", "total_wanted"],
            )
            for k, v in (result or {}).items():
                found[str(k).lower()] = v
        return found

    async def torrents_status(
        self, infohashes: list[str], keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not infohashes:
            return {}
        result = await self.call("core.get_torrents_status", {"id": infohashes}, keys)
        return {str(k).lower(): v for k, v in (result or {}).items()}

    async def health(self) -> HealthResult:
        try:
            await self.login()
            version = await self._call("daemon.get_version")
            plugins = await self._call("core.get_enabled_plugins")
        except (httpx.HTTPError, DelugeError) as exc:
            return HealthResult(self.name, ok=False, detail=str(exc))
        label_enabled = "Label" in (plugins or [])
        return HealthResult(
            self.name,
            ok=label_enabled,
            detail="ok" if label_enabled else "Label plugin is not enabled",
            data={"version": version, "plugins": plugins},
        )
