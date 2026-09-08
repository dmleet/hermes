"""Manual album requests: Signal -> AlbumTarget -> Acquisition through resolution and the
library check (docs/plan.md 5.2, 5.3, and the dedupe rules in section 4).

Ends in RESOLVED (missing, ready for search in M2), ALREADY_OWNED, NEEDS_REVIEW (candidates
recorded on the event), or FAILED (nothing found).
"""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.domain.models import (
    Acquisition,
    AlbumTarget,
    Event,
    LibraryStatus,
    Origin,
    ResolutionStatus,
    Signal,
    SignalKind,
    utcnow,
)
from hermes.domain.state import TERMINAL, transition
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.musicbrainz import ReleaseGroup
from hermes.services.approval import decide
from hermes.services.context import Context
from hermes.services.library_check import check_library
from hermes.services.pipeline import search_and_decide
from hermes.services.resolution import Resolution, resolve_by_mbid, resolve_by_name


def upsert_target(session: Session, rg: ReleaseGroup, preferred_release: str | None) -> AlbumTarget:
    target = session.scalar(select(AlbumTarget).where(AlbumTarget.release_group_mbid == rg.id))
    if target is None:
        target = AlbumTarget(release_group_mbid=rg.id, artist_name=rg.artist_name, title=rg.title)
        session.add(target)
    target.artist_name = rg.artist_name
    target.artist_mbid = rg.artist_mbid
    target.title = rg.title
    target.primary_type = rg.primary_type
    target.secondary_types = {"types": rg.secondary_types}
    target.first_release_year = rg.first_release_year
    target.release_mbids = {"ids": [r.id for r in rg.releases]}
    if preferred_release:
        target.preferred_release_mbid = preferred_release
    session.flush()
    return target


def _active_acquisition(session: Session, target: AlbumTarget) -> Acquisition | None:
    rows = session.scalars(
        select(Acquisition)
        .where(Acquisition.album_target_id == target.id)
        .order_by(Acquisition.id.desc())
    ).all()
    return next((a for a in rows if a.state not in TERMINAL), None)


async def submit_manual_request(
    session: Session,
    ctx: Context,
    *,
    artist: str | None = None,
    title: str | None = None,
    mbid: str | None = None,
) -> Acquisition:
    if not mbid and not (artist and title):
        raise ValueError("a manual request needs artist and title, or an MBID")
    policy = ctx.policy
    clients = ctx

    signal = Signal(
        kind=SignalKind.MANUAL,
        requested_artist=artist,
        requested_title=title,
        requested_mbid=mbid,
    )
    session.add(signal)
    session.commit()  # do not hold the SQLite write lock across the MusicBrainz call

    try:
        resolution: Resolution = (
            await resolve_by_mbid(clients.musicbrainz, policy.resolution, mbid)
            if mbid
            else await resolve_by_name(
                clients.musicbrainz, policy.resolution, artist or "", title or ""
            )
        )
    except httpx.HTTPError as exc:
        # Upstream trouble is not a resolution verdict: fail the acquisition with a
        # retryable reason rather than surfacing a stack trace to the caller.
        signal.resolution_status = ResolutionStatus.FAILED
        signal.resolution_note = f"MusicBrainz unavailable: {exc!r}"
        acq = Acquisition(album_target=None, signal=signal, state=S.MANUAL, origin=Origin.MANUAL)
        session.add(acq)
        session.flush()
        transition(
            session,
            acq,
            S.FAILED,
            f"MusicBrainz unavailable: {type(exc).__name__}: {exc}",
            data={"signal_id": signal.id, "retryable": True},
        )
        session.commit()
        return acq
    candidates = [c.as_dict() for c in resolution.candidates]

    if resolution.outcome != "resolved" or resolution.target is None:
        signal.resolution_status = (
            ResolutionStatus.NEEDS_REVIEW
            if resolution.outcome == "needs_review"
            else ResolutionStatus.FAILED
        )
        signal.resolution_note = resolution.note
        acq = Acquisition(album_target=None, signal=signal, state=S.MANUAL, origin=Origin.MANUAL)
        session.add(acq)
        session.flush()
        if resolution.outcome == "needs_review":
            transition(
                session,
                acq,
                S.NEEDS_REVIEW,
                f"resolution needs review: {resolution.note}",
                data={"candidates": candidates, "signal_id": signal.id},
                level="warning",
            )
        else:
            transition(
                session,
                acq,
                S.FAILED,
                f"resolution failed: {resolution.note}",
                data={"signal_id": signal.id},
            )
        session.commit()
        return acq

    rg = resolution.target
    target = upsert_target(session, rg, resolution.preferred_release_mbid)
    signal.album_target = target
    signal.resolution_status = ResolutionStatus.RESOLVED
    signal.resolution_note = f"{rg.artist_name} - {rg.title} ({rg.primary_type})"

    existing = _active_acquisition(session, target)
    if existing is not None:
        # Dedupe: a manual request for a target already in flight attaches to it. If that
        # acquisition is sitting on a decision (candidates ready, or waiting for approval),
        # a repeated manual request is the user saying "go": run the approval gate again.
        session.add(
            Event(
                acquisition=existing,
                message="manual request attached to existing acquisition",
                data={"signal_id": signal.id, "state": existing.state},
            )
        )
        session.commit()
        if existing.state == S.RESOLVED:
            # Resolved but never searched (Prowlarr was down, or an older row): carry on.
            return await continue_with_target(session, ctx, existing)
        if existing.state == S.CANDIDATES_READY:
            return await decide(session, ctx, existing)
        # AWAITING_APPROVAL stays waiting: a repeated form submit must not count as the
        # human approval that timid mode promises.
        return existing

    acq = Acquisition(album_target=target, signal=signal, state=S.MANUAL, origin=Origin.MANUAL)
    session.add(acq)
    session.flush()
    transition(
        session,
        acq,
        S.RESOLVED,
        f"resolved to {rg.artist_name} - {rg.title} ({rg.primary_type}, {rg.first_release_date})",
        data={
            "release_group_mbid": rg.id,
            "preferred_release_mbid": resolution.preferred_release_mbid,
            "candidates": candidates,
            "signal_id": signal.id,
        },
    )
    return await continue_with_target(session, ctx, acq)


