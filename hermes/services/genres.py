"""Genres for the queue and detail pages, from MusicBrainz's release-group genres.

A target born from a release-group lookup gets its genres on the same reply (``upsert_target``
stores them); targets from before that, or whose lookup predates this, have ``genres`` NULL
and a background job fills them in, active rows first, one request per second like every
other MusicBrainz call. What is stored is the top five with their vote counts, so the page
rule can change, and a filter can be added, without another pass over MusicBrainz.

The pages show fewer: two on a queue row (the line wraps at about 44 characters on a phone),
three on the detail page. Raw counts would show "rock · indie rock" for most indie albums,
so a genre whose words all appear in a more specific one is dropped when that one has at
least half its votes: "indie rock · post-punk revival" for Interpol, "industrial rock ·
industrial metal" for Nine Inch Nails, but a heavily voted "rock" survives one stray vote
for "math rock".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition, AlbumTarget
from hermes.domain.state import TERMINAL
from hermes.integrations.musicbrainz import NotFound, ReleaseGroup
from hermes.services.context import Context

log = logging.getLogger("hermes.genres")

KEPT = 5  # per target, in the database
BATCH = 20  # targets per tick: 20 s of the shared MusicBrainz limiter at most
ON_ROW = 2
ON_PAGE = 3


def stored(rg: ReleaseGroup) -> dict[str, Any]:
    """What a target keeps: the top ``KEPT`` genres with their counts, most votes first."""
    return {"top": [{"name": g.name, "count": g.count} for g in rg.genres[:KEPT]]}


def display(genres: dict[str, Any] | None, limit: int) -> list[str]:
    """The names to show, most votes first, a general genre dropped for a more specific one
    (its words a strict superset) that has at least half the votes."""
    rows = [
        (str(g.get("name") or ""), int(g.get("count") or 0))
        for g in (genres or {}).get("top") or []
        if g.get("name")
    ]
    kept: list[str] = []
    for name, count in rows:
        words = set(name.split())
        superseded = any(
            words < set(other.split()) and other_count * 2 >= count for other, other_count in rows
        )
        if not superseded:
            kept.append(name)
    return kept[:limit]


def _queue(limit: int) -> Any:
    """Targets never looked up for genres, the ones with an active acquisition first."""
    active = (
        select(Acquisition.album_target_id)
        .where(Acquisition.state.not_in([s.value for s in TERMINAL]))
        .scalar_subquery()
    )
    return (
        select(AlbumTarget.id)
        .where(AlbumTarget.genres.is_(None))
        .order_by(case((AlbumTarget.id.in_(active), 0), else_=1), AlbumTarget.id)
        .limit(limit)
    )


async def tick(session: Session, ctx: Context, *, limit: int = BATCH) -> dict[str, int]:
    """Fill in genres for up to ``limit`` targets that have none recorded. Returns counts by
    outcome. MusicBrainz being unreachable ends the batch; the next tick tries again."""
    ids = list(session.scalars(_queue(limit)).all())
    counts: dict[str, int] = {}
    for target_id in ids:
        target = session.get(AlbumTarget, target_id)
        if target is None:
            continue
        mbid = target.release_group_mbid
        session.commit()  # no write lock across the network call
        try:
            rg = await ctx.musicbrainz.release_group(mbid)
        except NotFound:
            found: dict[str, Any] = {"top": []}
            outcome = "missing"
        except httpx.HTTPError as exc:
            log.warning("genres for %s: MusicBrainz unavailable (%s); stopping", mbid, exc)
            counts["unavailable"] = counts.get("unavailable", 0) + 1
            break
        except Exception:  # noqa: BLE001 - genres are cosmetic; one odd reply must not stall them
            # Recorded as none found, so this target does not head every later batch.
            log.exception("genres for %s failed; recording none", mbid)
            session.rollback()
            found, outcome = {"top": []}, "error"
        else:
            found, outcome = stored(rg), "fetched" if rg.genres else "none"
        target = session.get(AlbumTarget, target_id)  # expired by the commit: re-read
        if target is None:
            continue
        target.genres = found
        session.commit()
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
