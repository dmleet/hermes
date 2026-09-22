"""Navidrome via the Subsonic API: health ping and a library scan trigger after imports."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import httpx

from hermes import __version__
from hermes.integrations import HealthResult


class NavidromeError(Exception):
    pass


class NavidromeClient:
    name = "navidrome"

    def __init__(self, base_url: str, user: str, password: str, *, timeout: float = 30.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._user = user
        self._password = password

    async def aclose(self) -> None:
        await self._http.aclose()

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()  # noqa: S324 - Subsonic spec
        return {
            "u": self._user,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": f"hermes/{__version__}",
            "f": "json",
        }

    async def _call(self, endpoint: str, **params: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(f"/rest/{endpoint}", params={**self._auth(), **params})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # str(exc) would include the URL and with it the auth token and salt.
            raise NavidromeError(f"{endpoint}: HTTP {exc.response.status_code}") from None
        except httpx.RequestError as exc:
            raise NavidromeError(f"{endpoint}: {type(exc).__name__}") from None
        body = resp.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise NavidromeError(f"{endpoint}: {err.get('message', 'unknown error')}")
        return dict(body)

    async def health(self) -> HealthResult:
        try:
            body = await self._call("ping")
        except (httpx.HTTPError, NavidromeError) as exc:
            return HealthResult(self.name, ok=False, detail=str(exc))
        return HealthResult(
            self.name,
            ok=True,
            detail="ok",
            data={"server_version": body.get("serverVersion"), "type": body.get("type")},
        )

    async def start_scan(self) -> dict[str, Any]:
        body = await self._call("startScan")
        return dict(body.get("scanStatus", {}))

    async def scan_status(self) -> dict[str, Any]:
        """``{"scanning": bool, ...}``: whether a scan (ours, the nightly one, a manual one) is
        walking the library right now."""
        body = await self._call("getScanStatus")
        return dict(body.get("scanStatus", {}))
