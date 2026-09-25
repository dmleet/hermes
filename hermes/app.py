"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from hermes import __version__
from hermes.api.routes import router as api_router
from hermes.config import Policy, Settings, load_policy
from hermes.db import make_engine, make_session_factory
from hermes.integrations import HealthResult, not_configured
from hermes.integrations.beets import BeetsClient
from hermes.integrations.coverart import CoverArtClient
from hermes.integrations.deluge import DelugeClient
from hermes.integrations.listenbrainz import ListenBrainzClient
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.integrations.navidrome import NavidromeClient
from hermes.integrations.prowlarr import ProwlarrClient
from hermes.job_status import JobStatus, job_problems
from hermes.services import art, discovery, genres, importer, observer
from hermes.services.context import Context
from hermes.ui.routes import render_error
from hermes.ui.routes import router as ui_router

UI_DIR = Path(__file__).parent / "ui"

log = logging.getLogger("hermes")


@dataclass
class Clients:
    prowlarr: ProwlarrClient | None
    deluge: dict[str, DelugeClient]  # keyed by policy.deluge.instances name
    beets: BeetsClient
    musicbrainz: MusicBrainzClient
    navidrome: NavidromeClient | None = None
    listenbrainz: ListenBrainzClient | None = None
    coverart: CoverArtClient | None = None
    deluge_instances_configured: bool = False

    @classmethod
    def from_config(cls, settings: Settings, policy: Policy) -> Clients:
        prowlarr = None
        if settings.prowlarr_url and settings.prowlarr_api_key:
            prowlarr = ProwlarrClient(
                settings.prowlarr_url, settings.prowlarr_api_key.get_secret_value()
            )
        deluge: dict[str, DelugeClient] = {}
        if settings.deluge_password:
            password = settings.deluge_password.get_secret_value()
            deluge = {
                name: DelugeClient(inst.url, password, name=f"deluge:{name}")
                for name, inst in policy.deluge.instances.items()
            }
        navidrome = None
        if settings.navidrome_url and settings.navidrome_user and settings.navidrome_password:
            navidrome = NavidromeClient(
                settings.navidrome_url,
                settings.navidrome_user,
                settings.navidrome_password.get_secret_value(),
            )
        listenbrainz = None
        if policy.listenbrainz.user or policy.listenbrainz.extra_playlists:
            token = settings.listenbrainz_token
            listenbrainz = ListenBrainzClient(
                token=token.get_secret_value() if token else None,
                contact=settings.musicbrainz_contact,
            )
        return cls(
            prowlarr=prowlarr,
            deluge=deluge,
            beets=BeetsClient(policy.beets.agent_url),
            musicbrainz=MusicBrainzClient(settings.musicbrainz_contact),
            navidrome=navidrome,
            listenbrainz=listenbrainz,
            coverart=CoverArtClient(settings.musicbrainz_contact) if policy.art.enabled else None,
            deluge_instances_configured=bool(policy.deluge.instances),
        )

    def context(self, settings: Settings, policy: Policy) -> Context:
        return Context(
            policy=policy,
            musicbrainz=self.musicbrainz,
            beets=self.beets,
            prowlarr=self.prowlarr,
            deluge=self.deluge,
            navidrome=self.navidrome,
            listenbrainz=self.listenbrainz,
            coverart=self.coverart,
            art_dir=settings.hermes_data_dir / "art" if self.coverart else None,
        )

    async def aclose(self) -> None:
        for client in (
            self.prowlarr,
            *self.deluge.values(),
            self.beets,
            self.musicbrainz,
            self.navidrome,
            self.listenbrainz,
            self.coverart,
        ):
            if client is not None:
                await client.aclose()

    async def health(self) -> list[HealthResult]:
        deluge_checks = [d.health() for d in self.deluge.values()]
        if not deluge_checks:
            deluge_checks = [
                _ready(
                    HealthResult(
                        "deluge",
                        ok=False,
                        detail="deluge.instances configured but DELUGE_PASSWORD is not set",
                    )
                    if self.deluge_instances_configured
                    else not_configured("deluge")
                )
            ]
        checks = [
            self.prowlarr.health() if self.prowlarr else _ready(not_configured("prowlarr")),
            *deluge_checks,
            self.beets.health(),
            self.musicbrainz.health(),
            self.navidrome.health() if self.navidrome else _ready(not_configured("navidrome")),
            self.listenbrainz.health()
            if self.listenbrainz
            else _ready(not_configured("listenbrainz")),
        ]
        return list(await asyncio.gather(*checks))


async def _ready(result: HealthResult) -> HealthResult:
    return result


_KEPT_TORRENT = re.compile(r"^\d+-[0-9a-f]{40}\.torrent$")


def remove_kept_torrents(directory: Path) -> int:
    """Earlier versions kept a copy of every submitted .torrent under the data directory
    as ``<acquisition id>-<infohash>.torrent``. A private tracker's torrent carries the
    account's passkey in its announce URL, and Deluge holds the file anyway, so those
    copies are removed on startup. Only that naming is touched and the directory is left
    in place: a data directory pointed at a download share must never lose the torrent
    client's own files. Returns the count."""
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or not _KEPT_TORRENT.match(path.name):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove kept torrent %s: %s", path.name, exc)
    if removed:
        log.warning(
            "removed %d kept torrent file(s) from %s: Hermes no longer stores them",
            removed,
            directory,
        )
    return removed


def effective_policy(settings: Settings, policy: Policy) -> Policy:
    if settings.hermes_dry_run is not None and settings.hermes_dry_run != policy.dry_run:
        policy = policy.model_copy(update={"dry_run": settings.hermes_dry_run})
    return policy


