"""Phase B of docs/ui-plan.md: album art from the Cover Art Archive, fetched out of band and
stored as served. Cosmetic: never inline, never on /healthz, never a broken image."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes.app import Clients, create_app, kick_art
from hermes.config import Policy, Settings
from hermes.domain.models import Acquisition, AlbumTarget
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.coverart import DEFAULT_BASE_URL as CAA
from hermes.integrations.coverart import CoverArtClient
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import art
from tests.fixtures import load
from tests.integration.test_ui import BEETS, PROWLARR, SLIP_RG, _html, _slip_search

pytestmark = pytest.mark.respx(assert_all_called=False)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
ARCHIVE = "https://archive.org/download/mbid-x/mbid-x-1_thumb500.jpg"


@pytest.fixture
def client(settings: Settings, policy: Policy) -> Iterator[TestClient]:
    from alembic import command

    from hermes.cli import alembic_config

    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
    assert clients.coverart is not None
    asyncio.run(clients.coverart.aclose())  # replaced by one with no rate limit
    clients.coverart = CoverArtClient("t@example.com", min_interval=0.0)
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    with TestClient(app) as c:
        yield c


def _request_slip(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})


def _target(client: TestClient) -> AlbumTarget:
    with client.app.state.session_factory() as session:
        t = session.scalar(select(AlbumTarget).where(AlbumTarget.release_group_mbid == SLIP_RG))
        assert t is not None
        session.expunge(t)
        return t


async def _tick(client: TestClient, **kw: object) -> dict[str, int]:
    with client.app.state.session_factory() as session:
        return await art.tick(session, client.app.state.context, **kw)  # type: ignore[arg-type]


async def test_fetch_serve_and_render(
    respx_mock: respx.Router, client: TestClient, settings: Settings
) -> None:
    _request_slip(respx_mock, client)
    assert _target(client).art_status == "pending"
    queue = _html(client, "/queue").text
    assert '<span class="art" aria-hidden="true">N</span>' in queue, "placeholder until fetched"
    assert client.get(f"/art/{SLIP_RG}.jpg").status_code == 404

    # The archive redirects to archive.org; the bytes are stored as served.
    respx_mock.get(f"{CAA}/release-group/{SLIP_RG}/front-500").respond(
        307, headers={"location": ARCHIVE}
    )
    respx_mock.get(ARCHIVE).respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
    assert await _tick(client) == {"fetched": 1}
    path = settings.hermes_data_dir / "art" / f"{SLIP_RG}.jpg"
    assert path.read_bytes() == JPEG and not list(path.parent.glob("*.tmp"))
    t = _target(client)
    assert t.art_status == "fetched" and t.art_checked_at is not None and t.art_failures == 0

    resp = client.get(f"/art/{SLIP_RG}.jpg")
    assert resp.status_code == 200 and resp.content == JPEG
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    img = f'<img class="art" src="/art/{SLIP_RG}.jpg" width="56" height="56" loading="lazy"'
    assert img in _html(client, "/queue").text
    head = _html(client, "/acquisitions/1").text.split("<h1>")[0]
    assert (
        f'<img class="art big" src="/art/{SLIP_RG}.jpg" width="160" height="160" decoding' in head
    )
    assert 'loading="lazy"' not in head, "the page-head cover is above the fold: eager"
    # Nothing due any more: the next tick does nothing and calls nobody.
    calls = respx_mock.calls.call_count
    assert await _tick(client) == {} and respx_mock.calls.call_count == calls

    # The route only ever serves what the job wrote.
    assert client.get("/art/not-an-mbid.jpg").status_code == 404
    assert client.get("/art/00000000-0000-0000-0000-000000000000.jpg").status_code == 404
    assert client.get("/art/..%2F..%2Fhermes.db.jpg").status_code == 404


async def test_missing_failed_and_backoff(respx_mock: respx.Router, client: TestClient) -> None:
    _request_slip(respx_mock, client)
    route = respx_mock.get(f"{CAA}/release-group/{SLIP_RG}/front-500")

    route.respond(404)
    assert await _tick(client) == {"missing": 1}
    t = _target(client)
    assert t.art_status == "missing" and t.art_failures == 0
    assert await _tick(client) == {}, "a 404 is remembered"
    # ... for a month.
    later = datetime.now(UTC) + art.MISSING_RETRY + timedelta(minutes=1)
    route.side_effect = httpx.ReadTimeout("slow")
    assert await _tick(client, now=later) == {"failed": 1}
    assert _target(client).art_failures == 1
    # Failed: not yet; after an hour, yes; the second failure waits a day.
    assert await _tick(client, now=later + timedelta(minutes=30)) == {}
    assert await _tick(client, now=later + timedelta(hours=1, minutes=1)) == {"failed": 1}
    assert _target(client).art_failures == 2
    assert await _tick(client, now=later + timedelta(hours=3)) == {}
    route.side_effect = None
    route.respond(200, content=PNG, headers={"content-type": "image/png"})
    assert await _tick(client, now=later + timedelta(days=1, hours=2)) == {"fetched": 1}
    assert _target(client).art_failures == 0
    resp = client.get(f"/art/{SLIP_RG}.jpg")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"

    # Not an image, or an unexpected status, is a failure to retry, not a file.
    for kwargs in (
        {"status_code": 200, "content": b"<html>", "headers": {"content-type": "text/html"}},
        {"status_code": 503},
        {"status_code": 200, "content": b"", "headers": {"content-type": "image/jpeg"}},
    ):
        with client.app.state.session_factory() as session:
            t2 = session.scalar(select(AlbumTarget))
            assert t2 is not None
            t2.art_status = "pending"
            session.commit()
        route.respond(**kwargs)  # type: ignore[arg-type]
        assert await _tick(client) == {"failed": 1}


async def test_preferred_release_first_and_active_rows_first(
    respx_mock: respx.Router, client: TestClient
) -> None:
    _request_slip(respx_mock, client)
    with client.app.state.session_factory() as session:
        t = session.scalar(select(AlbumTarget))
        assert t is not None
        t.preferred_release_mbid = "11111111-1111-1111-1111-111111111111"
        # A second, finished target with nothing active: it goes after the queue's rows.
        done = AlbumTarget(
            release_group_mbid="22222222-2222-2222-2222-222222222222",
            artist_name="Old",
            title="Album",
        )
        session.add(done)
        session.add(Acquisition(album_target=done, state=S.IMPORTED, origin="manual"))
        session.commit()
    release = respx_mock.get(f"{CAA}/release/11111111-1111-1111-1111-111111111111/front-500")
    release.respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
    group = respx_mock.get(f"{CAA}/release-group/{SLIP_RG}/front-500")
    group.respond(404)
    old = respx_mock.get(f"{CAA}/release-group/22222222-2222-2222-2222-222222222222/front-500")
    old.respond(200, content=JPEG, headers={"content-type": "image/jpeg"})

    assert await _tick(client, limit=1) == {"fetched": 1}
    assert release.called and not group.called and not old.called
    assert await _tick(client, limit=1) == {"fetched": 1}
    assert old.called

    # The release's cover is missing: fall through to the group.
    with client.app.state.session_factory() as session:
        t = session.scalar(select(AlbumTarget).where(AlbumTarget.release_group_mbid == SLIP_RG))
        assert t is not None
        t.art_status = "pending"
        session.commit()
    release.respond(404)
    assert await _tick(client) == {"missing": 1} and group.called


def test_disabled_and_no_scheduler(settings: Settings) -> None:
    policy = Policy.model_validate({"art": {"enabled": False}})
    clients = Clients.from_config(settings, policy)
    assert clients.coverart is None
    ctx = clients.context(settings, policy)
    assert ctx.coverart is None and ctx.art_dir is None
    app = FastAPI()
    app.state.scheduler = None
    kick_art(app)  # no scheduler: nothing to kick, nothing raised
    enabled = Clients.from_config(settings, Policy())
    assert enabled.coverart is not None
    assert enabled.context(settings, Policy()).art_dir == settings.hermes_data_dir / "art"


async def test_coverart_health_is_never_red() -> None:
    result = await CoverArtClient("t@example.com").health()
    assert result.ok and result.name == "coverart"
