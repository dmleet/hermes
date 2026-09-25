"""Import stage: hand a completed download to beets through the agent (docs/plan.md 5.9).

    READY_FOR_BEETS -> IMPORTING -> IMPORTED | IMPORT_NEEDS_REVIEW

The agent runs ``beet import -q -I --set hermes_acquisition=<id> [--search-id <release>]``.
Success is verified independently of the exit code by asking the library for albums
carrying the acquisition id. Anything else (skip, timeout, beets refusing the config, a path
beets cannot see) lands in IMPORT_NEEDS_REVIEW or FAILED with the evidence on the event.

Navidrome, when configured, is asked to scan once per drained queue rather than once per
album, and no import starts while it is scanning: beets writes each copied file several
times (tags, scrub, embedded art), and a scan that walks the folder meanwhile records a
half-written track and, with the source mtimes preserved, never re-reads it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.domain.models import (
    Acquisition,
    AlbumTarget,
    Event,
    GrabAttempt,
    GrabOutcome,
    LibraryStatus,
    utcnow,
)
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.integrations.musicbrainz import NotFound, ReleaseGroup
from hermes.integrations.navidrome import NavidromeError
from hermes.services.context import Context

log = logging.getLogger("hermes.importer")

# The scheduler's startup run and a manual tick (CLI, API) can coincide; the scan gate below
# reasons about "nothing is importing", which only holds if one tick runs at a time.
_lock = asyncio.Lock()

SCAN_REQUESTED = "Navidrome scan requested"

# The same order as beets' `match.preferred.countries` in deploy/beets/config.yaml, so a
# manual import and a Hermes import of the same rip tend to land on the same release.
PREFERRED_COUNTRIES = ("XW", "US", "GB")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_time(value: Any) -> datetime | None:
    """An ISO timestamp from the agent, or None."""
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def completed_attempt(acq: Acquisition) -> GrabAttempt | None:
    for attempt in sorted(acq.grab_attempts, key=lambda a: a.id, reverse=True):
        if attempt.outcome == GrabOutcome.COMPLETED and attempt.completed_path:
            return attempt
    return None


@dataclass
class DownloadShape:
    """What the downloaded files look like, from the torrent Hermes kept: the number a
    release's track list must agree with, how the tracks split across directories (one
    per disc in a multi-disc rip), and the medium when it can be told."""

    track_count: int | None = None
    layout: list[int] = field(default_factory=list)
    media: str | None = None  # "CD", "WEB", ... as the title parser names them

    def as_dict(self) -> dict[str, Any]:
        return {"audio_files": self.track_count, "layout": self.layout, "media": self.media}


# The title parser's media names against MusicBrainz medium formats.
_MEDIA_FORMATS = {
    "CD": ("CD", "Enhanced CD", "HDCD", "Copy Control CD", "SACD", "Hybrid SACD"),
    "WEB": ("Digital Media",),
    "Vinyl": ('12" Vinyl', '7" Vinyl', '10" Vinyl', "Vinyl"),
    "SACD": ("SACD", "Hybrid SACD"),
    "DVD": ("DVD", "DVD-Audio", "DVD-Video"),
    "Blu-Ray": ("Blu-ray",),
}


def pick_release(
    rg: ReleaseGroup, hint_year: int | None, shape: DownloadShape | None = None
) -> str | None:
    """The release beets should be told about. beets in quiet mode skips anything but a
    strong match against the release it is given, so the choice follows the files: official;
    the download's track count (a group can hold a 9-track "Ghosts I" beside the 36-track
    album); the same disc layout (two directories of 18 is a two-CD release, not the single
    36-track download, which beets scores as a track-order mismatch); the same medium (a
    rip with a log and cue sheet is a CD, not the digital release); then the year, a
    worldwide/major country, and a plain release over a disambiguated variant."""
    releases = list(rg.releases)
    if not releases:
        return None
    shape = shape or DownloadShape()
    wanted_formats = _MEDIA_FORMATS.get(shape.media or "", ())

    def key(r: object) -> tuple[int, int, int, int, int, int, int]:
        status_ok = 0 if getattr(r, "status", None) == "Official" else 1
        count = getattr(r, "track_count", None)
        tracks_ok = (
            0 if shape.track_count is None or count is None or count == shape.track_count else 1
        )
        media_counts = list(getattr(r, "media_track_counts", None) or [])
        layout_ok = (
            0 if len(shape.layout) < 2 or not media_counts or media_counts == shape.layout else 1
        )
        formats = getattr(r, "formats", None) or []
        media_ok = (
            0
            if not wanted_formats or not formats or any(f in wanted_formats for f in formats)
            else 1
        )
        year = getattr(r, "date", None) or ""
        year_ok = 0 if hint_year and year[:4] == str(hint_year) else 1
        country = getattr(r, "country", None) or ""
        country_rank = PREFERRED_COUNTRIES.index(country) if country in PREFERRED_COUNTRIES else 9
        plain = 0 if not getattr(r, "disambiguation", None) else 1
        return (status_ok, tracks_ok, layout_ok, media_ok, year_ok, country_rank, plain)

    return str(min(releases, key=key).id)


def download_shape(attempt: GrabAttempt, parsed_media: str | None) -> DownloadShape:
    """The files' shape as recorded at submit time (Hermes keeps no torrent file). The
    media comes from the release title when the tracker names it, else from the files."""
    recorded = attempt.download_shape or {}
    media = parsed_media or ("CD" if recorded.get("cd_rip") else None)
    return DownloadShape(
        track_count=recorded.get("audio_files") or None,
        layout=list(recorded.get("layout") or []),
        media=media,
    )


async def choose_search_id(
    ctx: Context, target: AlbumTarget, hint_year: int | None, shape: DownloadShape | None = None
) -> str | None:
    """The release to pin the import to, or None when the group has no release to pick.
    Raises ``httpx.HTTPError`` and ``NotFound`` from MusicBrainz: an import is never run
    unpinned for want of an answer (the caller waits, or asks a human)."""
    if target.preferred_release_mbid:
        return target.preferred_release_mbid
    rg = await ctx.musicbrainz.release_group(target.release_group_mbid)
    return pick_release(rg, hint_year or target.first_release_year, shape)


def _note_once(session: Session, acq: Acquisition, message: str, level: str = "warning") -> None:
    """Add an event unless the last one already says the same thing (poll loops)."""
    if acq.events and acq.events[-1].message == message:
        return
    session.add(Event(acquisition=acq, level=level, message=message))


async def start_import(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    if acq.state not in (S.READY_FOR_BEETS, S.IMPORT_NEEDS_REVIEW):
        raise ValueError(f"cannot import from state {acq.state}")
    attempt = completed_attempt(acq)
    target = acq.album_target
    if attempt is None or target is None:
        transition(session, acq, S.FAILED, "no completed download to import")
        session.commit()
        return acq

    health = await ctx.beets.health()
    if not health.ok:
        _note_once(session, acq, f"beets agent not ready: {health.detail}")
        session.commit()
        return acq

    beets_path = ctx.policy.paths.to_beets(attempt.completed_path or "")
    hint_year = None
    parsed_media = None
    candidate = next((c for c in acq.candidates if c.id == attempt.candidate_id), None)
    if candidate:
        parsed = candidate.parsed_quality.get("parsed") or {}
        # A remaster's files are the remaster: steer beets to that release, not the original.
        hint_year = parsed.get("remaster_year") or parsed.get("year")
        parsed_media = parsed.get("media")
    shape = download_shape(attempt, parsed_media)
    # Without a pinned release beets would search, and the import overlay (which drops the
    # fields that order candidates) would not apply: a MusicBrainz outage waits for the
    # next tick instead, and a group with nothing to pick goes to a human.
    try:
        search_id = await choose_search_id(ctx, target, hint_year, shape)
    except httpx.HTTPError as exc:
        _note_once(
            session,
            acq,
            f"MusicBrainz unavailable ({type(exc).__name__}); the import waits until it can "
            "pin the release",
        )
        session.commit()
        return acq
    except NotFound:
        search_id = None
    if search_id is None:
        message = (
            f"no release to pin in MusicBrainz release group {target.release_group_mbid} "
            "(merged or emptied?); import by hand"
        )
        session.refresh(acq, attribute_names=["state"])
        if acq.state == S.READY_FOR_BEETS:
            transition(session, acq, S.IMPORT_NEEDS_REVIEW, message, level="warning")
        else:
            _note_once(session, acq, message)
        session.commit()
        return acq

    # Another actor may have started this import while we were talking to MusicBrainz.
    session.refresh(acq, attribute_names=["state"])
    if acq.state not in (S.READY_FOR_BEETS, S.IMPORT_NEEDS_REVIEW):
        return acq

    try:
        job_id = await ctx.beets.submit_import(
            beets_path, acq.id, search_id, target.release_group_mbid
        )
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300]
        if exc.response.status_code == 409:
            transition(
                session,
                acq,
                S.FAILED,
                f"beets refuses to import: unsafe import config ({body})",
                data={"retryable": True, "beets_path": beets_path},
            )
        elif exc.response.status_code == 404:
            message = f"beets cannot see {beets_path} (check paths.deluge_root/beets_root)"
            if acq.state == S.IMPORT_NEEDS_REVIEW:
                _note_once(session, acq, message)
            else:
                transition(
                    session,
                    acq,
                    S.IMPORT_NEEDS_REVIEW,
                    message,
                    data={"beets_path": beets_path, "completed_path": attempt.completed_path},
                    level="warning",
                )
        else:
            _note_once(session, acq, f"beets agent error {exc.response.status_code}: {body}")
        session.commit()
        return acq
    except httpx.HTTPError as exc:
        _note_once(session, acq, f"beets agent unreachable: {exc}")
        session.commit()
        return acq

    session.refresh(acq, attribute_names=["state"])
    if acq.state not in (S.READY_FOR_BEETS, S.IMPORT_NEEDS_REVIEW):
        session.add(
            Event(
                acquisition=acq,
                level="warning",
                message=f"beets job {job_id} started but the acquisition is already {acq.state}",
            )
        )
        session.commit()
        return acq

    attempt.import_job_id = job_id
    attempt.import_started_at = utcnow()
    transition(
        session,
        acq,
        S.IMPORTING,
        f"beets import started (job {job_id}) for {beets_path}"
        + (f" with release {search_id}" if search_id else " without a release hint"),
        data={
            "job_id": job_id,
            "beets_path": beets_path,
            "search_id": search_id,
            **shape.as_dict(),
        },
    )
    session.commit()
    return acq


async def poll_import(
    session: Session, ctx: Context, acq: Acquisition, now: datetime
) -> Acquisition:
    attempt = completed_attempt(acq)
    if attempt is None or not attempt.import_job_id:
        transition(session, acq, S.IMPORT_NEEDS_REVIEW, "importing but no job is recorded")
        session.commit()
        return acq
    try:
        job = await ctx.beets.job(attempt.import_job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            transition(
                session,
                acq,
                S.IMPORT_NEEDS_REVIEW,
                f"beets agent no longer knows job {attempt.import_job_id} (agent restarted?)",
                level="warning",
            )
            session.commit()
        return acq
    except httpx.HTTPError:
        return acq  # transient; next tick

    if job.get("status") != "finished":
        if job.get("status") == "queued":
            # Waiting behind other imports on the agent's single worker: not running yet,
            # so not late. The agent's own subprocess timeout bounds each job it runs.
            return acq
        started = _aware(
            _parse_time(job.get("started_at"))
            or attempt.import_started_at
            or attempt.completed_at
            or attempt.added_at
        )
        timeout = timedelta(seconds=ctx.policy.beets.import_timeout_seconds)
        if _aware(now) - started > timeout:
            transition(
                session,
                acq,
                S.IMPORT_NEEDS_REVIEW,
                f"beets import still running after {timeout}; check the agent",
                data={"job_id": attempt.import_job_id},
                level="warning",
            )
            session.commit()
        return acq

    try:
        albums = await ctx.beets.albums_for_acquisition(acq.id)
    except httpx.HTTPError:
        return acq  # the library is busy (an import committing) or the agent is away
    evidence = {
        "job_id": attempt.import_job_id,
        "exit_code": job.get("exit_code"),
        "error": job.get("error"),
        "log_tail": job.get("log_tail", [])[-20:],
        "import_log_tail": job.get("import_log_tail", [])[-20:],
    }
    # The agent refused before running beets: the files' own MusicBrainz tags name another
    # album, so nothing was imported and there is no library entry to check.
    if job.get("refused"):
        transition(
            session,
            acq,
            S.IMPORT_NEEDS_REVIEW,
            f"beets import refused: {job.get('error')}; if these are the right files, "
            "import them by hand",
            data=evidence,
            level="warning",
        )
        session.commit()
        return acq
    # beets adds the album to its database (with our --set field) before it copies the
    # files, so an interrupted or failed import can leave a library entry that points at
    # the seeded files. Never call that done.
    if job.get("error") or job.get("exit_code") not in (0,):
        transition(
            session,
            acq,
            S.IMPORT_NEEDS_REVIEW,
            f"beets import did not finish cleanly (exit {job.get('exit_code')}, "
            f"{job.get('error') or 'no error text'}); check the library entry",
            data=evidence,
            level="warning",
        )
        session.commit()
        return acq
    if not albums:
        transition(
            session,
            acq,
            S.IMPORT_NEEDS_REVIEW,
            "beets finished but no album carries this acquisition (quiet mode probably skipped it)",
            data=evidence,
            level="warning",
        )
        session.commit()
        return acq
    # The import was pinned to a release in the target's group, so an album from another
    # group means beets did not import what was asked (a hand import of the wrong album,
    # or a free search). Say so rather than mark the album owned.
    target = acq.album_target
    wanted_group = target.release_group_mbid if target is not None else None
    foreign = [
        a
        for a in albums
        if wanted_group and a.mb_releasegroupid and a.mb_releasegroupid != wanted_group
    ]
    if foreign:
        transition(
            session,
            acq,
            S.IMPORT_NEEDS_REVIEW,
            f"beets imported {foreign[0].albumartist} - {foreign[0].album} from release group "
            f"{foreign[0].mb_releasegroupid}, not the target's {wanted_group}",
            data={**evidence, "mb_albumid": foreign[0].mb_albumid},
            level="warning",
        )
        session.commit()
        return acq
    library_root = ctx.policy.beets.library_root.rstrip("/")
    outside = [a.path for a in albums if not (a.path or "").startswith(library_root + "/")]
    if outside:
        transition(
            session,
            acq,
            S.IMPORT_NEEDS_REVIEW,
            f"beets library entry points outside {library_root}: {outside[0]} "
            + (
                "(a relative path: the beets agent is older than this beets and did not "
                "bind the music directory; update beets-hermes)"
                if not (outside[0] or "").startswith("/")
                else "(interrupted copy?); do not touch the seeded files"
            ),
            data={**evidence, "paths": outside},
            level="warning",
        )
        session.commit()
        return acq

    if target is not None:
        target.library_status = LibraryStatus.OWNED
        target.library_checked_at = now
    summary = [
        {
            "album": f"{a.albumartist} - {a.album}",
            "path": a.path,
            "items": a.quality.items,
            "formats": a.quality.formats,
            "mb_albumid": a.mb_albumid,
        }
        for a in albums
    ]
    transition(
        session,
        acq,
        S.IMPORTED,
        f"imported: {summary[0]['album']} ({summary[0]['items']} items) at {summary[0]['path']}",
        data={"job_id": attempt.import_job_id, "albums": summary},
    )
    session.commit()

    # Hermes imports run with -I and never record themselves in beets' incremental history;
    # record the folder now so the user's own `beet import` over the downloads directory
    # skips it instead of stopping at a duplicate prompt. Best effort: the import is done.
    beets_path = ctx.policy.paths.to_beets(attempt.completed_path or "")
    try:
        recorded = await ctx.beets.mark_imported(beets_path)
        session.add(
            Event(
                acquisition=acq,
                message=f"recorded in beets' incremental history ({recorded} folder group(s))",
                data={"beets_path": beets_path, "recorded": recorded},
            )
        )
    except httpx.HTTPError as exc:
        session.add(
            Event(
                acquisition=acq,
                level="warning",
                message=f"could not record {beets_path} in beets' incremental history: "
                f"{type(exc).__name__}; a manual `beet import` will meet it as a duplicate",
            )
        )
    session.commit()

    if _scan_wanted(ctx):
        if ctx.navidrome_scan_owed is None:
            ctx.navidrome_scan_owed = _owed_before_this_process(session)
        ctx.navidrome_scan_owed.append(acq.id)
    return acq


# A Navidrome scan that reports "scanning" for longer than this (a scanner wedged on a read)
# no longer holds imports: they go ahead, each with a note, rather than wait forever.
SCAN_HOLD_LIMIT = timedelta(hours=2)


def _scan_wanted(ctx: Context) -> bool:
    return ctx.navidrome is not None and ctx.policy.navidrome.trigger_scan


def _owed_before_this_process(session: Session) -> list[int]:
    """Imports verified before this process started that no scan request followed (Hermes
    restarted between the import and the drain): they are owed a scan too."""
    last_scan = session.scalar(
        select(Event.at).where(Event.message.like(f"{SCAN_REQUESTED}%")).order_by(Event.at.desc())
    )
    query = select(Acquisition.id).where(Acquisition.state == S.IMPORTED)
    if last_scan is not None:
        query = query.where(Acquisition.updated_at > last_scan)
    return list(session.scalars(query.order_by(Acquisition.id)))


async def navidrome_scanning(
    session: Session, ctx: Context, waiting: Sequence[Acquisition]
) -> bool:
    """Whether a Navidrome scan is running, in which case ``waiting`` (ready to import) are
    left for the next tick with a note. Navidrome being unreachable never holds an import:
    it is optional, and its health check says so."""
    if not _scan_wanted(ctx) or not waiting:
        ctx.navidrome_scanning_since = None  # nothing held, so no hold to time
        return False
    assert ctx.navidrome is not None
    try:
        status = await ctx.navidrome.scan_status()
    except (httpx.HTTPError, NavidromeError) as exc:
        log.warning("navidrome scan status unavailable (%s); not holding imports", exc)
        return False
    if not status.get("scanning"):
        ctx.navidrome_scanning_since = None
        return False
    now = utcnow()
    since = ctx.navidrome_scanning_since = ctx.navidrome_scanning_since or now
    if _aware(now) - _aware(since) > SCAN_HOLD_LIMIT:
        for acq in waiting:
            _note_once(
                session,
                acq,
                f"Navidrome has reported a scan for over {SCAN_HOLD_LIMIT}; importing anyway "
                "(a stuck scanner?); if this album shows up split, run a full scan",
            )
        session.commit()
        return False
    for acq in waiting:
        _note_once(
            session,
            acq,
            "Navidrome is scanning the library; the import waits so the scan cannot read "
            "half-written files",
            level="info",
        )
    session.commit()
    return True


async def request_scan_if_idle(session: Session, ctx: Context) -> str | None:
    """Ask Navidrome for one scan covering every import since the last request, but only
    when nothing is importing. Returns what happened, for the tick's counts."""
    if not _scan_wanted(ctx):
        ctx.navidrome_scan_owed = []
        return None
    if ctx.navidrome_scan_owed is None:
        ctx.navidrome_scan_owed = _owed_before_this_process(session)
    owed = ctx.navidrome_scan_owed
    if not owed:
        return None
    importing = session.scalar(
        select(Acquisition.id).where(Acquisition.state == S.IMPORTING).limit(1)
    )
    if importing is not None:
        return "scan_deferred"
    assert ctx.navidrome is not None
    rows = session.scalars(select(Acquisition).where(Acquisition.id.in_(owed))).all()
    try:
        status = await ctx.navidrome.start_scan()
    except (httpx.HTTPError, NavidromeError) as exc:
        # One warning per import, not one per tick: the nightly scan picks the albums up.
        for acq in rows:
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"Navidrome scan request failed: {exc}",
                )
            )
        session.commit()
        ctx.navidrome_scan_owed = []
        return "scan_failed"
    message = SCAN_REQUESTED + (f" for {len(owed)} imports" if len(owed) > 1 else "")
    for acq in rows:
        session.add(Event(acquisition=acq, message=message, data=status))
    session.commit()
    ctx.navidrome_scan_owed = []
    return "scan_requested"


