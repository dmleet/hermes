"""Client for the ``beets-hermes`` agent: the only way Hermes talks to beets.

Reads answer "what does the library own" questions; writes queue ``beet import`` jobs.
See beets-hermes/ for the server side and docs/plan.md A3/A3b for the contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from hermes.integrations import HealthResult

LOSSLESS_FORMATS = frozenset({"FLAC", "ALAC", "WAV", "AIFF", "APE", "WavPack", "DSF"})


class QualitySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: int
    formats: list[str]
    min_bitdepth: int | None = None
    min_samplerate: int | None = None
    lossy_items: int = 0

    @property
    def lossless(self) -> bool:
        return self.items > 0 and self.lossy_items == 0


class LibraryAlbum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    mb_albumid: str | None = None
    mb_releasegroupid: str | None = None
    albumartist: str
    album: str
    year: int | None = None
    original_year: int | None = None
    path: str | None = None
    hermes_acquisition: str | None = None
    quality: QualitySummary


# The agent API this Hermes speaks (AGENT_API in beetsplug/hermes.py). Both images are built
# from one commit and share a version, but a cluster can still pair an old agent with a new
# Hermes: the health check turns red and imports wait until the pair matches.
REQUIRED_AGENT_API = 2


class BeetsClient:
    name = "beets"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        read_retries: int = 4,
        retry_delay: float = 2.0,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._read_retries = read_retries
        self._retry_delay = retry_delay

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- health --------------------------------------------------------------------

    async def health(self) -> HealthResult:
        try:
            resp = await self._http.get("/healthz")
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            return HealthResult(self.name, ok=False, detail=str(exc))
        config_ok = bool(body.get("config_ok"))
        problems = body.get("config_problems") or []
        agent_api = body.get("agent_api")
        api_ok = agent_api == REQUIRED_AGENT_API
        if not api_ok:
            detail = (
                f"beets-hermes agent API {agent_api or 'unknown'} but this Hermes needs "
                f"{REQUIRED_AGENT_API}: deploy the beets-hermes image built with it"
            )
        elif config_ok:
            detail = "ok"
        else:
            detail = "; ".join(problems) or "beets import config rejected"
        return HealthResult(
            self.name,
            ok=config_ok and api_ok,
            detail=detail,
            data={
                "beets_version": body.get("beets_version"),
                "agent_api": agent_api,
                "agent_version": body.get("agent_version"),
                "config_ok": config_ok,
                "config_problems": problems,
                "worker_busy": body.get("worker_busy"),
            },
        )

    # -- reads ---------------------------------------------------------------------

    async def _albums(self, path: str, params: dict[str, str] | None = None) -> list[LibraryAlbum]:
        """GET a /library/... route. A 503 with ``retry`` means the library is briefly
        locked by an import commit; back off and try again a few times."""
        for attempt in range(self._read_retries + 1):
            resp = await self._http.get(path, params=params)
            if resp.status_code == 503 and attempt < self._read_retries:
                body: dict[str, Any] = resp.json() if resp.content else {}
                if body.get("retry"):
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue
            resp.raise_for_status()
            return [LibraryAlbum.model_validate(a) for a in resp.json().get("albums", [])]
        raise RuntimeError("unreachable")

    async def albums_in_release_group(self, mbid: str) -> list[LibraryAlbum]:
        return await self._albums(f"/library/release-group/{mbid}")

    async def albums_for_release(self, mbid: str) -> list[LibraryAlbum]:
        return await self._albums(f"/library/release/{mbid}")

    async def albums_for_acquisition(self, acquisition_id: int) -> list[LibraryAlbum]:
        return await self._albums(f"/library/acquisition/{acquisition_id}")

    async def search(
        self, artist: str | None = None, album: str | None = None
    ) -> list[LibraryAlbum]:
        params = {k: v for k, v in (("artist", artist), ("album", album)) if v}
        return await self._albums("/library/search", params)

    # -- writes --------------------------------------------------------------------

    async def submit_import(
        self,
        path: str,
        acquisition_id: int,
        search_id: str | None,
        release_group_id: str | None = None,
    ) -> str:
        """Queue a quiet import. With ``release_group_id`` the agent first reads the files'
        MusicBrainz release-group tags and refuses the job, without running beets, when
        they all name one other group."""
        resp = await self._http.post(
            "/import",
            json={
                "path": path,
                "acquisition_id": str(acquisition_id),
                "search_id": search_id,
                "release_group_id": release_group_id,
            },
        )
        resp.raise_for_status()
        return str(resp.json()["job_id"])

    async def mark_imported(self, path: str) -> int:
        """Record a verified import in beets' incremental history so the user's own
        ``beet import`` over the downloads directory skips the folder. Returns how many
        album groups were recorded."""
        resp = await self._http.post("/history", json={"path": path})
        resp.raise_for_status()
        return int(resp.json().get("recorded") or 0)

    async def job(self, job_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/jobs/{job_id}")
        resp.raise_for_status()
        return dict(resp.json())
