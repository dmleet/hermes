"""Server-rendered pages: request form, queue, history, acquisition detail. Plain forms with
POST-redirect-GET and a one-line notice carried in the query string; no JavaScript beyond
confirm() and the submit handler in base.html (docs/plan.md A9). The page design is
docs/ui-plan.md."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from hermes import __version__
from hermes.api.routes import Ctx, DbSession, kick_art
from hermes.api.schemas import requested_label
from hermes.domain.models import Acquisition, AlbumTarget, Candidate, Playlist, Signal
from hermes.domain.state import TERMINAL, InvalidTransition, can_transition
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.musicbrainz import NotFound
from hermes.services import approval, art, genres, importer
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

# The queue's order: rows a person has to act on first, then what Hermes, Deluge and
# beets are working on in pipeline order (read top to bottom it is a progress board), then
# the states waiting on a schedule or passing through between stages.
STATE_ORDER = [
    S.AWAITING_APPROVAL,
    S.NEEDS_REVIEW,
    S.IMPORT_NEEDS_REVIEW,
    S.STALLED,
    S.FAILED,
    S.SEARCHING,
    S.SUBMITTED,
    S.DOWNLOADING,
    S.READY_FOR_BEETS,
    S.IMPORTING,
    S.CANDIDATES_READY,
    S.RESOLVED,
    S.NO_MATCH,
    S.MANUAL,
    S.DISCOVERED,
]
# The queue's filters group states by who has the ball (docs/ui-plan.md 3.2). "In
# flight": Hermes, Deluge or beets are working on it, so the page is worth reloading on its
# own. "Needs you": only a person can move it; the header badge counts these. FAILED is a
# person's decision too (retry, search again or cancel), so it is in "needs you" rather
# than sorted to the bottom. NO_MATCH waits on the re-search schedule and has its own
# chip; the rest are between-stage states that last seconds and only show under "all".
LIVE_STATES = {S.SEARCHING, S.SUBMITTED, S.DOWNLOADING, S.READY_FOR_BEETS, S.IMPORTING}
ATTENTION = {S.AWAITING_APPROVAL, S.NEEDS_REVIEW, S.IMPORT_NEEDS_REVIEW, S.STALLED, S.FAILED}
GROUPS: dict[str, set[S]] = {"attention": ATTENTION, "inflight": LIVE_STATES}
DONE_PAGE = 50
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# The queue's order, once, as SQL: states a human must act on first, then the oldest row
# first within a state, then id. The detail page's prev/next walk the same order with
# bounded queries instead of loading the table (docs/ui-plan.md 3.2).
STATE_RANK = case(
    {s.value: i for i, s in enumerate(STATE_ORDER)}, value=Acquisition.state, else_=99
)
QUEUE_ORDER = (STATE_RANK, Acquisition.updated_at, Acquisition.id)
_TERMINAL = [s.value for s in TERMINAL]
_ATTENTION = [s.value for s in ATTENTION]


# What a state pill says. The raw name stays the CSS class (colour) and the API value;
# the words are for people, and short: the phone row has the pill beside the playlist.
STATE_LABELS: dict[S, str] = {
    S.DISCOVERED: "discovered",
    S.MANUAL: "requested",
    S.RESOLVED: "resolved",
    S.NEEDS_REVIEW: "needs review",
    S.ALREADY_OWNED: "owned",
    S.SEARCHING: "searching",
    S.NO_MATCH: "no match",
    S.CANDIDATES_READY: "candidates",
    S.AWAITING_APPROVAL: "needs approval",
    S.REJECTED: "rejected",
    S.SUBMITTED: "submitted",
    S.DOWNLOADING: "downloading",
    S.STALLED: "stalled",
    S.READY_FOR_BEETS: "ready to import",
    S.IMPORTING: "importing",
    S.IMPORT_NEEDS_REVIEW: "import review",
    S.IMPORTED: "imported",
    S.FAILED: "failed",
    S.CANCELLED: "cancelled",
}


def state_label(state: str) -> str:
    try:
        return STATE_LABELS[S(state)]
    except (ValueError, KeyError):
        return state.lower().replace("_", " ")


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


def _initial(acq: Acquisition) -> str:
    """One letter for the art placeholder box."""
    t = acq.album_target
    name = t.artist_name if t else (requested_label(acq) or "?")
    return next((ch for ch in name if ch.isalnum()), "?").upper()


_WEEKLY = re.compile(r"^(?P<name>.+?) for \S+, week of \d{4}-(?P<md>\d\d-\d\d)(?: \w+)?$")


def _playlist(name: str | None) -> str:
    """ListenBrainz's generated names carry the user and the full date ("Weekly Exploration
    for someone, week of 2026-09-14 Mon"); a row has room for the series and the day."""
    if not name:
        return ""
    m = _WEEKLY.match(name)
    return f"{m['name']}, {m['md']}" if m else name


