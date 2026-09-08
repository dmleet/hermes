"""``hermes`` command line: serve, check, db upgrade."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from hermes.config import PlaylistMode, Policy, Settings, load_policy
from hermes.db import make_engine, make_session_factory

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from hermes.app import Clients
    from hermes.services.context import Context

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(db_app, name="db", help="Database migrations.")

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def alembic_config(settings: Settings) -> AlembicConfig:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def _policy(settings: Settings) -> Policy:
    if settings.hermes_config_path.exists():
        return load_policy(settings.hermes_config_path)
    typer.echo(f"policy file {settings.hermes_config_path} not found; using defaults", err=True)
    return Policy()


@app.command()
def serve(
    reload: bool = typer.Option(False, help="Restart on source changes (development)."),
) -> None:
    """Run the API and background jobs. Applies pending migrations first."""
    import uvicorn

    settings = Settings()
    alembic_command.upgrade(alembic_config(settings), "head")
    uvicorn.run(
        "hermes.app:create_app",
        factory=True,
        host=settings.hermes_host,
        port=settings.hermes_port,
        reload=reload,
        reload_dirs=["hermes"] if reload else None,
    )


@app.command()
def check() -> None:
    """Print the same report as GET /healthz and exit non-zero if anything is unhealthy."""
    from hermes.app import Clients, effective_policy

    settings = Settings()
    policy = effective_policy(settings, _policy(settings))
    clients = Clients.from_config(settings, policy)

    async def run() -> bool:
        try:
            results = await clients.health()
        finally:
            await clients.aclose()
        report = {r.name: r.as_dict() for r in results}
        typer.echo(json.dumps({"dry_run": policy.dry_run, "checks": report}, indent=2))
        return all(r.ok for r in results)

    raise typer.Exit(code=0 if asyncio.run(run()) else 1)


def _runtime() -> tuple[Settings, Policy, Engine, Clients, Context]:
    """Settings, effective policy, engine (migrated), clients and pipeline context."""
    from hermes.app import Clients, effective_policy

    settings = Settings()
    policy = effective_policy(settings, _policy(settings))
    alembic_command.upgrade(alembic_config(settings), "head")
    engine = make_engine(settings.database_url)
    clients = Clients.from_config(settings, policy)
    return settings, policy, engine, clients, clients.context(settings, policy)


@app.command()
def approve(acquisition_id: int, by: str = "cli") -> None:
    """Approve an acquisition that is waiting (respects dry_run)."""
    from hermes.domain.models import Acquisition
    from hermes.services import approval

    _, _, engine, clients, ctx = _runtime()

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                acq = session.get(Acquisition, acquisition_id)
                if acq is None:
                    raise typer.BadParameter(f"no acquisition {acquisition_id}")
                acq = await approval.approve(session, ctx, acq, by=by)
                typer.echo(f"acquisition {acq.id}: {acq.state}")
                for e in acq.events[-4:]:
                    typer.echo(f"  {e.at:%H:%M:%S} [{e.level}] {e.message}")
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command()
def observe() -> None:
    """Run one observer tick: poll Deluge for every active grab attempt."""
    from hermes.services import observer

    _, _, engine, clients, ctx = _runtime()

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                typer.echo(json.dumps(await observer.tick(session, ctx)))
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command()
def discover() -> None:
    """Run one discovery tick: ingest new ListenBrainz playlists (and extra_playlists)."""
    from hermes.services import discovery

    _, _, engine, clients, ctx = _runtime()
    if ctx.listenbrainz is None:
        raise typer.BadParameter(
            "set listenbrainz.user or listenbrainz.extra_playlists in the policy first"
        )

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                typer.echo(json.dumps(await discovery.tick(session, ctx)))
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command("ingest-playlist")
def ingest_playlist(
    mbid: str = typer.Argument(..., help="ListenBrainz playlist MBID"),
    mode: str = typer.Option("acquire", "--mode", help="acquire | ignore"),
) -> None:
    """Ingest one ListenBrainz playlist by MBID, as if it were listed in extra_playlists."""
    from hermes.services import discovery

    if mode not in ("acquire", "ignore"):
        raise typer.BadParameter("mode must be acquire or ignore")
    playlist_mode: PlaylistMode = "acquire" if mode == "acquire" else "ignore"
    _, _, engine, clients, ctx = _runtime()
    if ctx.listenbrainz is None:
        from hermes.integrations.listenbrainz import ListenBrainzClient

        settings = Settings()
        token = settings.listenbrainz_token
        ctx.listenbrainz = ListenBrainzClient(
            token=token.get_secret_value() if token else None,
            contact=settings.musicbrainz_contact,
        )

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                row = await discovery.ingest_playlist(session, ctx, mbid, mode=playlist_mode)
                typer.echo(f"{row.title or row.mbid}: {row.status} ({row.track_count} tracks)")
                typer.echo(json.dumps(row.summary))
        finally:
            if ctx.listenbrainz is not None:
                await ctx.listenbrainz.aclose()
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command("research")
def research() -> None:
    """Run one re-search tick: NO_MATCH rows past search.retry_days, up to max_retries."""
    from hermes.services import discovery

    _, _, engine, clients, ctx = _runtime()

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                typer.echo(json.dumps(await discovery.research_tick(session, ctx)))
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command("import-tick")
def import_tick() -> None:
    """Run one import tick: start imports for ready downloads, poll running ones."""
    from hermes.services import importer

    _, _, engine, clients, ctx = _runtime()

    async def run() -> None:
        try:
            with make_session_factory(engine)() as session:
                typer.echo(json.dumps(await importer.tick(session, ctx)))
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command()
def request(
    artist: str = typer.Argument(None, help="Album artist (with TITLE)"),
    title: str = typer.Argument(None, help="Album title"),
    mbid: str = typer.Option(None, "--mbid", help="Release group or release MBID instead"),
) -> None:
    """Submit a manual album request and print the outcome with its event trail."""
    from hermes.services.requests import submit_manual_request

    if not mbid and not (artist and title):
        raise typer.BadParameter("give ARTIST and TITLE, or --mbid")
    settings, policy, engine, clients, ctx = _runtime()

    async def run() -> int:
        try:
            with make_session_factory(engine)() as session:
                acq = await submit_manual_request(
                    session, ctx, artist=artist, title=title, mbid=mbid
                )
                target = acq.album_target
                typer.echo(f"acquisition {acq.id}: {acq.state}")
                if target:
                    typer.echo(
                        f"  target: {target.artist_name} - {target.title} "
                        f"({target.primary_type}, {target.first_release_year}) "
                        f"rg={target.release_group_mbid} library={target.library_status}"
                    )
                for c in sorted(acq.candidates, key=lambda c: (c.rank is None, c.rank or 0)):
                    mark = f"#{c.rank}" if c.rank else "--"
                    size = f"{(c.size_bytes or 0) / 1e6:.0f}MB"
                    free = " FL" if c.freeleech else ""
                    why = f"  ({c.rejected_reason})" if c.rejected_reason else ""
                    typer.echo(
                        f"  {mark:>3} {c.title[:70]:70} {c.indexer_name} {size} "
                        f"s={c.seeders}{free}{why}"
                    )
                for e in acq.events:
                    typer.echo(f"  {e.at:%H:%M:%S} [{e.level}] {e.message}")
                    if "candidates" in e.data and acq.state == "NEEDS_REVIEW":
                        for c in e.data["candidates"]:
                            flag = "ok " if c["policy_ok"] else "no "
                            types = f"[{c['primary_type']} {c['secondary_types']}]"
                            why = f"  ({c['rejected_reason']})" if c["rejected_reason"] else ""
                            typer.echo(
                                f"      {flag} {c['score']:.2f} {c['artist']} - {c['title']} "
                                f"{types} {c['release_group_mbid']}{why}"
                            )
                return acq.id
        finally:
            await clients.aclose()
            engine.dispose()

    asyncio.run(run())


@app.command("parse-title")
def parse_title_cmd(
    title: Annotated[list[str], typer.Argument(help="One or more release titles")],
) -> None:
    """Show how the title parser reads tracker titles (useful for building the corpus)."""
    from hermes.services.title_parser import parse_title

    for t in title:
        p = parse_title(t)
        typer.echo(t)
        for k, v in p.as_dict().items():
            if v not in (None, [], False):
                typer.echo(f"    {k}: {v}")
        typer.echo(f"    ok: {p.ok}")


@app.command("validate-config")
def validate_config() -> None:
    """Load the policy file and report validation errors."""
    settings = Settings()
    policy = load_policy(settings.hermes_config_path)
    typer.echo(policy.model_dump_json(indent=2))


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    alembic_command.upgrade(alembic_config(Settings()), revision)


@db_app.command("revision")
def db_revision(message: str) -> None:
    """Autogenerate a migration from model changes."""
    alembic_command.revision(alembic_config(Settings()), message=message, autogenerate=True)
