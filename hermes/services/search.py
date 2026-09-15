"""Search stage: RESOLVED -> SEARCHING -> CANDIDATES_READY | NO_MATCH (docs/plan.md 5.4, 5.5).

Every Prowlarr row is persisted as a Candidate, rejected ones with their reason, so the
acquisition page can show exactly why a release was or was not considered.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import httpx
from sqlalchemy.orm import Session

from hermes.config import Policy
from hermes.domain.models import Acquisition, AlbumTarget, Candidate, Event
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.integrations.musicbrainz import MusicBrainzClient, NotFound
from hermes.integrations.prowlarr import ProwlarrClient
from hermes.services.ranking import RankedCandidate, evaluate

log = logging.getLogger("hermes.search")

SEARCHABLE = {S.RESOLVED, S.NO_MATCH, S.FAILED, S.CANDIDATES_READY}


def _to_row(acq: Acquisition, c: RankedCandidate) -> Candidate:
    r = c.release
    return Candidate(
        acquisition=acq,
        prowlarr_guid=r.guid,
        indexer_id=r.indexer_id,
        indexer_name=r.indexer,
        title=r.title,
        download_url=r.download_url,
        info_url=r.info_url,
        size_bytes=r.size,
        seeders=r.seeders,
        leechers=r.leechers,
        freeleech=r.freeleech,
        parsed_quality={
            "parsed": c.parsed.as_dict(),
            "match": c.match.as_dict(),
            "info_hash": r.info_hash,
            "files": r.files,
            "flags": r.indexer_flags,
            "rank_score": round(c.score, 3),
            "estimate": c.estimate,
        },
        match_score=c.match.score,
        rank=c.rank,
        rejected_reason=c.rejected_reason,
    )


async def ensure_track_lengths(
    session: Session, mb: MusicBrainzClient, target: AlbumTarget, hint_year: int | None
) -> list[int]:
    """Track lengths of one release in the target's group, fetched once and kept on the
    target (two MusicBrainz calls the first time). Empty when MusicBrainz is unavailable."""
    known = list((target.track_lengths or {}).get("ms") or [])
    if known:
        return known
    from hermes.services.importer import pick_release  # no cycle: importer does not import search

    try:
        rg = await mb.release_group(target.release_group_mbid)
        release_id = pick_release(rg, hint_year)
        if release_id is None:
            return []
        lengths = await mb.release_track_lengths(release_id)
    except (httpx.HTTPError, NotFound) as exc:
        log.warning(
            "no track lengths for %s - %s: %s", target.artist_name, target.title, type(exc).__name__
        )
        return []
    if lengths and all(ms > 0 for ms in lengths):
        target.track_lengths = {"release": release_id, "ms": lengths}
        session.commit()
    else:
        log.info("incomplete track lengths for %s; not cached", target.title)
    return lengths


def complete_duration_ms(lengths: list[int]) -> int | None:
    """The playing time, only when every track's length is known: a partial sum would
    make a 24/96 release look like 24/192."""
    if not lengths or any(ms <= 0 for ms in lengths):
        return None
    return sum(lengths)


async def _allowed_indexer_ids(
    session: Session, prowlarr: ProwlarrClient, policy: Policy, acq: Acquisition
) -> list[int] | None:
    """Resolve `search.indexers` (names, as Prowlarr shows them) to indexer ids.

    None means no restriction. A configured name Prowlarr does not know gets a warning
    event rather than silently narrowing the search: a renamed indexer should be noticed.
    """
    wanted = policy.search.indexers
    if not wanted:
        return None
    known = {ix["name"].casefold(): ix for ix in await prowlarr.indexers()}
    ids: list[int] = []
    for name in wanted:
        ix = known.get(name.casefold())
        if ix is None:
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"search.indexers names {name!r}, which Prowlarr does not have",
                )
            )
        elif not ix["enable"]:
            session.add(
                Event(
                    acquisition=acq,
                    level="warning",
                    message=f"search.indexers names {name!r}, which is disabled in Prowlarr",
                )
            )
        else:
            ids.append(int(ix["id"]))
    session.commit()
    return ids


async def run_search(
    session: Session,
    prowlarr: ProwlarrClient,
    policy: Policy,
    acq: Acquisition,
    musicbrainz: MusicBrainzClient | None = None,
) -> Acquisition:
    target = acq.album_target
    if target is None:
        raise ValueError("cannot search an acquisition without a resolved target")
    if acq.state not in SEARCHABLE:
        raise ValueError(f"cannot search from state {acq.state}")
    query = f"{target.artist_name} {target.title}"
    transition(
        session, acq, S.SEARCHING, f"searching Prowlarr for {query!r}", data={"query": query}
    )
    session.commit()

    duration_ms = None
    track_count = 0
    if musicbrainz is not None and policy.quality.max_sample_rate_khz is not None:
        lengths = await ensure_track_lengths(
            session, musicbrainz, target, target.first_release_year
        )
        duration_ms = complete_duration_ms(lengths)
        track_count = len(lengths) if duration_ms else 0

    try:
        indexer_ids = await _allowed_indexer_ids(session, prowlarr, policy, acq)
        if policy.search.indexers and not indexer_ids:
            transition(
                session,
                acq,
                S.FAILED,
                "none of search.indexers exists in Prowlarr: " + ", ".join(policy.search.indexers),
                data={"retryable": True},
            )
            session.commit()
            return acq
        releases = await prowlarr.search(query, indexer_ids=indexer_ids)
    except httpx.HTTPError as exc:
        transition(
            session,
            acq,
            S.FAILED,
            f"Prowlarr unavailable: {type(exc).__name__}: {exc}",
            data={"retryable": True},
        )
        session.commit()
        return acq

    try:
        return _rank_and_record(session, policy, acq, query, releases, duration_ms, track_count)
    except Exception as exc:  # noqa: BLE001 - never strand the acquisition in SEARCHING
        session.rollback()
        transition(
            session,
            acq,
            S.FAILED,
            f"search processing failed: {type(exc).__name__}: {exc}",
            data={"retryable": True},
        )
        session.commit()
        return acq


def _target_types(acq: Acquisition) -> set[str]:
    target = acq.album_target
    if target is None:
        return set()
    types = {target.primary_type} if target.primary_type else set()
    types |= set((target.secondary_types or {}).get("types") or [])
    return types


def _rank_and_record(
    session: Session,
    policy: Policy,
    acq: Acquisition,
    query: str,
    releases: list[Any],
    duration_ms: int | None = None,
    track_count: int = 0,
) -> Acquisition:
    target = acq.album_target
    assert target is not None
    ranked = evaluate(
        releases,
        artist=target.artist_name,
        title=target.title,
        year=target.first_release_year,
        policy=policy.quality,
        target_types=_target_types(acq),
        duration_ms=duration_ms,
        track_count=track_count,
    )
    # Earlier candidates may be referenced by grab attempts, so they are superseded rather
    # than deleted; only rows nothing points at are removed.
    referenced = {a.candidate_id for a in acq.grab_attempts}
    for old in list(acq.candidates):
        if old.id in referenced:
            old.rank = None
            old.rejected_reason = "superseded by a later search"
        else:
            acq.candidates.remove(old)
            session.delete(old)
    session.flush()
    for c in ranked:
        session.add(_to_row(acq, c))
    acq.search_retries += 1

    accepted = [c for c in ranked if c.accepted]
    reasons = Counter(c.rejected_reason or "" for c in ranked if not c.accepted)
    summary = {
        "results": len(releases),
        "accepted": len(accepted),
        "kept": min(len(accepted), policy.quality.keep_candidates),
        "rejections": dict(reasons.most_common(10)),
        "top": [
            {
                "rank": c.rank,
                "title": c.release.title,
                "indexer": c.release.indexer,
                "seeders": c.release.seeders,
                "size": c.release.size,
                "freeleech": c.release.freeleech,
                "score": round(c.score, 2),
            }
            for c in accepted[: policy.quality.keep_candidates]
        ],
    }
    if accepted:
        best = accepted[0].release.title
        transition(
            session,
            acq,
            S.CANDIDATES_READY,
            f"{len(accepted)} of {len(releases)} results acceptable; best: {best}",
            data=summary,
        )
    else:
        transition(
            session,
            acq,
            S.NO_MATCH,
            f"no acceptable release among {len(releases)} results",
            data=summary,
            level="warning",
        )
    if policy.dry_run and acq.state == S.CANDIDATES_READY:
        session.add(Event(acquisition=acq, message="dry_run is on: nothing will be grabbed"))
    session.commit()
    return acq
