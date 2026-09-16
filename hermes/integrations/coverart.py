"""Cover Art Archive client: the front cover of a release or release group, as served.

Public, no credentials. The archive answers with redirects to archive.org, which is slow
and sometimes down, so the only caller is the background art job (services/art.py); a
request or a pipeline stage never waits on this. Its own limiter, because it is a different
host from MusicBrainz but asks for the same politeness. The health check makes no network
call and never fails: art is cosmetic and must not turn /healthz red.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hermes import __version__
from hermes.integrations import HealthResult
from hermes.integrations.musicbrainz import RateLimiter

DEFAULT_BASE_URL = "https://coverartarchive.org"
IMAGE_TYPES = ("image/jpeg", "image/png")
MAX_BYTES = 1_000_000  # a front-500 is 30-60 KB; anything near this is not what we asked for


class CoverArtError(Exception):
    """A fetch that should be retried later: transport error, unexpected status, or a
    body that is not an image. A missing cover is not an error (see ``front``)."""


@dataclass(frozen=True)
class ArtImage:
    data: bytes
    content_type: str  # image/jpeg or image/png


class CoverArtClient:
    name = "coverart"

    def __init__(
        self,
        contact: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = 1.0,
        timeout: float = 30.0,  # archive.org's tail latency is routinely over ten seconds
        limiter: RateLimiter | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "User-Agent": f"hermes/{__version__} ({contact or 'no contact configured'})",
                "Accept": ", ".join(IMAGE_TYPES),
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(min_interval)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> HealthResult:
        return HealthResult(self.name, ok=True, detail="cosmetic; not checked")

    async def front(
        self, *, release_group_mbid: str, release_mbid: str | None = None, size: int = 500
    ) -> ArtImage | None:
        """The front cover: the given release's when it has one, else whichever release in
        the group the archive picks. ``None`` means the archive has no front cover for
        either (404); anything else that is not an image raises ``CoverArtError``."""
        paths = []
        if release_mbid:
            paths.append(f"/release/{release_mbid}/front-{size}")
        paths.append(f"/release-group/{release_group_mbid}/front-{size}")
        for path in paths:
            try:
                async with self._limiter:
                    resp = await self._http.get(path)
            except httpx.HTTPError as exc:
                raise CoverArtError(f"{path}: {exc.__class__.__name__}") from exc
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise CoverArtError(f"{path}: HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in IMAGE_TYPES:
                raise CoverArtError(f"{path}: not an image ({content_type or 'no content type'})")
            if not resp.content:
                raise CoverArtError(f"{path}: empty body")
            if len(resp.content) > MAX_BYTES:
                raise CoverArtError(f"{path}: {len(resp.content)} bytes is too large")
            return ArtImage(resp.content, content_type)
        return None
