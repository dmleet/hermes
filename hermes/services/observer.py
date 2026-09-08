"""Observer stage: watch active grab attempts in Deluge (docs/plan.md 5.8).

One ``tick`` polls every instance that has active attempts and moves acquisitions:

    SUBMITTED   -> DOWNLOADING      first bytes arrive
    DOWNLOADING -> READY_FOR_BEETS  finished, seeding, and moved to the completed path
    DOWNLOADING -> STALLED -> SUBMITTED (next candidate) | FAILED   no progress for stall_hours
    any         -> FAILED           torrent vanished from Deluge, or Deluge reports an error

The same function is the startup reconcile: it trusts Deluge, never memory.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition, Event, GrabAttempt, GrabOutcome, utcnow
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.integrations.deluge import STATUS_KEYS, DelugeError
from hermes.services.context import Context
from hermes.services.submit import submit

ACTIVE_STATES = {S.SUBMITTED, S.DOWNLOADING}
MOVE_GRACE = timedelta(minutes=10)


def active_attempts(session: Session) -> list[GrabAttempt]:
    rows = session.scalars(
        select(GrabAttempt)
        .join(Acquisition, GrabAttempt.acquisition_id == Acquisition.id)
        .where(GrabAttempt.outcome == GrabOutcome.ACTIVE, Acquisition.state.in_(ACTIVE_STATES))
    ).all()
    return list(rows)


def _completed_path(status: dict[str, Any]) -> str:
    return f"{str(status['save_path']).rstrip('/')}/{status['name']}"


def _aware(dt: datetime) -> datetime:
    """SQLite hands timezone-aware columns back naive; every stored time is UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _note_once(session: Session, acq: Acquisition, message: str, **kw: Any) -> None:
    """Add an event unless the last one already says the same thing (poll loops)."""
    if acq.events and acq.events[-1].message == message:
        return
    session.add(Event(acquisition=acq, message=message, **kw))


async def _fallback(session: Session, ctx: Context, acq: Acquisition, reason: str) -> None:
    """After a stalled or broken attempt: fall back to the next candidate, but only when the
    policy would have allowed that grab without a human. Otherwise wait in STALLED for an
    approval, which is exactly what a grab is."""
    transition(session, acq, S.STALLED, reason, level="warning")
    session.commit()
    policy = ctx.policy
    if policy.dry_run or policy.approval.timid:
        session.add(
            Event(
                acquisition=acq,
                message="next candidate needs approval (dry_run or timid); waiting",
            )
        )
        session.commit()
        return
    try:
        await submit(session, ctx, acq)
    except Exception as exc:  # noqa: BLE001 - never leave it stranded in STALLED
        session.rollback()
        transition(
            session,
            acq,
            S.FAILED,
            f"fallback submit failed: {type(exc).__name__}: {exc}",
            data={"retryable": True},
        )
        session.commit()


# Attempts whose hash was missing from Deluge on the previous tick (a daemon that is still
# loading its session can answer with a partial map; one miss is not a removal).
_missing_once: set[int] = set()


