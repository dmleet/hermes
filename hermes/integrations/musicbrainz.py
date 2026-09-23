"""MusicBrainz web service client (JSON), rate-limited to one request per second.

MusicBrainz asks for an identifying User-Agent with contact details and enforces roughly
1 req/s per IP; over the limit it answers 503 with a "server is currently busy" body. One
client instance serialises all calls through a single limiter, so every Hermes component
shares the same budget.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hermes import __version__
from hermes.integrations import HealthResult

DEFAULT_BASE_URL = "https://musicbrainz.org/ws/2"


class ArtistCredit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    mbid: str | None = None
    joinphrase: str = ""


class ReleaseRef(BaseModel):
    """A release as listed inside a release group."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    status: str | None = None
    date: str | None = None
    country: str | None = None
    disambiguation: str | None = None
    track_count: int | None = None  # across all media; present when fetched with inc=media
    media_track_counts: list[int] = Field(default_factory=list)  # per medium, in order
    formats: list[str] = Field(default_factory=list)


class Genre(BaseModel):
    """One of MusicBrainz's curated genres (the moderated subset of its tags) with its vote
    count, as they come on a release group."""

    name: str
    count: int


class ReleaseGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    primary_type: str | None = None
    secondary_types: list[str] = Field(default_factory=list)
    first_release_date: str | None = None
    artist_credits: list[ArtistCredit] = Field(default_factory=list)
    releases: list[ReleaseRef] = Field(default_factory=list)
    score: int | None = None  # search results only
    disambiguation: str | None = None
    genres: list[Genre] = Field(default_factory=list)  # most votes first; lookups only

    @property
    def artist_name(self) -> str:
        return "".join(f"{c.name}{c.joinphrase}" for c in self.artist_credits)

    @property
    def artist_mbid(self) -> str | None:
        return self.artist_credits[0].mbid if self.artist_credits else None

    @property
    def first_release_year(self) -> int | None:
        if self.first_release_date and self.first_release_date[:4].isdigit():
            return int(self.first_release_date[:4])
        return None