def _genres(target: AlbumTarget | None, limit: int = genres.ON_ROW) -> list[str]:
    return genres.display(target.genres, limit) if target is not None else []


templates.env.filters["mb"] = _mb
templates.env.filters["label"] = state_label
templates.env.filters["playlist"] = _playlist
templates.env.filters["genres"] = _genres
templates.env.filters["dt"] = _dt
templates.env.filters["event_data"] = _event_data
templates.env.filters["initial"] = _initial


def _qs(**params: Any) -> str:
    """A query string from the parameters that are set, or an empty string."""
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, "", 0))
    return f"?{query}" if query else ""


def _mode(request: Request) -> dict[str, Any]:
    """The two switches that decide whether a click spends ratio; shown on every page."""
    policy = request.app.state.policy
    return {"dry_run": policy.dry_run, "timid": policy.approval.timid}


def _attention(session: Session) -> int:
    """How many rows need a human; the badge on the Queue link."""
    return int(
        session.scalar(
            select(func.count()).select_from(Acquisition).where(Acquisition.state.in_(_ATTENTION))
        )
        or 0
    )


def _page(request: Request, session: Session, nav: str, **extra: Any) -> dict[str, Any]:
    """Context every page gets: mode badges, the needs-you count, the active nav item."""
    return {
        "nav": nav,
        "attention": _attention(session),
        "notice": request.query_params.get("notice"),
        **_mode(request),
        **extra,
    }


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
        "parsed": top.parsed_quality.get("parsed") or {},
        "estimate": top.parsed_quality.get("estimate"),
        "instance": instance,
        "routed": instance is not None and instance in ctx.deluge,
        "fetchable": bool(top.download_url),
        "pending": deluge.locations(acq.id)[0],
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
    # No session here: the error may be the database failing, so no needs-you count.
    back = request.headers.get("referer") or "/"
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status": status, "detail": detail, "back": back, "nav": "", **_mode(request)},
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


def _discovery_line(session: Any) -> str | None:
    """One sentence about the most recent playlist ingested, for the request page."""
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


# --- the queue's filter and order, shared by the queue page and the detail page's walk ---


def _filters(request: Request) -> tuple[str, str]:
    """The queue filter from the query string: a group name, an exact state, or an origin.
    Anything else is dropped rather than carried into a walk that matches nothing. Exact
    states have no chip; the query string keeps them for bookmarks and so a chip can come
    back without touching the routes."""
    q = request.query_params
    state = q.get("state") or ""
    if state not in GROUPS and state not in {s.value for s in S}:
        state = ""
    origin = q.get("origin") or ""
    return state, origin if origin in ("manual", "auto") else ""


def _active_filter(state_filter: str, origin_filter: str) -> Any:
    conds: list[Any] = [Acquisition.state.not_in(_TERMINAL)]
    if origin_filter in ("manual", "auto"):
        conds.append(Acquisition.origin == origin_filter)
    if state_filter in GROUPS:
        conds.append(Acquisition.state.in_([s.value for s in GROUPS[state_filter]]))
    elif state_filter:
        conds.append(Acquisition.state == state_filter)
    return and_(*conds)


def _in_filter(acq: Acquisition, state_filter: str, origin_filter: str) -> bool:
    """The Python side of `_active_filter`, for the row just acted on."""
    if acq.state in TERMINAL:
        return False
    if origin_filter in ("manual", "auto") and acq.origin != origin_filter:
        return False
    if state_filter in GROUPS:
        return acq.state in GROUPS[state_filter]
    return not state_filter or acq.state == state_filter