def create_app(
    settings: Settings | None = None,
    policy: Policy | None = None,
    clients: Clients | None = None,
    *,
    scheduler: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    if policy is None:
        policy = (
            load_policy(settings.hermes_config_path)
            if settings.hermes_config_path.exists()
            else Policy()
        )
    policy = effective_policy(settings, policy)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.policy = policy
        app.state.engine = make_engine(settings.database_url)
        app.state.session_factory = make_session_factory(app.state.engine)
        app.state.clients = clients or Clients.from_config(settings, policy)
        app.state.context = app.state.clients.context(settings, policy)
        remove_kept_torrents(settings.hermes_data_dir / "torrents")
        app.state.scheduler = None
        app.state.job_status = {}
        if scheduler:
            app.state.scheduler = _start_scheduler(app)
        try:
            yield
        finally:
            if app.state.scheduler:
                app.state.scheduler.shutdown(wait=False)
            await app.state.clients.aclose()
            app.state.engine.dispose()

    app = FastAPI(title="Hermes", version=__version__, lifespan=lifespan)
    app.include_router(api_router)
    app.include_router(ui_router)
    # App icons for "Add to Home Screen"; the manifest itself is a UI route.
    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")

    def _wants_html(request: Request) -> bool:
        return not request.url.path.startswith(("/api", "/docs", "/openapi", "/healthz")) and (
            "text/html" in request.headers.get("accept", "")
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if _wants_html(request):
            return render_error(request, exc.status_code, str(exc.detail))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        if _wants_html(request):
            return render_error(request, 422, "The form was incomplete or invalid.")
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @app.get("/livez")
    async def livez(request: Request) -> JSONResponse:
        """For the pod's probes: the process serves and its database answers. No dependency
        is called, so an outage elsewhere (a Prowlarr down all day, a Deluge hung on a
        mount) never takes the UI out of the Service; /healthz is for people."""
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - a probe answer, not an error page
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=503)
        return JSONResponse({"ok": True})

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        state = request.app.state
        checks: dict[str, Any] = {}
        try:
            with state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["db"] = {"ok": True, "configured": True, "detail": "ok"}
        except Exception as exc:  # noqa: BLE001 - health must not raise
            checks["db"] = {"ok": False, "configured": True, "detail": str(exc)}
        for result in await state.clients.health():
            checks[result.name] = result.as_dict()
        if state.job_status:
            problems = job_problems(state.job_status)
            checks["jobs"] = {
                "ok": not problems,
                "configured": True,
                "detail": "; ".join(problems) or "ok",
                "data": {
                    job_id: {
                        "last_ok": st.last_ok.isoformat() if st.last_ok else None,
                        "failing_since": st.failing_since.isoformat() if st.failing_since else None,
                        "last_error": st.last_error,
                    }
                    for job_id, st in state.job_status.items()
                },
            }
        ok = all(c["ok"] for c in checks.values())
        body = {
            "ok": ok,
            "version": __version__,
            "dry_run": state.policy.dry_run,
            "checks": checks,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


def kick_art(app: FastAPI) -> None:
    """Run the art job now rather than at its next interval: called after a manual request
    resolves, so the page usually has art by its first refresh. A no-op without a scheduler
    (tests, the CLI) or with art disabled."""
    sched = getattr(app.state, "scheduler", None)
    if sched is not None and sched.get_job("art") is not None:
        sched.modify_job("art", next_run_time=datetime.now())


def _job(
    app: FastAPI, job_id: str, tick: Callable[[Session, Context], Awaitable[dict[str, int]]]
) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        status: JobStatus | None = app.state.job_status.get(job_id)
        with app.state.session_factory() as session:
            try:
                counts = await tick(session, app.state.context)
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the scheduler
                log.exception("%s tick failed", job_id)
                if status is not None:
                    status.failing_since = status.failing_since or datetime.now(UTC)
                    status.last_error = f"{type(exc).__name__}: {exc}"[:300]
                return
        if status is not None:
            status.last_ok, status.failing_since = datetime.now(UTC), None
        if counts:
            log.info("%s: %s", job_id, counts)

    return run


def _start_scheduler(app: FastAPI) -> AsyncIOScheduler:
    """In-process jobs (docs/plan.md A6). The observer tick doubles as the startup reconcile."""

    observe_job = _job(app, "observe", observer.tick)
    import_job = _job(app, "import", importer.tick)
    discover_job = _job(app, "discover", discovery.tick)
    art_job = _job(app, "art", art.tick)
    genres_job = _job(app, "genres", genres.tick)
    research_job = _job(app, "research", discovery.research_tick)

    sched = AsyncIOScheduler()
    policy = app.state.policy
    jobs = (
        ("observe", observe_job, {"seconds": policy.deluge.poll_seconds}),
        ("import", import_job, {"seconds": policy.deluge.poll_seconds}),
        ("discover", discover_job, {"hours": policy.listenbrainz.poll_hours}),
        ("research", research_job, {"hours": 24}),
        ("genres", genres_job, {"minutes": 5}),
        *((("art", art_job, {"minutes": 5}),) if app.state.context.coverart else ()),
    )
    for job_id, func, every in jobs:
        app.state.job_status[job_id] = JobStatus(every=timedelta(**every))
        sched.add_job(
            func,
            "interval",
            **every,
            id=job_id,
            max_instances=1,
            coalesce=True,
            next_run_time=None,
        )
    sched.start()
    # Reconcile immediately on startup, then on the interval. Every tick is idempotent.
    for job_id, _, _ in jobs:
        sched.modify_job(job_id, next_run_time=datetime.now())
    return sched
