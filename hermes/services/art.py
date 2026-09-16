"""Album art for the pages (docs/ui-plan.md 4.1).

A background job fetches the Cover Art Archive's front cover for each album target and
keeps it as served under ``<data dir>/art/<release group mbid>.jpg``. Cosmetic: a slow or
absent archive never touches a request or a pipeline stage. Every target starts ``pending``
(the migration backfills the rows already there); ``fetched`` has a file, ``missing`` means
the archive has no front cover and is looked at again after a month (it gains art all the
time), ``failed`` is retried after an hour and then daily. Rows with an active acquisition
go first, so the queue gets its art before history does. Each fetch runs with the session
committed (conventions: no write lock across a network call).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition, AlbumTarget, utcnow
from hermes.domain.state import TERMINAL
from hermes.integrations.coverart import CoverArtError
from hermes.services.context import Context

log = logging.getLogger("hermes.art")

BATCH = 20  # targets per tick: bounds a tick when archive.org is slow
FAILED_RETRY_FIRST = timedelta(hours=1)
FAILED_RETRY = timedelta(days=1)
MISSING_RETRY = timedelta(days=30)

PENDING, FETCHED, MISSING, FAILED = "pending", "fetched", "missing", "failed"


def art_path(art_dir: Path, release_group_mbid: str) -> Path:
    return art_dir / f"{release_group_mbid}.jpg"


def due(now: datetime | None = None) -> Any:
    """Targets whose art should be (re)tried. Stored times are naive UTC (SQLite)."""
    now = _naive_utc(now or utcnow())
    checked = AlbumTarget.art_checked_at
    return or_(
        AlbumTarget.art_status == PENDING,
        checked.is_(None),
        and_(
            AlbumTarget.art_status == FAILED,
            AlbumTarget.art_failures <= 1,
            checked <= now - FAILED_RETRY_FIRST,
        ),
        and_(
            AlbumTarget.art_status == FAILED,
            AlbumTarget.art_failures > 1,
            checked <= now - FAILED_RETRY,
        ),
        and_(AlbumTarget.art_status == MISSING, checked <= now - MISSING_RETRY),
    )


def _naive_utc(value: datetime) -> datetime:
    """SQLite stores naive UTC; an aware time is converted, a naive one is taken as UTC."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _queue(limit: int, now: datetime | None = None) -> Any:
    active = (
        select(Acquisition.id)
        .where(
            Acquisition.album_target_id == AlbumTarget.id,
            Acquisition.state.not_in([s.value for s in TERMINAL]),
        )
        .exists()
    )
    return (
        select(AlbumTarget.id)
        .where(due(now))
        .order_by(case((active, 0), else_=1), AlbumTarget.id)
        .limit(limit)
    )


def _write(path: Path, data: bytes) -> None:
    """Atomic: a reader never sees a half-written cover. The temporary name carries the pid
    so a CLI tick and the server's job fetching the same target do not trip over each
    other's file; the last replace wins and both are the same image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def tick(
    session: Session, ctx: Context, *, limit: int = BATCH, now: datetime | None = None
) -> dict[str, int]:
    """Fetch art for up to ``limit`` due targets. Returns counts by outcome."""
    if ctx.coverart is None or ctx.art_dir is None:
        return {}
    ids = list(session.scalars(_queue(limit, now)).all())
    counts: dict[str, int] = {}
    for target_id in ids:
        target = session.get(AlbumTarget, target_id)
        if target is None:
            continue
        group, release = target.release_group_mbid, target.preferred_release_mbid
        session.commit()  # release the write lock before the network call
        note = ""
        try:
            image = await ctx.coverart.front(release_group_mbid=group, release_mbid=release)
            if image is not None:
                _write(art_path(ctx.art_dir, group), image.data)
        except (CoverArtError, OSError) as exc:
            # A full or read-only data directory is a failure like any other: recorded
            # with the backoff, so one bad row cannot starve the batch every tick.
            status, note = FAILED, str(exc)
        else:
            status = MISSING if image is None else FETCHED
        target = session.get(AlbumTarget, target_id)  # expired by the commit: re-read
        if target is None:
            continue
        target.art_status = status
        target.art_checked_at = _naive_utc(now or utcnow()).replace(tzinfo=UTC)
        target.art_failures = target.art_failures + 1 if status == FAILED else 0
        session.commit()
        counts[status] = counts.get(status, 0) + 1
        if status == FAILED:
            log.warning("art for %s: %s (failure %d)", group, note, target.art_failures)
    return counts