class ArtistHit(BaseModel):
    """An artist from a search: what a suggestion list shows to tell homonyms apart."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    disambiguation: str | None = None
    country: str | None = None
    type: str | None = None
    score: int | None = None


class Release(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    status: str | None = None
    date: str | None = None
    country: str | None = None
    release_group: ReleaseGroup
    artist_credits: list[ArtistCredit] = Field(default_factory=list)


class Recording(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    artist_credits: list[ArtistCredit] = Field(default_factory=list)
    # Every release the recording appears on, each carrying its release group.
    releases: list[Release] = Field(default_factory=list)

    @property
    def artist_name(self) -> str:
        return "".join(f"{c.name}{c.joinphrase}" for c in self.artist_credits)

    def release_groups(self) -> list[ReleaseGroup]:
        """The distinct release groups the recording appears on, each listing only the
        releases that contain it and credited as those releases are."""
        groups: dict[str, ReleaseGroup] = {}
        for rel in self.releases:
            rg = groups.get(rel.release_group.id)
            if rg is None:
                rg = rel.release_group.model_copy(
                    update={"artist_credits": rel.artist_credits, "releases": []}
                )
                groups[rg.id] = rg
            rg.releases.append(
                ReleaseRef(
                    id=rel.id,
                    title=rel.title,
                    status=rel.status,
                    date=rel.date,
                    country=rel.country,
                )
            )
        return list(groups.values())


def _credits(raw: list[dict[str, Any]] | None) -> list[ArtistCredit]:
    out = []
    for c in raw or []:
        artist = c.get("artist") or {}
        out.append(
            ArtistCredit(
                name=c.get("name") or artist.get("name") or "",
                mbid=artist.get("id"),
                joinphrase=c.get("joinphrase") or "",
            )
        )
    return out


def parse_release_group(raw: dict[str, Any]) -> ReleaseGroup:
    return ReleaseGroup(
        id=raw["id"],
        title=raw.get("title") or "",
        primary_type=raw.get("primary-type"),
        secondary_types=list(raw.get("secondary-types") or []),
        first_release_date=raw.get("first-release-date") or None,
        artist_credits=_credits(raw.get("artist-credit")),
        releases=[
            ReleaseRef(
                id=r["id"],
                title=r.get("title") or "",
                status=r.get("status"),
                date=r.get("date") or None,
                country=r.get("country"),
                disambiguation=r.get("disambiguation") or None,
                track_count=(
                    sum(int(m.get("track-count") or 0) for m in r["media"])
                    if r.get("media")
                    else None
                ),
                media_track_counts=[int(m.get("track-count") or 0) for m in r.get("media") or []],
                formats=sorted({str(m["format"]) for m in r.get("media") or [] if m.get("format")}),
            )
            for r in raw.get("releases") or []
        ],
        score=raw.get("score"),
        disambiguation=raw.get("disambiguation") or None,
        genres=sorted(
            (
                Genre(name=str(g["name"]), count=int(g.get("count") or 0))
                for g in raw.get("genres") or []
                if g.get("name")
            ),
            key=lambda g: (-g.count, g.name),
        ),
    )


def parse_artist(raw: dict[str, Any]) -> ArtistHit:
    return ArtistHit(
        id=raw["id"],
        name=raw.get("name") or "",
        disambiguation=raw.get("disambiguation") or None,
        country=raw.get("country") or None,
        type=raw.get("type") or None,
        score=raw.get("score"),
    )


def parse_recording(raw: dict[str, Any]) -> Recording:
    return Recording(
        id=raw["id"],
        title=raw.get("title") or "",
        artist_credits=_credits(raw.get("artist-credit")),
        releases=[parse_release(r) for r in raw.get("releases") or [] if r.get("release-group")],
    )


def parse_release(raw: dict[str, Any]) -> Release:
    return Release(
        id=raw["id"],
        title=raw.get("title") or "",
        status=raw.get("status"),
        date=raw.get("date") or None,
        country=raw.get("country"),
        release_group=parse_release_group(raw["release-group"]),
        artist_credits=_credits(raw.get("artist-credit")),
    )


class NotFound(Exception):
    pass


def lucene_escape(value: str) -> str:
    """Escape a value for use inside a quoted Lucene phrase."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


_LUCENE_SPECIAL = re.compile(r'[+\-!(){}\[\]^"~*?:\\/&|]')


def lucene_terms(value: str) -> str:
    """Turn free text into bare Lucene terms (OR-ed by MusicBrainz), operators stripped."""
    return " ".join(_LUCENE_SPECIAL.sub(" ", value).split())


