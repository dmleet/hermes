"""Suggestions for the request form (docs/ui-plan.md 3.1): an artist as you type, then
that artist's official albums, so the request carries a release-group MBID and never goes
through fuzzy resolution.

Both come from MusicBrainz through the one shared client and limiter. Interactive calls
are one attempt with a short timeout, never a retry: a slow answer is no suggestion, and
the form works without one. Two guards keep the budget bounded whatever a page does: a
single slot, so a second suggestion request while one is in flight is answered empty
rather than queued behind the pipeline's calls; and caches, five minutes per typed text
and an hour per artist, so backspacing and a second request for the same artist cost
nothing. Various Artists is left out: its album list is tens of thousands long and the
policy rejects its albums anyway.

The artist query is the typed text as bare terms: MusicBrainz indexes artist names into an
n-gram field with a popularity boost, so "sigur ro" and "godspeed you" rank the right
artist first from three characters on, with no wildcard and nothing to escape. The album
list is a search, not a browse: browse cannot filter by release status, and a well-loved
artist has hundreds of bootleg live release groups beside a few dozen official ones.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from cachetools import TTLCache

from hermes.config import ResolutionPolicy
from hermes.integrations.musicbrainz import ReleaseGroup
from hermes.services.context import Context
from hermes.services.resolution import policy_rejection

MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

MIN_CHARS = 3  # MusicBrainz's n-gram index starts at three characters
ARTIST_LIMIT = 8
ALBUM_PAGE = 100  # the search endpoint's maximum
ALBUM_MAX = 300  # three pages: enough for any artist's official albums and EPs
TIMEOUT = 5.0  # seconds; a suggestion later than this is no suggestion

# One suggestion request at a time, process-wide; a second one is answered empty. The
# pipeline's own MusicBrainz calls still queue on the shared limiter as before.
_slot = asyncio.Lock()
_artists: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=256, ttl=300)
_albums: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=64, ttl=3600)


def primary_types(policy: ResolutionPolicy) -> list[str]:
    types = ["album"]
    if policy.allow_ep:
        types.append("ep")
    if policy.allow_single:
        types.append("single")
    return types


def _album(rg: ReleaseGroup, policy: ResolutionPolicy) -> dict[str, Any]:
    kind = " / ".join([rg.primary_type or "?", *rg.secondary_types])
    return {
        "mbid": rg.id,
        "title": rg.title,
        "year": rg.first_release_year,
        "type": kind,
        "rejected": policy_rejection(rg, policy),
    }


async def artists(ctx: Context, text: str) -> list[dict[str, Any]]:
    """Up to ``ARTIST_LIMIT`` artists for the typed text, MusicBrainz's ranking, or an
    empty list: too short, busy, unreachable, or nothing found."""
    key = " ".join(text.split()).casefold()
    if len(key) < MIN_CHARS:
        return []
    hit = _artists.get(key)
    if hit is not None:
        return hit
    if _slot.locked():
        return []
    async with _slot:
        try:
            found = await ctx.musicbrainz.search_artists(
                key, limit=ARTIST_LIMIT + 1, timeout=TIMEOUT, retries=0
            )
        except httpx.HTTPError:
            return []
    result = [
        {
            "mbid": a.id,
            "name": a.name,
            "disambiguation": a.disambiguation,
            "country": a.country,
            "type": a.type,
        }
        for a in found
        if a.id != VARIOUS_ARTISTS_MBID
    ][:ARTIST_LIMIT]
    _artists[key] = result
    return result


async def albums(ctx: Context, artist_mbid: str) -> list[dict[str, Any]]:
    """The artist's official release groups of the types the policy allows, newest first,
    each marked with what the policy would say about it (a soundtrack or a live album is
    shown, not hidden: the request path sends a rejected pick to review with the reason)."""
    if not MBID.match(artist_mbid) or artist_mbid == VARIOUS_ARTISTS_MBID:
        return []
    hit = _albums.get(artist_mbid)
    if hit is not None:
        return hit
    if _slot.locked():
        return []
    policy = ctx.policy.resolution
    groups: list[ReleaseGroup] = []
    async with _slot:
        try:
            offset = 0
            while True:
                page, total = await ctx.musicbrainz.official_release_groups(
                    artist_mbid,
                    primary_types(policy),
                    limit=ALBUM_PAGE,
                    offset=offset,
                    timeout=TIMEOUT,
                    retries=0,
                )
                groups.extend(page)
                offset += ALBUM_PAGE
                if not page or offset >= min(total, ALBUM_MAX):
                    break
        except httpx.HTTPError:
            return []
    result = sorted(
        (_album(rg, policy) for rg in groups),
        key=lambda a: (-(a["year"] or 0), a["title"].casefold()),
    )
    _albums[artist_mbid] = result
    return result


def clear_caches() -> None:
    """For tests."""
    _artists.clear()
    _albums.clear()