def failure_retryable(acq: Acquisition) -> bool:
    """Did the failure come from something transient (MusicBrainz or a tracker being down)
    rather than a verdict (no such album)? The FAILED transition says so in its data."""
    for event in reversed(acq.events):
        if event.data.get("to") == S.FAILED:
            return bool(event.data.get("retryable"))
    return False


def can_retry_request(acq: Acquisition) -> bool:
    """A request that failed before resolving, for a transient reason, can be re-run as it
    was asked. A "no such album" verdict cannot: the fix is a different spelling or an MBID."""
    return (
        acq.state == S.FAILED
        and acq.album_target is None
        and acq.signal is not None
        and failure_retryable(acq)
    )


def continued_as(acq: Acquisition) -> int | None:
    """The acquisition that took over from this one (a retry, or the in-flight acquisition
    a reviewed request was folded into)."""
    for event in reversed(acq.events):
        if event.data.get("continued_as"):
            return int(event.data["continued_as"])
    return None


async def retry_request(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    """Re-run a manual request that failed before it resolved for a transient reason. A new
    acquisition is created from the original signal's request; the failed one is closed and
    points at its successor."""
    signal = acq.signal
    if not can_retry_request(acq) or signal is None:
        raise ValueError(
            "only a request that failed before resolving, for a transient reason, can be "
            "retried; for a 'no such album' result change the spelling or give an MBID"
        )
    new = await submit_manual_request(
        session,
        ctx,
        artist=signal.requested_artist,
        title=signal.requested_title,
        mbid=signal.requested_mbid,
    )
    transition(
        session,
        acq,
        S.CANCELLED,
        f"retried as acquisition {new.id}",
        data={"continued_as": new.id},
    )
    session.commit()
    return new


async def resolve_manually(
    session: Session, ctx: Context, acq: Acquisition, release_group_mbid: str
) -> Acquisition:
    """A human picked the release group for a request that needed review."""
    if acq.state != S.NEEDS_REVIEW:
        raise ValueError(f"cannot pick a release group from state {acq.state}")
    rg = await ctx.musicbrainz.release_group(release_group_mbid)  # NotFound propagates
    target = upsert_target(session, rg, None)
    signal = acq.signal
    if signal is not None:
        signal.album_target = target
        signal.resolution_status = ResolutionStatus.RESOLVED
        signal.resolution_note = f"{rg.artist_name} - {rg.title} (chosen by hand)"
    existing = _active_acquisition(session, target)
    if existing is not None and existing.id != acq.id:
        transition(
            session,
            acq,
            S.CANCELLED,
            f"superseded by acquisition {existing.id}, which already covers "
            f"{rg.artist_name} - {rg.title}",
            data={"continued_as": existing.id},
        )
        session.commit()
        return existing
    acq.album_target = target
    transition(
        session,
        acq,
        S.RESOLVED,
        f"resolved by hand to {rg.artist_name} - {rg.title} ({rg.primary_type}, "
        f"{rg.first_release_date})",
        data={"release_group_mbid": rg.id},
    )
    return await continue_with_target(session, ctx, acq)


async def continue_with_target(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    """From RESOLVED: library check, then search and the approval gate."""
    target = acq.album_target
    assert target is not None
    rg_releases = list((target.release_mbids or {}).get("ids") or [])
    session.commit()  # release the write lock before asking beets
    check = await check_library(
        ctx.beets,
        release_group_mbid=target.release_group_mbid,
        release_mbids=rg_releases,
        artist=target.artist_name,
        title=target.title,
        year=target.first_release_year,
    )
    target.library_status = check.status
    target.library_checked_at = utcnow()
    albums = [a.model_dump() for a in check.albums]
    if check.owned:
        lossy = check.status == LibraryStatus.OWNED_LOSSY
        transition(
            session,
            acq,
            S.ALREADY_OWNED,
            "already in the library"
            + (" (lossy only; upgrade path arrives in v1.5)" if lossy else "")
            + f" [matched by {check.matched_by}]",
            data={"albums": albums, "matched_by": check.matched_by, "lossy": lossy},
        )
    else:
        session.add(Event(acquisition=acq, message="not in the library; ready for search"))
        session.commit()
        if ctx.prowlarr is not None:
            return await search_and_decide(session, ctx, acq)
        session.add(Event(acquisition=acq, message="Prowlarr not configured; search skipped"))
    session.commit()
    return acq