async def retry_import(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    """From IMPORT_NEEDS_REVIEW, or FAILED with a completed download, try again. A human's
    retry starts beets outside the tick, so it meets the same gate: not under a scan."""
    async with _lock:
        if await navidrome_scanning(session, ctx, [acq]):
            raise ValueError("Navidrome is scanning the library; retry once it has finished")
        if acq.state == S.FAILED and completed_attempt(acq) is not None:
            transition(session, acq, S.READY_FOR_BEETS, "retrying import")
            session.commit()
        return await start_import(session, ctx, acq)


async def _guarded(session: Session, acq: Acquisition, work: Awaitable[Acquisition]) -> bool:
    """Run one row's step; an unexpected error is noted on that row and the tick goes on,
    so one bad row cannot stop every other import, tick after tick."""
    try:
        await work
        return True
    except Exception as exc:  # noqa: BLE001 - per-row isolation
        log.exception("import step failed for acquisition %s", acq.id)
        session.rollback()
        try:
            _note_once(session, acq, f"import step failed: {type(exc).__name__}: {exc}"[:500])
            session.commit()
        except Exception:  # noqa: BLE001 - the database itself may be the problem
            session.rollback()
        return False


async def tick(session: Session, ctx: Context, now: datetime | None = None) -> dict[str, int]:
    """Start imports for downloads that are ready; poll the ones in progress; when the queue
    has drained, ask Navidrome to scan."""
    async with _lock:
        return await _tick(session, ctx, now or utcnow())


async def _tick(session: Session, ctx: Context, now: datetime) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    # Poll what was already running before starting new jobs, so a job started this tick is
    # first looked at next tick (the agent runs them one at a time anyway).
    importing = session.scalars(
        select(Acquisition).where(Acquisition.state == S.IMPORTING).order_by(Acquisition.id)
    ).all()
    for acq in importing:
        if await _guarded(session, acq, poll_import(session, ctx, acq, now)):
            counts["polled"] += 1
    ready = session.scalars(
        select(Acquisition).where(Acquisition.state == S.READY_FOR_BEETS).order_by(Acquisition.id)
    ).all()
    if await navidrome_scanning(session, ctx, ready):
        counts["waiting_for_scan"] = len(ready)
    else:
        for acq in ready:
            if await _guarded(session, acq, start_import(session, ctx, acq)):
                counts["started" if acq.state == S.IMPORTING else "not_started"] += 1
            else:
                counts["errors"] += 1
    # Last, after this tick's starts: the scan goes out only when nothing is importing.
    outcome = await request_scan_if_idle(session, ctx)
    if outcome:
        counts[outcome] += 1
    return dict(counts)
