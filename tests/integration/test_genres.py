"""Genres on targets: stored from the release-group lookup that creates a target, filled in
by the background job for targets that predate it (active rows first), shown two to a queue
row and three on the detail page (services/genres.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import AlbumTarget
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import genres
from hermes.ui.routes import _playlist
from tests.fixtures import load
from tests.integration.test_ui import BEETS, DUMMY_RG, PROWLARR, SLIP_RG, _html, _slip_search

pytestmark = pytest.mark.respx(assert_all_called=False)

RELEASE = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"  # a release in the Dummy group


@pytest.fixture
def client(settings: Settings, policy: Policy) -> Iterator[TestClient]:
    from alembic import command

    from hermes.cli import alembic_config

    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    with TestClient(app) as c:
        yield c


def _mock_stack(respx_mock: respx.Router) -> None:
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))


def _request_dummy(respx_mock: respx.Router, client: TestClient) -> int:
    """Portishead's Dummy by release MBID: the fixture group carries six genres."""
    respx_mock.get(f"{MB}/release-group/{RELEASE}").respond(
        404, json=load("musicbrainz/rg_missing")
    )
    respx_mock.get(f"{MB}/release/{RELEASE}").respond(json=load("musicbrainz/release_dummy"))
    respx_mock.get(f"{MB}/release-group/{DUMMY_RG}").respond(json=load("musicbrainz/rg_dummy"))
    _mock_stack(respx_mock)
    body = client.post("/api/requests", json={"mbid": RELEASE}).json()
    return int(body["id"])


def _target(client: TestClient, mbid: str) -> AlbumTarget:
    with client.app.state.session_factory() as session:
        t = session.scalar(select(AlbumTarget).where(AlbumTarget.release_group_mbid == mbid))
        assert t is not None
        session.expunge(t)
        return t


async def _tick(client: TestClient, **kw: object) -> dict[str, int]:
    with client.app.state.session_factory() as session:
        return await genres.tick(session, client.app.state.context, **kw)  # type: ignore[arg-type]


def test_request_stores_genres_and_pages_show_them(
    respx_mock: respx.Router, client: TestClient
) -> None:
    acq_id = _request_dummy(respx_mock, client)
    target = _target(client, DUMMY_RG)
    assert target.genres is not None
    assert [g["name"] for g in target.genres["top"]] == [
        "trip hop",
        "electronic",
        "downtempo",
        "alternative rock",
        "experimental",
    ]  # five kept, most votes first, ties by name ("rock" is the sixth)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["target"]["genres"][:3] == ["trip hop", "electronic", "downtempo"]

    queue = _html(client, "/queue").text
    assert '<span class="stack genres">trip hop · electronic</span>' in queue
    detail = _html(client, f"/acquisitions/{acq_id}").text
    assert "1994) · trip hop · electronic · downtempo</span>" in detail


def test_request_without_genres_stores_an_empty_answer(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """A group MusicBrainz has no genres for is recorded as looked up, not left for the job."""
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    _mock_stack(respx_mock)
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    assert _target(client, SLIP_RG).genres == {"top": []}
    queue = _html(client, "/queue").text
    assert 'class="stack genres"' not in queue


async def test_job_fills_older_targets_active_rows_first(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    _mock_stack(respx_mock)
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    # Targets from before genres existed: NULL, one of them with no acquisition at all.
    with client.app.state.session_factory() as session:
        session.add(AlbumTarget(release_group_mbid="older-rg", artist_name="Older", title="Album"))
        for t in session.scalars(select(AlbumTarget)):
            t.genres = None
        session.commit()
    slip = respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(
        json={
            **_slip_search()["release-groups"][0],
            "genres": [{"name": "industrial rock", "count": 3}],
        }
    )
    older = respx_mock.get(f"{MB}/release-group/older-rg").respond(
        404, json=load("musicbrainz/rg_missing")
    )

    assert (await _tick(client)) == {"fetched": 1, "missing": 1}
    assert slip.calls[0].request.url.params["inc"] == "releases+media+artist-credits+genres"
    # The active one (the request) went first although the orphan has the higher id.
    assert respx_mock.calls[-2].request.url.path.endswith(SLIP_RG)
    assert older.called
    assert _target(client, SLIP_RG).genres == {"top": [{"name": "industrial rock", "count": 3}]}
    assert _target(client, "older-rg").genres == {"top": []}
    assert (await _tick(client)) == {}  # nothing left to do


async def test_job_stops_when_musicbrainz_is_down(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    _mock_stack(respx_mock)
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    with client.app.state.session_factory() as session:
        for t in session.scalars(select(AlbumTarget)):
            t.genres = None
        session.commit()
    respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(500)
    assert (await _tick(client)) == {"unavailable": 1}
    assert _target(client, SLIP_RG).genres is None  # tried again next tick


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("Weekly Exploration for lbuser, week of 2026-08-24 Mon", "Weekly Exploration, 08-24"),
        ("Weekly Jams for someone, week of 2026-09-14 Mon", "Weekly Jams, 09-14"),
        ("My mixtape", "My mixtape"),
        (None, ""),
    ],
)
def test_playlist_names_are_shortened_on_rows(name: str | None, short: str) -> None:
    assert _playlist(name) == short
