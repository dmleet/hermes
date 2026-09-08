"""Prowlarr client. Search-only: Hermes never asks Prowlarr to grab (docs/plan.md A1)."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hermes.integrations import HealthResult

FREE_FLAGS = {"freeleech", "g_freeleech", "neutral"}


class ReleaseResult(BaseModel):
    """One row of ``GET /api/v1/search``, reduced to what Hermes uses."""

    model_config = ConfigDict(extra="ignore")

    guid: str
    indexer_id: int
    indexer: str
    title: str
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    download_url: str | None = None
    info_url: str | None = None
    magnet_url: str | None = None
    info_hash: str | None = None
    categories: list[int] = Field(default_factory=list)
    publish_date: str | None = None
    protocol: str | None = None
    indexer_flags: list[str] = Field(default_factory=list)
    files: int | None = None
    grabs: int | None = None

    @property
    def freeleech(self) -> bool:
        return any(f.lower() in FREE_FLAGS for f in self.indexer_flags)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ReleaseResult:
        return cls(
            guid=raw["guid"],
            indexer_id=raw["indexerId"],
            indexer=raw.get("indexer") or "",
            title=raw.get("title") or "",
            size=raw.get("size"),
            seeders=raw.get("seeders"),
            leechers=raw.get("leechers"),
            download_url=raw.get("downloadUrl"),
            info_url=raw.get("infoUrl"),
            magnet_url=raw.get("magnetUrl"),
            info_hash=(raw.get("infoHash") or None),
            categories=[c["id"] for c in raw.get("categories") or [] if "id" in c],
            publish_date=raw.get("publishDate"),
            protocol=raw.get("protocol"),
            indexer_flags=list(raw.get("indexerFlags") or []),
            files=raw.get("files"),
            grabs=raw.get("grabs"),
        )


class ProwlarrClient:
    name = "prowlarr"

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 90.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> HealthResult:
        try:
            resp = await self._http.get("/api/v1/system/status")
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            return HealthResult(self.name, ok=False, detail=str(exc))
        return HealthResult(
            self.name,
            ok=True,
            detail="ok",
            data={"version": body.get("version")},
        )

    async def indexers(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/api/v1/indexer")
        resp.raise_for_status()
        return [
            {"id": ix["id"], "name": ix["name"], "enable": ix.get("enable", True)}
            for ix in resp.json()
        ]

    async def search(
        self,
        query: str,
        *,
        categories: tuple[int, ...] = (3000,),
        indexer_ids: list[int] | None = None,
        limit: int = 100,
    ) -> list[ReleaseResult]:
        """Music search across the configured indexers (or the given ones)."""
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("query", query),
            ("type", "music"),
            ("limit", str(limit)),
            *[("categories", str(c)) for c in categories],
        ]
        if indexer_ids:
            params += [("indexerIds", str(i)) for i in indexer_ids]
        resp = await self._http.get("/api/v1/search", params=params)
        resp.raise_for_status()
        return [ReleaseResult.from_api(r) for r in resp.json()]

    async def fetch_torrent(self, download_url: str) -> bytes:
        """Download the .torrent through Prowlarr's proxied URL (token logic applies).

        Uses a header-less client: the URL already carries the API key as a query
        parameter, and a redirect to the tracker must not receive our X-Api-Key header.
        Errors are re-raised with a message that does not contain the URL (and so the key).
        """
        async with httpx.AsyncClient(timeout=self._http.timeout, follow_redirects=True) as bare:
            try:
                resp = await bare.get(download_url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise httpx.HTTPStatusError(
                    f"Prowlarr download proxy answered HTTP {exc.response.status_code}",
                    request=exc.request,
                    response=exc.response,
                ) from None
            except httpx.RequestError as exc:
                raise httpx.ConnectError(
                    f"Prowlarr download proxy unreachable: {type(exc).__name__}"
                ) from None
        return resp.content
