"""Submit stage: fetch the .torrent through Prowlarr and add it to the routed Deluge
(docs/plan.md 5.7). Hermes owns the association: the infohash is computed locally and checked
against what Deluge reports. The torrent bytes live only in this function: a private
tracker's torrent carries the account's passkey in its announce URL, Deluge keeps the
file for seeding, and Hermes tracks the download by infohash alone. What the importer
later needs from the file (how the tracks are laid out) is read here and stored on the
attempt. A submit interrupted between the Deluge add and the attempt commit is recovered
on the next submit by looking for a torrent at this acquisition's own download path.

Candidates are tried in rank order. A candidate is skipped (with its reason recorded on
the row) when its indexer maps to no Deluge instance or offers no torrent file. When the
torrent fetch itself fails the whole indexer is deferred for this run, since the likely
cause (tokens exhausted on a "Required" indexer, tracker down) affects every release
there; the candidates keep their rank for a later retry. No candidate left means FAILED.
"""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from hermes.bencode import BencodeError, TorrentInfo
from hermes.domain.models import Acquisition, Candidate, Event, GrabAttempt, GrabOutcome, utcnow
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.integrations.deluge import DelugeError
from hermes.services.context import Context
from hermes.services.ranking import torrent_rate_verdict


def attempted_guids(acq: Acquisition) -> set[str]:
    """Torrents already tried, by Prowlarr guid so a re-search cannot re-offer them."""
    by_id = {c.id: c for c in acq.candidates}
    return {
        by_id[a.candidate_id].prowlarr_guid for a in acq.grab_attempts if a.candidate_id in by_id
    }


def next_candidate(acq: Acquisition, skip_indexers: set[str] | None = None) -> Candidate | None:
    """Highest-ranked accepted candidate that has not been tried yet."""
    tried = attempted_guids(acq)
    skip = skip_indexers or set()
    ranked = sorted((c for c in acq.candidates if c.rank is not None), key=lambda c: c.rank or 0)
    return next(
        (c for c in ranked if c.prowlarr_guid not in tried and c.indexer_name not in skip), None
    )


def _skip(session: Session, acq: Acquisition, candidate: Candidate, reason: str) -> None:
    candidate.rejected_reason = f"skipped at submit: {reason}"
    candidate.rank = None
    session.add(
        Event(
            acquisition=acq,
            level="warning",
            message=f"skipping {candidate.title}: {reason}",
            data={"candidate_id": candidate.id},
        )
    )


def _candidate_for(acq: Acquisition, infohash: str) -> Candidate | None:
    """The candidate whose tracker-reported infohash is this torrent's, if the tracker
    reports one (public trackers do; private ones usually do not)."""
    for c in acq.candidates:
        if (c.parsed_quality.get("info_hash") or "").lower() == infohash:
            return c
    return None


async def _adopt_existing(
    session: Session, ctx: Context, acq: Acquisition, pending: str, completed: str
) -> Acquisition | None:
    """Recovery for a submit that died between the Deluge add and the attempt commit: a
    torrent already sitting at this acquisition's directory in any configured Deluge is
    adopted as the attempt, provided a candidate's reported infohash identifies it. When
    none does, the fetch goes ahead: Deluge answers "already in session" for the same
    torrent and the attempt is recorded against the candidate actually fetched."""
    known = {a.infohash for a in acq.grab_attempts}
    for instance, deluge in ctx.deluge.items():
        try:
            found = await deluge.find_at([pending, completed])
        except (httpx.HTTPError, DelugeError):
            continue  # an unreachable instance is the submit's problem, not the adoption's
        for infohash, status in found.items():
            if infohash in known:
                continue
            candidate = _candidate_for(acq, infohash)
            if candidate is None:
                message = (
                    f"torrent {status.get('name')} ({infohash[:12]}) sits at "
                    f"{status.get('download_location') or pending} in Deluge {instance} but "
                    f"matches no candidate's infohash; not adopted"
                )
                if not acq.events or acq.events[-1].message != message:
                    session.add(Event(acquisition=acq, level="warning", message=message))
                    session.commit()
                continue
            session.refresh(acq, attribute_names=["state"])
            if acq.state not in (S.CANDIDATES_READY, S.AWAITING_APPROVAL, S.STALLED, S.FAILED):
                return acq
            attempt = GrabAttempt(
                acquisition=acq,
                candidate_id=candidate.id,
                deluge_instance=instance,
                infohash=infohash,
                torrent_name=str(status.get("name") or ""),
                download_location=pending,
                completed_location=completed,
                last_progress_at=utcnow(),
                last_total_done=0,
                outcome=GrabOutcome.ACTIVE,
            )
            session.add(attempt)
            session.flush()
            acq.active_grab_id = attempt.id
            transition(
                session,
                acq,
                S.SUBMITTED,
                f"adopted {attempt.torrent_name} already in Deluge {instance} at "
                f"{status.get('download_location') or pending} (an earlier submit was "
                f"interrupted after the add)",
                data={"attempt_id": attempt.id, "infohash": infohash, "instance": instance},
                level="warning",
            )
            session.commit()
            return acq
    return None


