"""Server-rendered pages: queue, acquisition detail, request form. Plain forms with
POST-redirect-GET and a one-line notice carried in the query string; no JavaScript
beyond confirm() on destructive buttons (docs/plan.md A9)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from hermes import __version__
from hermes.api.routes import Ctx, DbSession
from hermes.api.schemas import requested_label
from hermes.domain.models import Acquisition, Playlist, Signal
from hermes.domain.state import TERMINAL, InvalidTransition, can_transition
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.musicbrainz import NotFound
from hermes.services import approval, importer
from hermes.services.context import Context
from hermes.services.pipeline import search_and_decide
from hermes.services.requests import (
    can_retry_request,
    continued_as,
    resolve_manually,
    retry_request,
    submit_manual_request,
)
from hermes.services.submit import next_candidate

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Every page shows which Hermes it is, error pages included: the cluster pins commit
# tags, so the header is the quickest way to tell whether a roll actually landed.
templates.env.globals["version"] = __version__

STATE_ORDER = [
    S.AWAITING_APPROVAL,
    S.NEEDS_REVIEW,
    S.IMPORT_NEEDS_REVIEW,
    S.STALLED,
    S.DOWNLOADING,
    S.SUBMITTED,
    S.READY_FOR_BEETS,
    S.IMPORTING,
    S.CANDIDATES_READY,
    S.SEARCHING,
    S.RESOLVED,
    S.NO_MATCH,
    S.FAILED,
    S.MANUAL,
    S.DISCOVERED,
]
# States where the page is worth reloading on its own.
LIVE_STATES = {S.SEARCHING, S.SUBMITTED, S.DOWNLOADING, S.READY_FOR_BEETS, S.IMPORTING}


def _mb(n: int | None) -> str:
    return f"{(n or 0) / 1e6:.0f} MB"


def _dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Stored times are UTC; SQLite hands them back naive. Say so on the page."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(fmt) + " UTC"


def _event_data(data: dict[str, Any]) -> dict[str, Any]:
    """The part of an event's data worth expanding: drop what the message already says and
    the resolution candidate dump, which has its own table when it matters."""
    return {k: v for k, v in data.items() if k not in ("from", "to", "candidates")}


templates.env.filters["mb"] = _mb
templates.env.filters["dt"] = _dt
templates.env.filters["event_data"] = _event_data


def _mode(request: Request) -> dict[str, Any]:
    """The two switches that decide whether a click spends ratio; shown on every page."""
    policy = request.app.state.policy
    return {"dry_run": policy.dry_run, "timid": policy.approval.timid}


def _approval_preview(ctx: Context, acq: Acquisition) -> dict[str, Any] | None:
    """What Approve would do: the candidate it fetches and where the torrent would go, so
    the one screen that spends ratio says so before the click. A routing miss (no Deluge
    instance for the indexer) is shown here rather than discovered at submit time."""
    top = next_candidate(acq)
    if top is None:
        return None
    deluge = ctx.policy.deluge
    instance = deluge.instance_for(top.indexer_name)
    return {
        "candidate": top,
        "instance": instance,
        "routed": instance is not None and instance in ctx.deluge,
        "pending": f"{deluge.pending_root.rstrip('/')}/{acq.id}",
        "label": deluge.label,
    }


def _state_phrase(state: str) -> str:
    return {
        S.AWAITING_APPROVAL: "waiting for your approval",
        S.NEEDS_REVIEW: "waiting for review",
        S.RESOLVED: "resolved",
        S.SEARCHING: "searching",
        S.CANDIDATES_READY: "deciding",
        S.NO_MATCH: "no match yet",
        S.SUBMITTED: "sent to Deluge",
        S.DOWNLOADING: "downloading",
        S.STALLED: "stalled",
        S.READY_FOR_BEETS: "waiting for beets",
        S.IMPORTING: "importing",
        S.IMPORT_NEEDS_REVIEW: "import needs review",
        S.FAILED: "failed",
    }.get(S(state), state.lower())


def render_error(request: Request, status: int, detail: str) -> HTMLResponse:
    back = request.headers.get("referer") or "/"
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status": status, "detail": detail, "back": back, **_mode(request)},
        status_code=status,
    )


def _redirect(acquisition_id: int, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/acquisitions/{acquisition_id}?notice={quote(notice)}", status_code=303
    )


def _get(session: Any, acquisition_id: int) -> Acquisition:
    acq: Acquisition | None = session.get(Acquisition, acquisition_id)
    if acq is None:
        raise HTTPException(404, f"There is no acquisition #{acquisition_id}.")
    return acq


def _review_candidates(acq: Acquisition) -> list[dict[str, Any]]:
    """The resolution candidates recorded when the request needed review."""
    for event in reversed(acq.events):
        if event.data.get("to") == S.NEEDS_REVIEW and event.data.get("candidates"):
            return list(event.data["candidates"])
    return []


# The states a person has to act on; the queue's "needs you" filter.
ATTENTION = {S.AWAITING_APPROVAL, S.NEEDS_REVIEW, S.IMPORT_NEEDS_REVIEW, S.STALLED}
DONE_PAGE = 50


def _discovery_line(session: Any) -> str | None:
    """One sentence about the most recent playlist ingested, for the queue header."""
    row = session.scalar(
        select(Playlist)
        .where(Playlist.status == "ingested")
        .order_by(Playlist.ingested_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    summary = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(row.summary.items()))
    when = _dt(row.ingested_at, "%Y-%m-%d %H:%M")
    return f"Last playlist: {row.title or row.mbid} ({row.track_count} tracks, {when}): {summary}."


@router.get("/", response_class=HTMLResponse)
def queue(request: Request, session: DbSession, ctx: Ctx) -> HTMLResponse:
    rows = session.scalars(select(Acquisition).order_by(Acquisition.updated_at.desc())).all()
    state_filter = request.query_params.get("state") or ""
    origin_filter = request.query_params.get("origin") or ""
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except ValueError:
        page = 1

    active_all = [a for a in rows if a.state not in TERMINAL]
    done_all = [a for a in rows if a.state in TERMINAL]
    if origin_filter in ("manual", "auto"):
        active_all = [a for a in active_all if a.origin == origin_filter]
        done_all = [a for a in done_all if a.origin == origin_filter]
    state_counts: dict[str, int] = {}
    for a in active_all:
        state_counts[a.state] = state_counts.get(a.state, 0) + 1
    attention = sum(1 for a in active_all if a.state in ATTENTION)

    active = active_all
    if state_filter == "attention":
        active = [a for a in active_all if a.state in ATTENTION]
    elif state_filter:
        active = [a for a in active_all if a.state == state_filter]
    active.sort(key=lambda a: STATE_ORDER.index(S(a.state)) if a.state in STATE_ORDER else 99)
    pages = max((len(done_all) + DONE_PAGE - 1) // DONE_PAGE, 1)
    page = min(page, pages)
    done = done_all[(page - 1) * DONE_PAGE : page * DONE_PAGE]

    def link(**changes: Any) -> str:
        params = {"state": state_filter, "origin": origin_filter, "page": page}
        params.update(changes)
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v and v != 1)
        return "/" + (f"?{query}" if query else "")

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "active": active,
            "active_total": len(active_all),
            "done": done,
            "done_total": len(done_all),
            "page": page,
            "pages": pages,
            "state_filter": state_filter,
            "origin_filter": origin_filter,
            "state_counts": sorted(
                state_counts.items(),
                key=lambda kv: STATE_ORDER.index(S(kv[0])) if kv[0] in STATE_ORDER else 99,
            ),
            "attention": attention,
            "link": link,
            "discovery": _discovery_line(session),
            "discovery_configured": ctx.listenbrainz is not None,
            **_mode(request),
            "requested": requested_label,
            "retryable": can_retry_request,
            "notice": request.query_params.get("notice"),
            "refresh": 30 if any(a.state in LIVE_STATES for a in active) else None,
            "searchable": {S.RESOLVED, S.NO_MATCH, S.FAILED},
        },
    )


@router.get("/acquisitions/{acquisition_id}", response_class=HTMLResponse)
def detail(acquisition_id: int, request: Request, ctx: Ctx, session: DbSession) -> HTMLResponse:
    acq = _get(session, acquisition_id)
    candidates = sorted(acq.candidates, key=lambda c: (c.rank is None, c.rank or 0, c.id))
    state = S(acq.state)
    has_target = acq.album_target is not None
    decision_states = (S.AWAITING_APPROVAL, S.CANDIDATES_READY, S.STALLED)
    return templates.TemplateResponse(
        request,
        "acquisition.html",
        {
            "acq": acq,
            "requested": requested_label(acq),
            "candidates": candidates,
            "review_candidates": _review_candidates(acq) if state == S.NEEDS_REVIEW else [],
            "preview": _approval_preview(ctx, acq) if state in decision_states else None,
            "can_approve": state in (S.AWAITING_APPROVAL, S.CANDIDATES_READY),
            "can_fallback": state == S.STALLED,
            "can_reject": can_transition(state, S.REJECTED),
            "can_cancel": can_transition(state, S.CANCELLED),
            "can_search": has_target
            and state in (S.RESOLVED, S.NO_MATCH, S.FAILED, S.CANDIDATES_READY),
            "never_searched": acq.search_retries == 0,
            "can_retry_import": state == S.IMPORT_NEEDS_REVIEW
            or (state == S.FAILED and importer.completed_attempt(acq) is not None),
            "can_retry_request": can_retry_request(acq),
            "failed_unresolved": state == S.FAILED and not has_target,
            "continued_as": continued_as(acq),
            **_mode(request),
            "notice": request.query_params.get("notice"),
            "refresh": 15 if state in LIVE_STATES else None,
        },
    )


@router.post("/acquisitions/{acquisition_id}/approve")
async def ui_approve(acquisition_id: int, ctx: Ctx, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        await approval.approve(session, ctx, acq, by="ui")
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    if ctx.policy.dry_run:
        notice = (
            "Approved. Dry run is on, so nothing was grabbed; turn it off and approve "
            "again to grab."
        )
    elif acq.state == "SUBMITTED":
        notice = "Approved and sent to Deluge."
    else:
        notice = f"Approved; now {acq.state}."
    return _redirect(acquisition_id, notice)


@router.post("/acquisitions/{acquisition_id}/reject")
def ui_reject(
    acquisition_id: int, session: DbSession, reason: Annotated[str, Form()] = ""
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        approval.reject(session, acq, by="ui", reason=reason)
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(acquisition_id, "Rejected.")


@router.post("/acquisitions/{acquisition_id}/cancel")
def ui_cancel(acquisition_id: int, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        approval.cancel(session, acq, by="ui")
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(acquisition_id, "Cancelled.")


@router.post("/acquisitions/{acquisition_id}/search")
async def ui_search(acquisition_id: int, ctx: Ctx, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    if ctx.prowlarr is None:
        raise HTTPException(503, "Prowlarr is not configured, so there is nothing to search.")
    try:
        await search_and_decide(session, ctx, acq)
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    if acq.state == S.NO_MATCH:
        notice = "Searched again: nothing acceptable on the tracker."
    elif acq.state == S.AWAITING_APPROVAL:
        notice = "Searched: a candidate is waiting for your approval."
    else:
        notice = f"Searched; now {acq.state}."
    return _redirect(acquisition_id, notice)


@router.post("/acquisitions/{acquisition_id}/retry-import")
async def ui_retry_import(acquisition_id: int, ctx: Ctx, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        await importer.retry_import(session, ctx, acq)
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(acquisition_id, f"Import retried; now {acq.state}.")


@router.post("/acquisitions/{acquisition_id}/retry")
async def ui_retry_request(acquisition_id: int, ctx: Ctx, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        new = await retry_request(session, ctx, acq)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(new.id, f"Retried #{acquisition_id} as request #{new.id}: {new.state}.")


@router.post("/acquisitions/{acquisition_id}/resolve")
async def ui_resolve(
    acquisition_id: int,
    ctx: Ctx,
    session: DbSession,
    release_group_mbid: Annotated[str, Form()],
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    try:
        acq = await resolve_manually(session, ctx, acq, release_group_mbid.strip())
    except NotFound as exc:
        raise HTTPException(404, f"MusicBrainz has no release group {release_group_mbid}.") from exc
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    if acq.id != acquisition_id:
        return _redirect(
            acq.id,
            f"That album is already in flight as #{acq.id} ({_state_phrase(acq.state)}); "
            f"#{acquisition_id} was closed.",
        )
    return _redirect(acq.id, f"Release group chosen; now {acq.state}.")


@router.post("/requests")
async def ui_request(
    ctx: Ctx,
    session: DbSession,
    artist: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    mbid: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if not mbid.strip() and not (artist.strip() and title.strip()):
        raise HTTPException(422, "Give an artist and an album title, or a MusicBrainz ID.")
    last_signal = session.scalar(select(func.max(Signal.id))) or 0
    acq = await submit_manual_request(
        session,
        ctx,
        artist=artist.strip() or None,
        title=title.strip() or None,
        mbid=mbid.strip() or None,
    )
    if acq.signal_id is not None and acq.signal_id <= last_signal:
        # The album was already in flight: this request was attached to it rather than
        # creating a new acquisition. Say so, or the id change goes unnoticed.
        return _redirect(
            acq.id,
            f"Already requested as #{acq.id} ({_state_phrase(acq.state)}); "
            "your request was attached to it.",
        )
    return _redirect(acq.id, f"Request #{acq.id} is {acq.state}.")