class RateLimiter:
    """Minimum spacing between calls, shared by every coroutine using this client."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = min_interval
        self._lock = asyncio.Lock()
        self._last = -float("inf")
        self._clock = clock
        self._sleep = sleep

    async def __aenter__(self) -> None:
        async with self._lock:
            wait = self._last + self._interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last = self._clock()

    async def __aexit__(self, *exc: object) -> None:
        return None


class MusicBrainzClient:
    name = "musicbrainz"

    def __init__(
        self,
        contact: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = 1.0,
        timeout: float = 30.0,
        busy_retries: int = 3,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.contact = contact
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "User-Agent": f"hermes/{__version__} ({contact or 'no contact configured'})",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        self._limiter = limiter or RateLimiter(min_interval)
        self._busy_retries = busy_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> HealthResult:
        """No network call: a health probe every 30 s would burn the rate budget."""
        ok = bool(self.contact)
        return HealthResult(
            self.name,
            ok=ok,
            detail="ok" if ok else "MUSICBRAINZ_CONTACT is empty; MusicBrainz requires one",
            data={"user_agent": self._http.headers["User-Agent"]},
        )

    async def _get(
        self,
        path: str,
        params: dict[str, str],
        *,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """``retries`` and ``timeout`` default to the client's; an interactive caller (the
        request form's suggestions) passes 0 and a few seconds, since a late answer is
        worth nothing to it and the pipeline's calls are waiting on the same limiter."""
        params = {**params, "fmt": "json"}
        retries = self._busy_retries if retries is None else retries
        for attempt in range(retries + 1):
            try:
                async with self._limiter:
                    resp = await self._http.get(
                        path,
                        params=params,
                        timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
                    )
            except httpx.TimeoutException:
                # MusicBrainz stalls now and then; treated like its "busy" reply.
                if attempt < retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise
            # 503 is MusicBrainz's "busy" reply; 502 and 504 are its gateway on a bad day.
            if resp.status_code in (502, 503, 504) and attempt < retries:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code == 404:
                raise NotFound(path)
            resp.raise_for_status()
            return dict(resp.json())
        raise RuntimeError("unreachable")

    async def search_release_groups(
        self, artist: str, title: str, *, limit: int = 10, loose: bool = False
    ) -> list[ReleaseGroup]:
        """Exact: artist and title as phrases. Loose: artist as a phrase, title as bare terms,
        so "Lift Your Skinny Fists" still finds "Lift Yr. Skinny Fists..."; the resolver's
        similarity scoring then decides whether the hit is close enough."""
        if loose:
            terms = lucene_terms(title)
            if not terms:
                return []
            query = f'artist:"{lucene_escape(artist)}" AND releasegroup:({terms})'
        else:
            query = f'artist:"{lucene_escape(artist)}" AND releasegroup:"{lucene_escape(title)}"'
        body = await self._get("/release-group/", {"query": query, "limit": str(limit)})
        return [parse_release_group(r) for r in body.get("release-groups", [])]

    async def search_artists(
        self,
        text: str,
        *,
        limit: int = 8,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> list[ArtistHit]:
        """Artists for free text as bare terms, MusicBrainz's ranking. Its artist index
        carries an n-gram field with a popularity boost, so a fragment of three characters
        or more finds the artist without a wildcard."""
        terms = lucene_terms(text)
        if not terms:
            return []
        body = await self._get(
            "/artist/", {"query": terms, "limit": str(limit)}, retries=retries, timeout=timeout
        )
        return [parse_artist(a) for a in body.get("artists", [])]

    async def official_release_groups(
        self,
        artist_mbid: str,
        primary_types: Sequence[str],
        *,
        limit: int = 100,
        offset: int = 0,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> tuple[list[ReleaseGroup], int]:
        """One page of the artist's release groups that have an official release, of the
        given primary types, with the total. A search rather than a browse: browse cannot
        filter by release status, and bootleg live recordings outnumber official albums
        ten to one for a well-loved artist."""
        types = " OR ".join(primary_types)
        query = f"arid:{artist_mbid} AND status:official AND primarytype:({types})"
        body = await self._get(
            "/release-group/",
            {"query": query, "limit": str(limit), "offset": str(offset)},
            retries=retries,
            timeout=timeout,
        )
        groups = [parse_release_group(r) for r in body.get("release-groups", [])]
        return groups, int(body.get("count") or 0)

    async def release_group(self, mbid: str) -> ReleaseGroup:
        """The group with every release and, per release, its track count (``media``), which
        is what tells a 9-track "Ghosts I" from the 36-track "Ghosts I-IV" in one group; and
        its genres, which ride along on the same request."""
        body = await self._get(
            f"/release-group/{mbid}", {"inc": "releases+media+artist-credits+genres"}
        )
        return parse_release_group(body)

    async def release(self, mbid: str) -> Release:
        body = await self._get(f"/release/{mbid}", {"inc": "release-groups+artist-credits"})
        return parse_release(body)

    async def release_track_lengths(self, mbid: str) -> list[int]:
        """Track lengths in milliseconds, in order, for one release (0 where unknown)."""
        body = await self._get(f"/release/{mbid}", {"inc": "recordings"})
        return [
            int(t.get("length") or 0)
            for medium in body.get("media") or []
            for t in medium.get("tracks") or []
        ]

    async def recording(self, mbid: str) -> Recording:
        """A recording with every release (and release group) it appears on: one call
        answers "which album is this track from" for discovery."""
        body = await self._get(
            f"/recording/{mbid}", {"inc": "releases+release-groups+artist-credits"}
        )
        return parse_recording(body)