def _key(acq: Acquisition) -> tuple[int, datetime, int]:
    """The row's position in QUEUE_ORDER, comparable in SQL. SQLite stores naive UTC."""
    rank = STATE_ORDER.index(S(acq.state)) if acq.state in STATE_ORDER else 99
    at = acq.updated_at.replace(tzinfo=None) if acq.updated_at.tzinfo else acq.updated_at
    return rank, at, acq.id


def _after(key: tuple[int, datetime, int]) -> Any:
    """Rows sorted after `key` in QUEUE_ORDER."""
    rank, at, id_ = key
    same_rank = STATE_RANK.__eq__(rank)
    return or_(
        STATE_RANK.__gt__(rank),
        and_(same_rank, Acquisition.updated_at > at),
        and_(same_rank, Acquisition.updated_at == at, Acquisition.id > id_),
    )


def _before(key: tuple[int, datetime, int]) -> Any:
    """Rows sorted before `key` in QUEUE_ORDER."""
    rank, at, id_ = key
    same_rank = STATE_RANK.__eq__(rank)
    return or_(
        STATE_RANK.__lt__(rank),
        and_(same_rank, Acquisition.updated_at < at),
        and_(same_rank, Acquisition.updated_at == at, Acquisition.id < id_),
    )


def _next_in_list(
    session: Session,
    key: tuple[int, datetime, int],
    state_filter: str,
    origin_filter: str,
    *,
    exclude_id: int | None = None,
) -> int | None:
    """The first row after `key` in the list. `exclude_id` is the row just acted on: an
    approved row is still in the unfiltered queue, sorted after its old position, and
    must not be offered as its own successor."""
    conds = [_active_filter(state_filter, origin_filter), _after(key)]
    if exclude_id is not None:
        conds.append(Acquisition.id != exclude_id)
    return session.scalar(select(Acquisition.id).where(*conds).order_by(*QUEUE_ORDER).limit(1))


def _triage(
    session: Session, acq: Acquisition, state_filter: str, origin_filter: str
) -> dict[str, Any] | None:
    """Prev/next and position for the detail page, within the queue filter it was opened
    with. Four bounded queries; never the table. A row that has left the filter (a search
    that found nothing while walking "needs you") still gets neighbours, but no position."""
    if acq.state in TERMINAL:
        return None
    flt = _active_filter(state_filter, origin_filter)
    key = _key(acq)
    prev_id = session.scalar(
        select(Acquisition.id)
        .where(flt, _before(key))
        .order_by(STATE_RANK.desc(), Acquisition.updated_at.desc(), Acquisition.id.desc())
        .limit(1)
    )
    total = session.scalar(select(func.count()).select_from(Acquisition).where(flt)) or 0
    position = None
    if _in_filter(acq, state_filter, origin_filter):
        before = session.scalar(
            select(func.count()).select_from(Acquisition).where(flt, _before(key))
        )
        position = int(before or 0) + 1
    return {
        "prev": prev_id,
        "next": _next_in_list(session, key, state_filter, origin_filter),
        "position": position,
        "total": int(total),
    }


def _finish(
    request: Request,
    session: Session,
    acq: Acquisition,
    key_before: tuple[int, datetime, int],
    notice: str,
    *,
    advance: bool = True,
) -> RedirectResponse:
    """Where an action lands. From the queue table: back to the queue. From a detail page
    opened as part of a walk (`walk=1` plus the queue filter): the next item, but only when
    the action left this one no longer needing a human (out of the ATTENTION states, or out
    of the filter the walk was opened with) and not in FAILED, so a dry-run approval and a
    failed submit stay on the screen that explains them. A live approval advances from any
    list: the row is still in the unfiltered queue, lower down, but it is done with.
    Otherwise: the same page."""
    q = request.query_params
    state_filter, origin_filter = _filters(request)
    if q.get("back") == "queue":
        return RedirectResponse(
            "/queue" + _qs(state=state_filter, origin=origin_filter, notice=notice),
            status_code=303,
        )
    walk = q.get("walk") or ""
    done = acq.state not in ATTENTION or not _in_filter(acq, state_filter, origin_filter)
    if walk and advance and acq.state != S.FAILED and done:
        nxt = _next_in_list(session, key_before, state_filter, origin_filter, exclude_id=acq.id)
        if nxt is None:
            return RedirectResponse(
                "/queue"
                + _qs(
                    state=state_filter,
                    origin=origin_filter,
                    notice=f"{notice} Nothing else in this list.",
                ),
                status_code=303,
            )
        return RedirectResponse(
            f"/acquisitions/{nxt}"
            + _qs(walk=1, state=state_filter, origin=origin_filter, notice=notice, prev=acq.id),
            status_code=303,
        )
    return RedirectResponse(
        f"/acquisitions/{acq.id}"
        + _qs(walk=walk, state=state_filter, origin=origin_filter, notice=notice),
        status_code=303,
    )