async def observe_attempt(
    session: Session,
    ctx: Context,
    attempt: GrabAttempt,
    status: dict[str, Any] | None,
    now: datetime,
) -> None:
    acq = attempt.acquisition
    if status is None:
        if attempt.id not in _missing_once:
            _missing_once.add(attempt.id)
            _note_once(
                session,
                acq,
                f"torrent {attempt.infohash} not reported by Deluge; checking again next tick",
            )
            session.commit()
            return
        _missing_once.discard(attempt.id)
        attempt.outcome = GrabOutcome.REMOVED
        transition(
            session,
            acq,
            S.FAILED,
            f"torrent {attempt.infohash} is no longer in Deluge {attempt.deluge_instance}",
            data={"attempt_id": attempt.id},
        )
        session.commit()
        return
    _missing_once.discard(attempt.id)

    state = str(status.get("state") or "")
    total_done = int(status.get("total_done") or 0)
    if total_done > (attempt.last_total_done or 0):
        attempt.last_total_done = total_done
        attempt.last_progress_at = now

    if state == "Error":
        attempt.outcome = GrabOutcome.FAILED
        await _fallback(session, ctx, acq, f"Deluge reports an error for {status.get('name')}")
        return

    if acq.state == S.SUBMITTED and (total_done > 0 or state in ("Downloading", "Seeding")):
        transition(
            session,
            acq,
            S.DOWNLOADING,
            f"downloading: {status.get('name')}",
            data={"total_done": total_done, "total_wanted": status.get("total_wanted")},
        )
        session.commit()

    # Deluge reports a finished torrent as Seeding, but also as Queued (over the seeding
    # slot limit) or Paused (stop-at-ratio). All of those are done; Moving/Checking/Error
    # are not.
    finished = bool(status.get("is_finished")) and state not in ("Moving", "Checking", "Error")
    moved = str(status.get("save_path", "")).rstrip("/") == attempt.completed_location.rstrip("/")
    if finished and not moved:
        # Deluge moves on the finished alert; an adopted torrent that was already complete
        # never gets that alert and stays where it is. Give the move a grace period, then
        # accept the actual location: the files are there and beets copies from anywhere.
        since = _aware(attempt.last_progress_at or attempt.added_at)
        if _aware(now) - since < MOVE_GRACE:
            _note_once(
                session,
                acq,
                f"finished; waiting for Deluge to move to {attempt.completed_location}",
                data={"save_path": status.get("save_path")},
            )
            session.commit()
            return
        session.add(
            Event(
                acquisition=acq,
                level="warning",
                message=f"Deluge did not move the torrent; using its actual location "
                f"{status.get('save_path')}",
            )
        )
        moved = True
    if finished and moved:
        attempt.outcome = GrabOutcome.COMPLETED
        attempt.completed_at = now
        attempt.completed_path = _completed_path(status)
        if acq.state == S.SUBMITTED:  # finished within one poll interval
            transition(session, acq, S.DOWNLOADING, "downloading (completed before first poll)")
        transition(
            session,
            acq,
            S.READY_FOR_BEETS,
            f"download complete at {attempt.completed_path}",
            data={
                "attempt_id": attempt.id,
                "completed_path": attempt.completed_path,
                "beets_path": ctx.policy.paths.to_beets(attempt.completed_path),
                "total_done": total_done,
            },
        )
        session.commit()
        return

    stall_after = timedelta(hours=ctx.policy.deluge.stall_hours)
    last = _aware(attempt.last_progress_at or attempt.added_at)
    if acq.state == S.DOWNLOADING and _aware(now) - last > stall_after:
        attempt.outcome = GrabOutcome.STALLED
        await _fallback(
            session,
            ctx,
            acq,
            f"no progress for {ctx.policy.deluge.stall_hours}h "
            f"({total_done / 1e6:.0f} MB done, {status.get('num_seeds')} seeds)",
        )


async def tick(session: Session, ctx: Context, now: datetime | None = None) -> dict[str, int]:
    """Poll Deluge for every active attempt. Returns counts for logging/tests."""
    now = now or utcnow()
    counts: dict[str, int] = defaultdict(int)
    by_instance: dict[str, list[GrabAttempt]] = defaultdict(list)
    for attempt in active_attempts(session):
        by_instance[attempt.deluge_instance].append(attempt)

    for instance, attempts in by_instance.items():
        client = ctx.deluge.get(instance)
        if client is None:
            counts["unconfigured_instance"] += len(attempts)
            continue
        try:
            statuses = await client.torrents_status([a.infohash for a in attempts], STATUS_KEYS)
        except (httpx.HTTPError, DelugeError):
            counts["unreachable"] += len(attempts)
            continue  # transient; try again next tick, never fail acquisitions for this
        for attempt in attempts:
            await observe_attempt(
                session, ctx, attempt, statuses.get(attempt.infohash.lower()), now
            )
            session.commit()  # progress bookkeeping must persist even when no state changed
            counts["observed"] += 1
    return dict(counts)