async def submit(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    if ctx.prowlarr is None:
        raise ValueError("Prowlarr is not configured")
    policy = ctx.policy
    from_state = acq.state
    if from_state not in (S.CANDIDATES_READY, S.AWAITING_APPROVAL, S.STALLED, S.FAILED):
        raise ValueError(f"cannot submit from state {from_state}")

    deferred: dict[str, str] = {}  # indexer -> why its candidates were left for later
    session.commit()  # release the write lock before the network calls below

    pending, completed = policy.deluge.locations(acq.id)
    if policy.deluge.layout == "per_acquisition":
        # In a flat pool the directory is everyone's, so nothing there is ours by location;
        # an interrupted submit is caught by Deluge's "already in session" below instead.
        adopted = await _adopt_existing(session, ctx, acq, pending, completed)
        if adopted is not None:
            return adopted

    while True:
        candidate = next_candidate(acq, skip_indexers=set(deferred))
        if candidate is None:
            if deferred:
                why = "; ".join(f"{ix}: {reason}" for ix, reason in deferred.items())
                transition(
                    session,
                    acq,
                    S.FAILED,
                    f"torrent fetch failed, retry later ({why})",
                    data={"retryable": True, "deferred_indexers": deferred},
                )
            else:
                transition(session, acq, S.FAILED, "no candidate left to submit")
            session.commit()
            return acq

        instance = policy.deluge.instance_for(candidate.indexer_name)
        if instance is None or instance not in ctx.deluge:
            _skip(
                session, acq, candidate, f"no Deluge instance for indexer {candidate.indexer_name}"
            )
            continue
        if not candidate.download_url:
            _skip(session, acq, candidate, "no torrent download URL (magnet-only)")
            continue

        try:
            data = await ctx.prowlarr.fetch_torrent(candidate.download_url)
        except httpx.HTTPError as exc:
            deferred[candidate.indexer_name] = f"{type(exc).__name__}: {exc}"
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"torrent fetch failed on {candidate.indexer_name}; deferring "
                    f"its candidates (tokens exhausted?): {exc}",
                    data={"candidate_id": candidate.id},
                )
            )
            continue
        try:
            torrent = TorrentInfo(data)
        except BencodeError as exc:
            _skip(session, acq, candidate, f"not a torrent file: {exc}")
            continue

        target = acq.album_target
        track_ms = list(((target.track_lengths if target else {}) or {}).get("ms") or [])
        verdict = torrent_rate_verdict(torrent.audio_sizes(), track_ms, policy.quality)
        if verdict:
            _skip(session, acq, candidate, verdict)
            continue
        shape = {
            "audio_files": torrent.audio_file_count,
            "layout": torrent.audio_layout(),
            "cd_rip": torrent.looks_like_cd_rip,
        }
        deluge = ctx.deluge_for(instance)
        try:
            await deluge.ensure_label(policy.deluge.label)
            reported = await deluge.add_torrent(
                f"hermes-{acq.id}.torrent",
                data,
                {
                    "download_location": pending,
                    "move_completed": True,
                    "move_completed_path": completed,
                    "add_paused": False,
                },
            )
        except (httpx.HTTPError, DelugeError) as exc:
            transition(
                session,
                acq,
                S.FAILED,
                f"Deluge {instance} unavailable: {type(exc).__name__}: {exc}",
                data={"retryable": True, "candidate_id": candidate.id},
            )
            session.commit()
            return acq

        # Another actor (a second tick, a second click) may have moved this acquisition
        # while we were talking to Prowlarr and Deluge. Do not double-transition.
        session.refresh(acq, attribute_names=["state"])
        if acq.state not in (S.CANDIDATES_READY, S.AWAITING_APPROVAL, S.STALLED, S.FAILED):
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"torrent {reported} added to Deluge {instance} but the "
                    f"acquisition is already {acq.state}; leaving it",
                )
            )
            session.commit()
            return acq

        if reported != torrent.infohash:
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"Deluge reported hash {reported}, computed {torrent.infohash}",
                )
            )

        # The torrent is live: record it before anything else can fail.
        attempt = GrabAttempt(
            acquisition=acq,
            candidate_id=candidate.id,
            deluge_instance=instance,
            infohash=reported,
            torrent_name=torrent.name,
            download_location=pending,
            completed_location=completed,
            last_progress_at=utcnow(),
            last_total_done=0,
            outcome=GrabOutcome.ACTIVE,
            download_shape=shape,
        )
        session.add(attempt)
        session.flush()
        acq.active_grab_id = attempt.id
        try:
            await deluge.set_label(reported, policy.deluge.label)
        except (httpx.HTTPError, DelugeError) as exc:
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"could not label torrent {reported} in Deluge {instance}: {exc}",
                )
            )
        transition(
            session,
            acq,
            S.SUBMITTED,
            f"added to Deluge {instance}: {torrent.name} ({torrent.total_size / 1e6:.0f} MB, "
            f"{torrent.file_count} files) -> {pending}",
            data={
                "candidate_id": candidate.id,
                "attempt_id": attempt.id,
                "infohash": reported,
                "instance": instance,
                "pending": pending,
                "completed": completed,
                "size": torrent.total_size,
            },
        )
        session.commit()
        return acq
