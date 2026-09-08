"""ListenBrainz: the playlists generated for a user and their tracks (docs/plan.md 5.1).

Read-only. Public playlists need no token; a token (``LISTENBRAINZ_TOKEN``) only adds the
user's private ones. The token travels in a header and never appears in an error message.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hermes import __version__
from hermes.integrations import HealthResult

DEFAULT_BASE_URL = "https://api.listenbrainz.org/1"
_JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"
_JSPF_TRACK = "https://musicbrainz.org/doc/jspf#track"


class ListenBrainzError(Exception):
    """An HTTP failure with the URL's secrets (none today) and headers left out."""


class Track(BaseModel):
    model_config = ConfigDict(extra="ignore")

    position: int
    recording_mbid: str | None
    title: str = ""
    artist: str = ""
    album: str | None = None
    # ListenBrainz's own idea of a release for the track (cover-art lookup), a hint only.
    release_mbid: str | None = None


class Playlist(BaseModel):
    """A playlist's metadata; ``tracks`` is filled by ``playlist()`` only."""

    model_config = ConfigDict(extra="ignore")

    mbid: str
    title: str
    creator: str = ""
    created_for: str | None = None
    created_at: str | None = None
    # troi's patch name: "weekly-exploration", "weekly-jams", "daily-jams"; None for a
    # hand-made playlist.
    source: str | None = None
    public: bool = True
    tracks: list[Track] = Field(default_factory=list)


def _mbid_from_url(value: Any) -> str | None:
    """JSPF identifiers are URLs ("https://musicbrainz.org/recording/<mbid>"), sometimes a
    list of them."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    return tail if len(tail) == 36 else None


def parse_playlist(raw: dict[str, Any]) -> Playlist:
    pl = raw.get("playlist", raw)
    ext = (pl.get("extension") or {}).get(_JSPF_PLAYLIST) or {}
    meta = ext.get("additional_metadata") or {}
    tracks = []
    for position, track in enumerate(pl.get("track") or []):
        text = (track.get("extension") or {}).get(_JSPF_TRACK) or {}
        tmeta = text.get("additional_metadata") or {}
        tracks.append(
            Track(
                position=position,
                recording_mbid=_mbid_from_url(track.get("identifier")),
                title=track.get("title") or "",
                artist=track.get("creator") or "",
                album=track.get("album") or None,
                release_mbid=tmeta.get("caa_release_mbid"),
            )
        )
    return Playlist(
        mbid=_mbid_from_url(pl.get("identifier")) or "",
        title=pl.get("title") or "",
        creator=pl.get("creator") or "",
        created_for=ext.get("created_for"),
        created_at=pl.get("date"),
        source=(meta.get("algorithm_metadata") or {}).get("source_patch"),
        public=bool(ext.get("public", True)),
        tracks=tracks,
    )


class ListenBrainzClient:
    name = "listenbrainz"

    def __init__(
        self,
        *,
        token: str | None = None,
        contact: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        headers = {
            "User-Agent": f"hermes/{__version__} ({contact or 'no contact configured'})",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Token {token}"
        self._has_token = bool(token)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> HealthResult:
        """No network call: the API is rate limited per client and a probe every 30 s would
        spend that budget on nothing (same reasoning as the MusicBrainz client)."""
        return HealthResult(self.name, ok=True, detail="ok", data={"token": self._has_token})

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            resp = await self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ListenBrainzError(
                f"ListenBrainz {path}: HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise ListenBrainzError(f"ListenBrainz {path}: {type(exc).__name__}") from None
        return dict(resp.json())

    async def created_for(self, user: str, *, limit: int = 200) -> list[Playlist]:
        """Playlists generated for ``user`` (Weekly Exploration and friends), newest first,
        without tracks. Generated playlists expire after two weeks, so the list is short."""
        out: dict[str, Playlist] = {}
        offset = 0
        page_size = 50
        while len(out) < limit:
            body = await self._get(
                f"/user/{user}/playlists/createdfor",
                {"count": str(page_size), "offset": str(offset)},
            )
            page = [parse_playlist(p) for p in body.get("playlists") or []]
            for playlist in page:
                out.setdefault(playlist.mbid, playlist)
            offset += len(page)
            total = int(body.get("playlist_count") or 0)
            if len(page) < page_size or offset >= total:
                break
        return list(out.values())[:limit]

    async def playlist(self, mbid: str) -> Playlist:
        """One playlist with its tracks (JSPF)."""
        return parse_playlist(await self._get(f"/playlist/{mbid}"))