# --- pages ---


@router.get("/", response_class=HTMLResponse)
def request_page(request: Request, session: DbSession, ctx: Ctx) -> Response:
    """The landing page is the request form: the quick-add case. Old bookmarks of the
    queue and its finished list, which used to live here, are sent on."""
    q = request.query_params
    if q.get("page"):
        return RedirectResponse("/history" + _qs(page=q.get("page")), status_code=302)
    if q.get("state") or q.get("origin"):
        return RedirectResponse(
            "/queue" + _qs(state=q.get("state"), origin=q.get("origin")), status_code=302
        )
    recent = session.scalars(
        select(Acquisition)
        .where(Acquisition.origin == "manual")
        .order_by(Acquisition.created_at.desc())
        .limit(5)
    ).all()
    return templates.TemplateResponse(
        request,
        "request.html",
        _page(
            request,
            session,
            "request",
            recent=recent,
            requested=requested_label,
            discovery=_discovery_line(session),
            discovery_configured=ctx.listenbrainz is not None,
        ),
    )


@router.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    """So "Add to Home Screen" gives an icon that opens the request page."""
    body = {
        "name": "Hermes",
        "short_name": "Hermes",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#fafafa",
        "theme_color": "#0b5fff",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return JSONResponse(body, media_type="application/manifest+json")


@router.get("/art/{mbid}.jpg", include_in_schema=False)
def art_file(mbid: str, request: Request) -> FileResponse:
    """A fetched cover, as the archive served it (JPEG or PNG; the browser sniffs). The
    MBID is checked against its shape so the path can only ever be a file the art job
    wrote. Cached for a year: covers rarely change and the name is the release group."""
    art_dir = request.app.state.context.art_dir
    if art_dir is None or not MBID.match(mbid):
        raise HTTPException(404, "No art for that release group.")
    path = art.art_path(art_dir, mbid)
    if not path.is_file():
        raise HTTPException(404, "No art for that release group.")
    with path.open("rb") as fh:
        media_type = "image/png" if fh.read(4) == b"\x89PNG" else "image/jpeg"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, session: DbSession, ctx: Ctx) -> HTMLResponse:
    state_filter, origin_filter = _filters(request)
    active = session.scalars(
        select(Acquisition)
        .where(_active_filter(state_filter, origin_filter))
        .order_by(*QUEUE_ORDER)
    ).all()
    counts: dict[str, int] = {
        state: n
        for state, n in session.execute(
            select(Acquisition.state, func.count())
            .where(_active_filter("", origin_filter))
            .group_by(Acquisition.state)
        )
    }

    def link(**changes: Any) -> str:
        params: dict[str, Any] = {"state": state_filter, "origin": origin_filter}
        params.update(changes)
        return "/queue" + _qs(**params)

    return templates.TemplateResponse(
        request,
        "queue.html",
        _page(
            request,
            session,
            "queue",
            active=active,
            active_total=sum(counts.values()),
            group_counts={
                "attention": sum(n for s, n in counts.items() if s in ATTENTION),
                "inflight": sum(n for s, n in counts.items() if s in LIVE_STATES),
                "nomatch": counts.get(S.NO_MATCH.value, 0),
            },
            state_filter=state_filter,
            origin_filter=origin_filter,
            # The per-state breakdown, as text under the chips: a glance at what the
            # machine is doing, not a filter (docs/ui-plan.md 3.2).
            state_line=" · ".join(
                f"{state_label(state)} {n}"
                for state, n in sorted(
                    counts.items(),
                    key=lambda kv: STATE_ORDER.index(S(kv[0])) if kv[0] in STATE_ORDER else 99,
                )
            ),
            link=link,
            # Row links open the detail page as a walk through this list; inline actions
            # come back here.
            walk=_qs(walk=1, state=state_filter, origin=origin_filter),
            back=_qs(back="queue", state=state_filter, origin=origin_filter),
            requested=requested_label,
            retryable=can_retry_request,
            refresh=30 if any(a.state in LIVE_STATES for a in active) else None,
            searchable={S.RESOLVED, S.NO_MATCH, S.FAILED},
        ),
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, session: DbSession) -> HTMLResponse:
    state_filter, origin_filter = _filters(request)
    search = (request.query_params.get("q") or "").strip()
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except ValueError:
        page = 1

    conds: list[Any] = [Acquisition.state.in_(_TERMINAL)]
    if origin_filter in ("manual", "auto"):
        conds.append(Acquisition.origin == origin_filter)
    if search:
        like = f"%{search}%"
        conds.append(
            or_(
                AlbumTarget.artist_name.ilike(like),
                AlbumTarget.title.ilike(like),
                Signal.requested_artist.ilike(like),
                Signal.requested_title.ilike(like),
            )
        )
    base = select(Acquisition).outerjoin(Acquisition.album_target).outerjoin(Acquisition.signal)
    counts: dict[str, int] = {
        state: n
        for state, n in session.execute(
            select(Acquisition.state, func.count())
            .select_from(Acquisition)
            .outerjoin(Acquisition.album_target)
            .outerjoin(Acquisition.signal)
            .where(*conds)
            .group_by(Acquisition.state)
        )
    }
    if state_filter:
        conds.append(Acquisition.state == state_filter)
    total = sum(n for s, n in counts.items() if not state_filter or s == state_filter)
    pages = max((total + DONE_PAGE - 1) // DONE_PAGE, 1)
    page = min(page, pages)
    done = session.scalars(
        base.where(*conds)
        .order_by(Acquisition.updated_at.desc())
        .offset((page - 1) * DONE_PAGE)
        .limit(DONE_PAGE)
    ).all()

    def link(**changes: Any) -> str:
        params: dict[str, Any] = {
            "state": state_filter,
            "origin": origin_filter,
            "q": search,
            "page": page,
        }
        params.update(changes)
        if params.get("page") == 1:
            params["page"] = ""
        return "/history" + _qs(**params)

    return templates.TemplateResponse(
        request,
        "history.html",
        _page(
            request,
            session,
            "history",
            done=done,
            done_total=total,
            page=page,
            pages=pages,
            state_filter=state_filter,
            origin_filter=origin_filter,
            search=search,
            state_counts=sorted(counts.items(), key=lambda kv: -kv[1]),
            link=link,
            requested=requested_label,
        ),
    )


@router.get("/acquisitions/{acquisition_id}", response_class=HTMLResponse)
def detail(acquisition_id: int, request: Request, ctx: Ctx, session: DbSession) -> HTMLResponse:
    acq = _get(session, acquisition_id)
    candidates = sorted(acq.candidates, key=lambda c: (c.rank is None, c.rank or 0, c.id))
    state = S(acq.state)
    has_target = acq.album_target is not None
    decision_states = (S.AWAITING_APPROVAL, S.CANDIDATES_READY, S.STALLED)
    state_filter, origin_filter = _filters(request)
    walk = request.query_params.get("walk") or ""
    try:
        prev_id: int | None = int(request.query_params.get("prev") or 0) or None
    except ValueError:
        prev_id = None
    return templates.TemplateResponse(
        request,
        "acquisition.html",
        _page(
            request,
            session,
            "queue" if state not in TERMINAL else "history",
            acq=acq,
            terminal=state in TERMINAL,
            requested=requested_label(acq),
            candidates=candidates,
            review_candidates=_review_candidates(acq) if state == S.NEEDS_REVIEW else [],
            preview=_approval_preview(ctx, acq) if state in decision_states else None,
            preferable=approval.preferable(acq),
            preferred_id=approval.preferred_id(acq),
            can_approve=state in (S.AWAITING_APPROVAL, S.CANDIDATES_READY),
            can_fallback=state == S.STALLED,
            can_reject=can_transition(state, S.REJECTED),
            can_cancel=can_transition(state, S.CANCELLED),
            can_search=has_target
            and state in (S.RESOLVED, S.NO_MATCH, S.FAILED, S.CANDIDATES_READY),
            never_searched=acq.search_retries == 0,
            can_retry_import=state == S.IMPORT_NEEDS_REVIEW
            or (state == S.FAILED and importer.completed_attempt(acq) is not None),
            can_retry_request=can_retry_request(acq),
            failed_unresolved=state == S.FAILED and not has_target,
            continued_as=continued_as(acq),
            triage=_triage(session, acq, state_filter, origin_filter) if walk else None,
            # Links and form actions carry the walk context so the next page keeps it.
            walk_qs=_qs(walk=walk, state=state_filter, origin=origin_filter),
            list_qs=_qs(state=state_filter, origin=origin_filter),
            prev_id=prev_id,
            refresh=15 if state in LIVE_STATES else None,
        ),
    )


# --- actions ---


@router.post("/acquisitions/{acquisition_id}/approve")
async def ui_approve(
    acquisition_id: int, request: Request, ctx: Ctx, session: DbSession
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
    try:
        await approval.approve(session, ctx, acq, by="ui")
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    if ctx.policy.dry_run:
        notice = (
            f"Approved #{acq.id}. Dry run is on, so nothing was grabbed; turn it off and "
            "approve again to grab."
        )
    elif acq.state == "SUBMITTED":
        notice = f"Approved #{acq.id} and sent to Deluge."
    else:
        notice = f"Approved #{acq.id}; now {state_label(acq.state)}."
    return _finish(request, session, acq, key, notice)


@router.post("/acquisitions/{acquisition_id}/prefer")
def ui_prefer(
    acquisition_id: int,
    request: Request,
    session: DbSession,
    candidate_id: Annotated[int, Form()],
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.acquisition_id != acq.id:
        raise HTTPException(404, f"There is no candidate #{candidate_id} on this acquisition.")
    try:
        approval.prefer(session, acq, candidate, by="ui")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _finish(
        request,
        session,
        acq,
        key,
        f"Preferred {candidate.title}; Approve will fetch it.",
        advance=False,
    )


@router.post("/acquisitions/{acquisition_id}/reject")
def ui_reject(
    acquisition_id: int,
    request: Request,
    session: DbSession,
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
    try:
        approval.reject(session, acq, by="ui", reason=reason)
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return _finish(request, session, acq, key, f"Rejected #{acq.id}.")


@router.post("/acquisitions/{acquisition_id}/cancel")
def ui_cancel(acquisition_id: int, request: Request, session: DbSession) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
    try:
        approval.cancel(session, acq, by="ui")
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return _finish(request, session, acq, key, f"Cancelled #{acq.id}.")


@router.post("/acquisitions/{acquisition_id}/search")
async def ui_search(
    acquisition_id: int, request: Request, ctx: Ctx, session: DbSession
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
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
        notice = f"Searched; now {state_label(acq.state)}."
    return _finish(request, session, acq, key, notice, advance=False)


@router.post("/acquisitions/{acquisition_id}/retry-import")
async def ui_retry_import(
    acquisition_id: int, request: Request, ctx: Ctx, session: DbSession
) -> RedirectResponse:
    acq = _get(session, acquisition_id)
    key = _key(acq)
    try:
        await importer.retry_import(session, ctx, acq)
    except (ValueError, InvalidTransition) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _finish(
        request, session, acq, key, f"Import retried; now {state_label(acq.state)}.", advance=False
    )


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
    return _redirect(acq.id, f"Release group chosen; now {state_label(acq.state)}.")


@router.post("/requests")
async def ui_request(
    request: Request,
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
    kick_art(request.app)
    if acq.signal_id is not None and acq.signal_id <= last_signal:
        # The album was already in flight: this request was attached to it rather than
        # creating a new acquisition. Say so, or the id change goes unnoticed.
        return _redirect(
            acq.id,
            f"Already requested as #{acq.id} ({_state_phrase(acq.state)}); "
            "your request was attached to it.",
        )
    return _redirect(acq.id, f"Request #{acq.id} is {acq.state}.")
