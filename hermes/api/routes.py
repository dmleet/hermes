from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.api.schemas import (
    AcquisitionOut,
    AcquisitionSummary,
    DecisionBody,
    ManualRequest,
    PlaylistOut,
    PreferBody,
    ResolveBody,
)
from hermes.domain.models import Acquisition, Candidate, Playlist
from hermes.domain.state import InvalidTransition
from hermes.integrations.musicbrainz import NotFound
from hermes.services import approval, discovery, importer, observer
from hermes.services.context import Context
from hermes.services.pipeline import search_and_decide
from hermes.services.requests import resolve_manually, retry_request, submit_manual_request

router = APIRouter(prefix="/api")


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_context(request: Request) -> Context:
    ctx: Context = request.app.state.context
    return ctx


DbSession = Annotated[Session, Depends(get_session)]
Ctx = Annotated[Context, Depends(get_context)]


def _load(session: Session, acquisition_id: int) -> Acquisition:
    acq = session.get(Acquisition, acquisition_id)
    if acq is None:
        raise HTTPException(404, "no such acquisition")
    return acq


@router.post("/requests", response_model=AcquisitionOut, status_code=201)
async def create_request(body: ManualRequest, ctx: Ctx, session: DbSession) -> AcquisitionOut:
    acq = await submit_manual_request(
        session, ctx, artist=body.artist, title=body.title, mbid=body.mbid
    )
    return AcquisitionOut.from_model(acq)


@router.get("/acquisitions", response_model=list[AcquisitionSummary])
def list_acquisitions(
    session: DbSession,
    state: str | None = None,
    origin: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AcquisitionSummary]:
    stmt = select(Acquisition).order_by(Acquisition.updated_at.desc()).limit(limit).offset(offset)
    if state:
        stmt = stmt.where(Acquisition.state == state)
    if origin:
        stmt = stmt.where(Acquisition.origin == origin)
    return [AcquisitionSummary.from_model(a) for a in session.scalars(stmt)]


@router.get("/playlists", response_model=list[PlaylistOut])
def list_playlists(session: DbSession, limit: int = 100) -> list[PlaylistOut]:
    """Playlists discovery has seen, newest first, with each one's outcome counts."""
    stmt = select(Playlist).order_by(Playlist.seen_at.desc()).limit(limit)
    return [PlaylistOut.model_validate(p) for p in session.scalars(stmt)]


@router.post("/jobs/discover")
async def run_discovery(ctx: Ctx, session: DbSession) -> dict[str, int]:
    """One discovery tick now (what the scheduler runs every listenbrainz.poll_hours)."""
    return await discovery.tick(session, ctx)


@router.post("/jobs/research")
async def run_research(ctx: Ctx, session: DbSession) -> dict[str, int]:
    """One re-search tick now (NO_MATCH rows past search.retry_days)."""
    return await discovery.research_tick(session, ctx)


@router.get("/acquisitions/{acquisition_id}", response_model=AcquisitionOut)
def get_acquisition(acquisition_id: int, session: DbSession) -> AcquisitionOut:
    return AcquisitionOut.from_model(_load(session, acquisition_id))


@router.post("/acquisitions/{acquisition_id}/search", response_model=AcquisitionOut)
async def search_acquisition(acquisition_id: int, ctx: Ctx, session: DbSession) -> AcquisitionOut:
    """(Re)run the Prowlarr search and the approval gate for a resolved acquisition."""
    acq = _load(session, acquisition_id)
    if ctx.prowlarr is None:
        raise HTTPException(503, "Prowlarr is not configured")
    try:
        acq = await search_and_decide(session, ctx, acq)
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/resolve", response_model=AcquisitionOut)
async def resolve_acquisition(
    acquisition_id: int, body: ResolveBody, ctx: Ctx, session: DbSession
) -> AcquisitionOut:
    """A human picks the release group for a request in NEEDS_REVIEW."""
    acq = _load(session, acquisition_id)
    try:
        acq = await resolve_manually(session, ctx, acq, body.release_group_mbid)
    except NotFound as exc:
        raise HTTPException(404, f"no such release group: {body.release_group_mbid}") from exc
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/retry", response_model=AcquisitionOut)
async def retry_acquisition(acquisition_id: int, ctx: Ctx, session: DbSession) -> AcquisitionOut:
    """Re-run a request that failed before resolving (returns the new acquisition)."""
    acq = _load(session, acquisition_id)
    try:
        new = await retry_request(session, ctx, acq)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(new)


@router.post("/acquisitions/{acquisition_id}/approve", response_model=AcquisitionOut)
async def approve_acquisition(
    acquisition_id: int, ctx: Ctx, session: DbSession, body: DecisionBody | None = None
) -> AcquisitionOut:
    acq = _load(session, acquisition_id)
    try:
        acq = await approval.approve(session, ctx, acq, by=(body.by if body else "api"))
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/prefer", response_model=AcquisitionOut)
def prefer_candidate(acquisition_id: int, session: DbSession, body: PreferBody) -> AcquisitionOut:
    """Put one of the acquisition's candidates in front, so Approve fetches it. Reorders
    only; nothing is grabbed here."""
    acq = _load(session, acquisition_id)
    candidate = session.get(Candidate, body.candidate_id)
    if candidate is None or candidate.acquisition_id != acq.id:
        raise HTTPException(404, "no such candidate on this acquisition")
    try:
        acq = approval.prefer(session, acq, candidate, by=body.by)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/reject", response_model=AcquisitionOut)
def reject_acquisition(
    acquisition_id: int, session: DbSession, body: DecisionBody | None = None
) -> AcquisitionOut:
    acq = _load(session, acquisition_id)
    try:
        acq = approval.reject(
            session, acq, by=(body.by if body else "api"), reason=(body.reason if body else "")
        )
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/cancel", response_model=AcquisitionOut)
def cancel_acquisition(
    acquisition_id: int, session: DbSession, body: DecisionBody | None = None
) -> AcquisitionOut:
    acq = _load(session, acquisition_id)
    try:
        acq = approval.cancel(session, acq, by=(body.by if body else "api"))
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/acquisitions/{acquisition_id}/retry-import", response_model=AcquisitionOut)
async def retry_import(acquisition_id: int, ctx: Ctx, session: DbSession) -> AcquisitionOut:
    acq = _load(session, acquisition_id)
    try:
        acq = await importer.retry_import(session, ctx, acq)
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return AcquisitionOut.from_model(acq)


@router.post("/jobs/observe")
async def run_observer(ctx: Ctx, session: DbSession) -> dict[str, int]:
    """One observer tick: poll Deluge for every active grab attempt."""
    return await observer.tick(session, ctx)


@router.post("/jobs/import")
async def run_importer(ctx: Ctx, session: DbSession) -> dict[str, int]:
    """One import tick: start imports for ready downloads, poll running ones."""
    return await importer.tick(session, ctx)
