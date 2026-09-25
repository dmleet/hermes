"""Discovery: ListenBrainz playlists -> Signals -> AlbumTargets -> Acquisitions (docs/plan.md 5.1,
5.2 and the dedupe rules in section 4), plus the periodic re-search of NO_MATCH rows.

``tick()`` runs daily (and once at startup as the reconcile). It is idempotent: a playlist
is recorded once (``Playlist`` row), every track becomes one ``Signal`` (unique per
playlist and position), and a resolved signal either joins the acquisition already in
flight for its album or starts a new one that runs the library check, the search and
the approval gate exactly as a manual request does. An ``ignore`` playlist is recorded
with no tracks fetched. Nothing here spends ratio: every path to a grab goes through
``approval.decide`` and its ``dry_run``/``timid`` gates.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hermes.config import PlaylistMode
from hermes.domain.models import (
    Acquisition,
    AlbumTarget,
    Event,
    Origin,
    Playlist,
    PlaylistStatus,
    ResolutionStatus,
    Signal,
    SignalKind,
    utcnow,
)
from hermes.domain.state import TERMINAL, transition
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.listenbrainz import ListenBrainzError, Track
from hermes.integrations.listenbrainz import Playlist as LBPlaylist
from hermes.integrations.musicbrainz import NotFound
from hermes.services.context import Context
from hermes.services.pipeline import search_and_decide
from hermes.services.requests import continue_with_target, failure_retryable, upsert_target
from hermes.services.resolution import resolve_recording

log = logging.getLogger("hermes.discovery")

# The scheduler's startup run and a manual tick (CLI, API) can coincide; a playlist half
# ingested by one must not be picked up by the other, so discovery is serialised per process.
_lock = asyncio.Lock()
# A row still SEARCHING after this long was cut off mid-search; re-search picks it up.
STRANDED_SEARCH = timedelta(hours=1)


class UpstreamUnavailable(Exception):
    """A dependency (MusicBrainz, beets) is not answering. The playlist stops here with its
    remaining tracks still pending and the next tick resumes it; nothing is marked failed
    for a reason that will have passed by then."""


# Terminal outcomes that mean "do not acquire this album again" (section 4). CANCELLED is
# not among them: a cancelled acquisition was a human saying "not now", not "never".
_NOT_REACQUIRED = frozenset({S.ALREADY_OWNED, S.IMPORTED, S.REJECTED})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def tick(session: Session, ctx: Context) -> dict[str, int]:
    """Look at the generated playlists for the configured user plus ``extra_playlists``;
    ingest whatever is new. Returns counts by outcome."""
    async with _lock:
        return await _tick(session, ctx)


async def _tick(session: Session, ctx: Context) -> dict[str, int]:
    lb = ctx.listenbrainz
    if lb is None:
        return {}
    pol = ctx.policy.listenbrainz
    counts: Counter[str] = Counter()
    wanted: list[tuple[str, PlaylistMode, LBPlaylist | None]] = []  # mbid, mode, listing entry
    if pol.user:
        try:
            listing = await lb.created_for(pol.user)
        except ListenBrainzError as exc:
            log.warning("could not list playlists for %s: %s", pol.user, exc)
            return {"listing_failed": 1}
        for entry in listing:
            # A patch the policy does not name is ignored, never acquired by surprise.
            mode = pol.playlists.get(entry.source or "", "ignore")
            wanted.append((entry.mbid, mode, entry))
    for mbid, mode in pol.extra_playlists.items():
        wanted.append((mbid, mode, None))

    for mbid, mode, listed in wanted:
        known = session.scalar(select(Playlist).where(Playlist.mbid == mbid))
        if (
            known is not None
            and known.mode == mode
            and known.status in (PlaylistStatus.INGESTED, PlaylistStatus.IGNORED)
        ):
            counts["unchanged"] += 1
            continue
        row = await _ingest(session, ctx, mbid, mode=mode, listed=listed)
        counts[str(row.status)] += 1
    return dict(counts)


async def ingest_playlist(
    session: Session,
    ctx: Context,
    mbid: str,
    *,
    mode: PlaylistMode,
    listed: LBPlaylist | None = None,
) -> Playlist:
    """Record one playlist and, unless ignored, every track on it. Safe to call again: a
    track already recorded is skipped, one recorded but not yet resolved (an interrupted
    run) is picked up where it stopped."""
    async with _lock:
        return await _ingest(session, ctx, mbid, mode=mode, listed=listed)


async def _ingest(
    session: Session,
    ctx: Context,
    mbid: str,
    *,
    mode: PlaylistMode,
    listed: LBPlaylist | None = None,
) -> Playlist:
    lb = ctx.listenbrainz
    if lb is None:
        raise ValueError("ListenBrainz is not configured")
    row = session.scalar(select(Playlist).where(Playlist.mbid == mbid))
    if row is None:
        row = Playlist(
            mbid=mbid,
            title=listed.title if listed else "",
            source=(listed.source if listed and listed.source else "extra"),
            created_for=listed.created_for if listed else None,
            lb_created_at=listed.created_at if listed else None,
            mode=mode,
        )
        session.add(row)
    row.mode = mode
    if mode == "ignore":
        row.status = PlaylistStatus.IGNORED
        row.note = "ignored by policy; tracks not fetched"
        session.commit()
        return row
    row.status = PlaylistStatus.PARTIAL
    session.commit()  # release the write lock before the network call

    try:
        playlist = await lb.playlist(mbid)
    except (ListenBrainzError, ValueError) as exc:  # ValueError: a body that is not JSON
        row.status = PlaylistStatus.FAILED
        row.note = str(exc)
        session.commit()
        return row
    row.title = playlist.title or row.title
    row.source = playlist.source or row.source
    row.created_for = playlist.created_for or row.created_for
    row.lb_created_at = playlist.created_at or row.lb_created_at
    row.track_count = len(playlist.tracks)
    session.commit()

    existing = {
        s.position: s
        for s in session.scalars(select(Signal).where(Signal.source_playlist_mbid == mbid))
    }
    summary: Counter[str] = Counter(row.summary or {})
    for track in playlist.tracks:
        signal = existing.get(track.position)
        if signal is None:
            signal = Signal(
                kind=SignalKind.LISTENBRAINZ,
                recording_mbid=track.recording_mbid,
                source_playlist_mbid=mbid,
                source_playlist_name=row.title,
                position=track.position,
                requested_artist=track.artist or None,
                requested_title=track.title or None,
            )
            session.add(signal)
            session.commit()
        elif signal.resolution_status != ResolutionStatus.PENDING:
            continue
        try:
            outcome = await _process(session, ctx, signal, track, row)
        except UpstreamUnavailable as exc:
            session.rollback()
            row.status = PlaylistStatus.PARTIAL
            row.note = f"stopped at track {track.position + 1}: {exc}; resumes on the next tick"
            session.commit()
            log.warning("playlist %s: %s", row.title or mbid, row.note)
            return row
        except Exception:  # noqa: BLE001 - one odd track must not stop the playlist
            # Left PENDING: the playlist stays PARTIAL and the next poll tries this track
            # again, after the rest of the playlist has gone through.
            session.rollback()
            log.exception("playlist %s: track %s failed", row.title or mbid, track.position + 1)
            summary["errors"] += 1
            continue
        summary[outcome] += 1
        row.summary = dict(summary)
        session.commit()

    still_pending = session.scalar(
        select(func.count())
        .select_from(Signal)
        .where(
            Signal.source_playlist_mbid == mbid,
            Signal.resolution_status == ResolutionStatus.PENDING,
        )
    )
    if still_pending:
        row.status = PlaylistStatus.PARTIAL
        row.note = f"{still_pending} track(s) still pending; resumes on the next tick"
    else:
        row.status = PlaylistStatus.INGESTED
        row.ingested_at = utcnow()
        row.note = None
    session.commit()
    return row


async def _process(
    session: Session, ctx: Context, signal: Signal, track: Track, playlist: Playlist
) -> str:
    """Resolve one track's recording to an album and act on it. Returns an outcome label
    for the playlist summary. The signal's resolution and its acquisition are committed
    together, so a crash in between cannot leave a resolved signal with no acquisition
    (the signal would still be pending and the next tick would redo it)."""
    if not signal.recording_mbid:
        signal.resolution_status = ResolutionStatus.FAILED
        signal.resolution_note = "track has no recording MBID"
        session.commit()
        return "no_mbid"

    # The same recording seen before (another week, another playlist) resolves the same
    # way; do not ask MusicBrainz again.
    prior = session.scalar(
        select(Signal)
        .where(
            Signal.recording_mbid == signal.recording_mbid,
            Signal.id != signal.id,
            Signal.album_target_id.is_not(None),
        )
        .order_by(Signal.id.desc())
    )
    candidates: list[dict[str, object]] = []
    if prior is not None and prior.album_target is not None:
        target = prior.album_target
        note = f"same recording as signal {prior.id}"
    else:
        session.commit()  # no write lock across the MusicBrainz calls
        try:
            resolution = await resolve_recording(
                ctx.musicbrainz, ctx.policy.resolution, signal.recording_mbid
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"MusicBrainz {type(exc).__name__}") from exc
        candidates = [c.as_dict() for c in resolution.candidates]
        if resolution.outcome == "not_found":
            signal.resolution_status = ResolutionStatus.FAILED
            signal.resolution_note = resolution.note
            session.commit()
            return "not_found"
        if resolution.outcome != "resolved" or resolution.target is None:
            signal.resolution_status = ResolutionStatus.IGNORED
            signal.resolution_note = resolution.note
            session.commit()
            return "nothing_acquirable"
        found = session.scalar(
            select(AlbumTarget).where(AlbumTarget.release_group_mbid == resolution.target.id)
        )
        if found is None:
            # The recording lookup lists only the releases carrying that recording; the
            # target wants the whole group (every release, for the library check).
            try:
                rg = await ctx.musicbrainz.release_group(resolution.target.id)
            except NotFound:
                rg = resolution.target
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(f"MusicBrainz {type(exc).__name__}") from exc
            found = upsert_target(session, rg, None)
        target = found
        note = f"{target.artist_name} - {target.title} ({target.primary_type})"
    signal.album_target = target
    signal.resolution_status = ResolutionStatus.RESOLVED
    signal.resolution_note = note

    where = f"{playlist.title or playlist.mbid} #{track.position + 1}"
    acquisitions = sorted(target.acquisitions, key=lambda a: a.id, reverse=True)
    active = next((a for a in acquisitions if a.state not in TERMINAL), None)
    if active is not None:
        session.add(
            Event(
                acquisition=active,
                message=f"discovered again in {where}",
                data={"signal_id": signal.id, "state": active.state},
            )
        )
        session.commit()
        return "attached"
    done = next((a for a in acquisitions if a.state in _NOT_REACQUIRED), None)
    if done is not None:
        signal.resolution_note = f"{note}; not re-acquired: #{done.id} is {done.state}"
        session.commit()
        return "not_reacquired"

    acq = Acquisition(album_target=target, signal=signal, state=S.DISCOVERED, origin=Origin.AUTO)
    session.add(acq)
    session.flush()
    transition(
        session,
        acq,
        S.RESOLVED,
        f"discovered in {where}: {track.artist} - {track.title} is on "
        f"{target.artist_name} - {target.title} ({target.primary_type}, "
        f"{target.first_release_year or '?'})",
        data={
            "signal_id": signal.id,
            "recording_mbid": signal.recording_mbid,
            "release_group_mbid": target.release_group_mbid,
            "playlist_mbid": playlist.mbid,
            "candidates": candidates,
        },
    )
    session.commit()  # signal, acquisition and its first event land together
    try:
        acq = await continue_with_target(session, ctx, acq)
    except httpx.HTTPError as exc:
        # The row is RESOLVED and the daily re-search finishes it (library check, search).
        raise UpstreamUnavailable(f"beets or Prowlarr {type(exc).__name__}") from exc
    return acq.state.lower()


async def research_tick(session: Session, ctx: Context) -> dict[str, int]:
    """Search again for albums the tracker did not have last time (``NO_MATCH``), once
    ``search.retry_days`` have passed, until ``search.max_retries`` searches have been
    spent; then give up (``REJECTED``, so a later signal does not revive it but a manual
    request still can). Also finishes ``RESOLVED`` rows that never got their library check
    and search (a dependency was down), and retries ``FAILED`` rows whose failure was
    transient and that have no download yet. Serialised with discovery: both act on the
    same rows."""
    async with _lock:
        return await _research(session, ctx)


async def _research(session: Session, ctx: Context) -> dict[str, int]:
    if ctx.prowlarr is None:
        return {}
    pol = ctx.policy.search
    cutoff = utcnow() - timedelta(days=pol.retry_days)
    counts: Counter[str] = Counter()
    rows = session.scalars(
        select(Acquisition)
        .where(Acquisition.state.in_([S.NO_MATCH, S.RESOLVED, S.FAILED, S.SEARCHING]))
        .order_by(Acquisition.id)
    ).all()
    for acq in rows:
        # The list was built before the network waits below; another actor (a click, the
        # discovery tick) may have moved this row since. Act on what is in the database.
        session.refresh(acq)
        state = acq.state
        if state == S.SEARCHING:
            # A search takes seconds; one this old was cut off (a restart mid-search) and
            # nothing else would ever move it on.
            if _aware(acq.updated_at) > utcnow() - STRANDED_SEARCH:
                continue
            transition(
                session,
                acq,
                S.FAILED,
                "search was interrupted (Hermes restarted mid-search?); searching again",
                data={"retryable": True},
                level="warning",
            )
            session.commit()
            state = S.FAILED
        if state == S.NO_MATCH:
            if _aware(acq.updated_at) > cutoff:
                counts["waiting"] += 1
                continue
            if acq.search_retries >= pol.max_retries:
                transition(
                    session,
                    acq,
                    S.REJECTED,
                    f"gave up: no acceptable release after {acq.search_retries} searches "
                    f"(search.max_retries)",
                )
                session.commit()
                counts["gave_up"] += 1
                continue
        elif state == S.RESOLVED:
            if acq.search_retries > 0:
                continue  # searched before: mid-flight, not ours
        elif state == S.FAILED:
            if not failure_retryable(acq) or acq.grab_attempts:
                continue  # a verdict, or a failure past the download (the importer's)
        else:
            counts["moved"] += 1
            continue
        session.commit()
        try:
            if state == S.RESOLVED:
                await continue_with_target(session, ctx, acq)
            else:
                await search_and_decide(session, ctx, acq)
        except Exception as exc:  # noqa: BLE001 - one row must not stop the others
            session.rollback()
            log.warning("re-search of acquisition %s failed: %s", acq.id, exc)
            counts["failed"] += 1
            continue
        counts[acq.state.lower()] += 1
    return dict(counts)
