"""Discovery: ListenBrainz playlists become signals and acquisitions, idempotently, and the
re-search tick retries NO_MATCH rows until it gives up."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import Acquisition, Playlist, Signal, utcnow
from hermes.integrations.listenbrainz import DEFAULT_BASE_URL as LB
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import discovery
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

BEETS = "http://beets.test:8338"
PROWLARR = "http://prowlarr.test"
WE = "55f2a01f-3974-46ca-8461-a8a84886eba4"  # weekly exploration
DJ = "be1f3f2b-9ac3-41db-a0b2-3a21ba068278"  # daily jams (ignored by default)
WJ = "54cc305b-f68e-4ab8-a2b9-3188727a3b5b"  # weekly jams (ignored by default)
JUNO_RG = "e3146a45-0834-44ad-a8cc-92c37930bec8"
RECORDINGS = {
    "dad4820e-4887-4d1e-83e2-6fe1166b3f49": "recording_dad4820e",
    "0fffc332-04c6-4619-aa9a-3d043c447612": "recording_0fffc332",
    "503ea09d-91b5-467c-b030-8677da9656d6": "recording_503ea09d",
}


def _policy(**listenbrainz: object) -> Policy:
    return Policy.model_validate(
        {
            "listenbrainz": {"user": "lbuser", **listenbrainz},
            "beets": {"agent_url": BEETS},
            "quality": {"max_sample_rate_khz": None},
            "deluge": {"instances": {"dev": {"url": "http://deluge.test", "indexers": ["X"]}}},
        }
    )


@pytest.fixture
def client(
    respx_mock: respx.Router, settings: Settings, request: pytest.FixtureRequest
) -> Iterator[TestClient]:
    from alembic import command

    from hermes.cli import alembic_config

    policy = getattr(request, "param", None) or _policy()
    respx_mock.get(f"{LB}/user/lbuser/playlists/createdfor").respond(
        json=load("listenbrainz/createdfor")
    )
    respx_mock.get(f"{LB}/playlist/{WE}").respond(
        json=load("listenbrainz/playlist_weekly_exploration")
    )
    for mbid, name in RECORDINGS.items():
        respx_mock.get(f"{MB}/recording/{mbid}").respond(json=load(f"musicbrainz/{name}"))
    respx_mock.get(f"{MB}/release-group/{JUNO_RG}").respond(json=load("musicbrainz/rg_juno"))
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])

    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    with TestClient(app) as c:
        yield c


async def _tick(client: TestClient) -> dict[str, int]:
    app = client.app
    with app.state.session_factory() as session:
        return await discovery.tick(session, app.state.context)


async def _research(client: TestClient) -> dict[str, int]:
    app = client.app
    with app.state.session_factory() as session:
        return await discovery.research_tick(session, app.state.context)


async def test_weekly_exploration_becomes_signals_and_one_acquisition(
    client: TestClient, respx_mock: respx.Router
) -> None:
    counts = await _tick(client)
    assert counts == {"ingested": 1, "ignored": 2}

    with client.app.state.session_factory() as session:
        playlists = {p.mbid: p for p in session.scalars(select(Playlist))}
        assert playlists[WE].status == "ingested" and playlists[WE].track_count == 3
        assert playlists[WE].summary == {"no_match": 1, "nothing_acquirable": 2}
        assert playlists[DJ].status == "ignored" and playlists[WJ].mode == "ignore"
        signals = session.scalars(select(Signal).order_by(Signal.position)).all()
        assert [s.resolution_status for s in signals] == ["ignored", "ignored", "resolved"]
        assert "Remix" in (signals[0].resolution_note or "")
        assert signals[2].album_target is not None
        assert signals[2].album_target.release_group_mbid == JUNO_RG
        acqs = session.scalars(select(Acquisition)).all()
        assert len(acqs) == 1 and acqs[0].origin == "auto" and acqs[0].state == "NO_MATCH"
        assert acqs[0].signal_id == signals[2].id
        first = acqs[0].events[0].message
        assert first.startswith("discovered in Weekly Exploration for lbuser")
        assert "Her Entrance is on Parra for Cuva - Juno (Album, 2021)" in first

    # The queue names the discovered album and the API says where it came from.
    body = client.get(f"/api/acquisitions/{acqs[0].id}").json()
    assert body["requested"] == "Parra for Cuva - Her Entrance"
    assert "Juno" in client.get("/queue", headers={"accept": "text/html"}).text

    # Idempotent: the next tick changes nothing and asks ListenBrainz for no tracks.
    calls_before = respx_mock.get(f"{LB}/playlist/{WE}").call_count
    assert await _tick(client) == {"unchanged": 3}
    assert respx_mock.get(f"{LB}/playlist/{WE}").call_count == calls_before
    with client.app.state.session_factory() as session:
        assert session.scalar(select(Signal.id).order_by(Signal.id.desc())) == 3


async def test_same_recording_again_attaches_without_asking_musicbrainz(
    client: TestClient, respx_mock: respx.Router
) -> None:
    await _tick(client)
    # A curated playlist containing the same track, configured as an extra playlist.
    extra = "aaaaaaaa-1111-4222-8333-444444444444"
    jspf = load("listenbrainz/playlist_weekly_exploration")
    jspf["playlist"]["identifier"] = f"https://listenbrainz.org/playlist/{extra}"
    jspf["playlist"]["title"] = "Curated"
    del jspf["playlist"]["extension"]  # a hand-made playlist has no patch metadata
    respx_mock.get(f"{LB}/playlist/{extra}").respond(json=jspf)
    mb_calls = respx_mock.get(f"{MB}/recording/503ea09d-91b5-467c-b030-8677da9656d6").call_count

    app = client.app
    with app.state.session_factory() as session:
        row = await discovery.ingest_playlist(session, app.state.context, extra, mode="acquire")
        assert row.status == "ingested" and row.source == "extra" and row.title == "Curated"
        assert row.summary == {"attached": 1, "nothing_acquirable": 2}
        acqs = session.scalars(select(Acquisition)).all()
        assert len(acqs) == 1
        assert acqs[0].events[-1].message == "discovered again in Curated #3"
    assert (
        respx_mock.get(f"{MB}/recording/503ea09d-91b5-467c-b030-8677da9656d6").call_count
        == mb_calls
    )


async def test_listing_failure_is_reported_not_raised(
    client: TestClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{LB}/user/lbuser/playlists/createdfor").respond(503)
    assert await _tick(client) == {"listing_failed": 1}


async def test_research_waits_then_retries_then_gives_up(client: TestClient) -> None:
    await _tick(client)
    # Fresh NO_MATCH: too soon to search again.
    assert await _research(client) == {"waiting": 1}

    app = client.app
    with app.state.session_factory() as session:
        acq = session.scalars(select(Acquisition)).one()
        acq.updated_at = utcnow() - timedelta(days=8)
        session.commit()
    assert await _research(client) == {"no_match": 1}
    with app.state.session_factory() as session:
        acq = session.scalars(select(Acquisition)).one()
        assert acq.search_retries == 2
        acq.search_retries = app.state.policy.search.max_retries
        acq.updated_at = utcnow() - timedelta(days=8)
        session.commit()
    assert await _research(client) == {"gave_up": 1}
    with app.state.session_factory() as session:
        acq = session.scalars(select(Acquisition)).one()
        assert acq.state == "REJECTED" and "gave up" in acq.events[-1].message


async def test_research_searches_resolved_rows_that_never_were(client: TestClient) -> None:
    app = client.app
    with app.state.session_factory() as session:
        # A row left at RESOLVED (Prowlarr was down, or it predates the search stage).
        from hermes.domain.models import AlbumTarget, Origin
        from hermes.domain.state import AcquisitionState as S
        from hermes.domain.state import transition

        target = AlbumTarget(release_group_mbid=JUNO_RG, artist_name="Parra for Cuva", title="Juno")
        session.add(target)
        acq = Acquisition(album_target=target, state=S.MANUAL, origin=Origin.MANUAL)
        session.add(acq)
        session.flush()
        transition(session, acq, S.RESOLVED, "resolved")
        session.commit()
    assert await _research(client) == {"no_match": 1}


async def test_concurrent_ticks_do_not_double_process(client: TestClient) -> None:
    """The startup reconcile and a manual tick can coincide (seen live: duplicate
    'discovered again' events); discovery is serialised per process."""
    import asyncio

    app = client.app
    with app.state.session_factory() as s1, app.state.session_factory() as s2:
        results = await asyncio.gather(
            discovery.tick(s1, app.state.context), discovery.tick(s2, app.state.context)
        )
    assert sorted(results, key=str) == [{"ingested": 1, "ignored": 2}, {"unchanged": 3}]
    with app.state.session_factory() as session:
        assert len(session.scalars(select(Signal)).all()) == 3
        assert len(session.scalars(select(Acquisition)).all()) == 1


HER_ENTRANCE = "503ea09d-91b5-467c-b030-8677da9656d6"  # the one track that resolves (Juno)


async def test_musicbrainz_outage_mid_playlist_resumes_next_tick(
    client: TestClient, respx_mock: respx.Router
) -> None:
    """Review H1: a MusicBrainz failure used to mark the track failed and the playlist
    ingested, losing the rest of the week. Now the track stays pending, the playlist stays
    partial, and the next tick finishes it with no duplicates."""
    route = respx_mock.get(f"{MB}/recording/{HER_ENTRANCE}")
    route.mock(side_effect=httpx.ConnectError("down"))
    assert await _tick(client) == {"partial": 1, "ignored": 2}
    app = client.app
    with app.state.session_factory() as session:
        row = session.scalars(select(Playlist).where(Playlist.mbid == WE)).one()
        assert row.status == "partial" and "MusicBrainz" in (row.note or "")
        signals = session.scalars(select(Signal).order_by(Signal.position)).all()
        assert [x.resolution_status for x in signals] == ["ignored", "ignored", "pending"]
        assert session.scalars(select(Acquisition)).all() == []

    route.mock(return_value=httpx.Response(200, json=load("musicbrainz/recording_503ea09d")))
    assert await _tick(client) == {"ingested": 1, "unchanged": 2}
    with app.state.session_factory() as session:
        row = session.scalars(select(Playlist).where(Playlist.mbid == WE)).one()
        assert row.status == "ingested" and row.note is None
        assert row.summary == {"nothing_acquirable": 2, "no_match": 1}
        assert len(session.scalars(select(Signal)).all()) == 3
        acqs = session.scalars(select(Acquisition)).all()
        assert len(acqs) == 1 and acqs[0].state == "NO_MATCH"


async def test_prowlarr_outage_fails_retryably_and_research_retries(
    client: TestClient, respx_mock: respx.Router
) -> None:
    """Review H2: a search that failed on a Prowlarr outage parks the row as FAILED
    (retryable); the daily job now picks it up."""
    search = respx_mock.get(f"{PROWLARR}/api/v1/search")
    search.mock(side_effect=httpx.ConnectError("down"))
    assert await _tick(client) == {"ingested": 1, "ignored": 2}
    app = client.app
    with app.state.session_factory() as session:
        row = session.scalars(select(Playlist).where(Playlist.mbid == WE)).one()
        assert row.summary == {"nothing_acquirable": 2, "failed": 1}
        acq = session.scalars(select(Acquisition)).one()
        assert acq.state == "FAILED"
        assert acq.events[-1].data.get("retryable") is True
    search.mock(return_value=httpx.Response(200, json=[]))
    assert await _research(client) == {"no_match": 1}


async def test_beets_outage_leaves_resolved_and_research_checks_the_library_first(
    client: TestClient, respx_mock: respx.Router
) -> None:
    """Review M3: a row left at RESOLVED (beets was down) gets its library check before
    any search when the daily job finishes it."""
    library = respx_mock.get(url__regex=rf"{BEETS}/library/.*")
    library.mock(side_effect=httpx.ConnectError("down"))
    counts = await _tick(client)
    assert counts == {"partial": 1, "ignored": 2}, counts
    app = client.app
    with app.state.session_factory() as session:
        acq = session.scalars(select(Acquisition)).one()
        assert acq.state == "RESOLVED" and acq.album_target.library_status == "unknown"
    library.mock(return_value=httpx.Response(200, json={"albums": []}))
    assert await _research(client) == {"no_match": 1}
    with app.state.session_factory() as session:
        acq = session.scalars(select(Acquisition)).one()
        messages = [e.message for e in acq.events]
        assert "not in the library; ready for search" in messages
        assert messages.index("not in the library; ready for search") < len(messages) - 1
        assert acq.album_target.library_status == "missing"


async def test_research_does_not_clobber_a_row_moved_meanwhile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: rows are listed once, then each search takes seconds; a row cancelled in
    the UI meanwhile must stay cancelled."""
    from hermes.domain.models import AlbumTarget, Origin
    from hermes.domain.state import AcquisitionState as S
    from hermes.domain.state import transition
    from hermes.services import discovery as disc

    await _tick(client)  # acquisition 1: Juno, NO_MATCH
    app = client.app
    with app.state.session_factory() as session:
        target = AlbumTarget(release_group_mbid="x" * 36, artist_name="A", title="B")
        session.add(target)
        acq = Acquisition(album_target=target, state=S.MANUAL, origin=Origin.MANUAL)
        session.add(acq)
        session.flush()
        transition(session, acq, S.RESOLVED, "r")
        transition(session, acq, S.SEARCHING, "s")
        transition(session, acq, S.NO_MATCH, "n")
        for a in session.scalars(select(Acquisition)):
            a.updated_at = utcnow() - timedelta(days=8)
        session.commit()
        second_id = acq.id

    real = disc.search_and_decide
    cancelled: list[int] = []

    async def cancel_the_other_then_search(session, ctx, acq):
        if not cancelled:
            with app.state.session_factory() as other:
                victim = other.get(Acquisition, second_id)
                transition(other, victim, S.CANCELLED, "cancelled by ui")
                other.commit()
            cancelled.append(second_id)
        return await real(session, ctx, acq)

    monkeypatch.setattr(disc, "search_and_decide", cancel_the_other_then_search)
    counts = await _research(client)
    assert counts == {"no_match": 1, "moved": 1}, counts
    with app.state.session_factory() as session:
        assert session.get(Acquisition, second_id).state == "CANCELLED"


async def test_interrupted_ingest_resumes_without_duplicates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M5: a crash mid-ingest (anything, not only an outage) leaves the playlist
    partial and the unfinished signal pending; the next tick completes it once."""
    from hermes.services import discovery as disc

    real = disc.resolve_recording
    calls: list[str] = []

    async def crash_on_third(mb, policy, mbid):
        calls.append(mbid)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return await real(mb, policy, mbid)

    monkeypatch.setattr(disc, "resolve_recording", crash_on_third)
    with pytest.raises(RuntimeError):
        await _tick(client)
    app = client.app
    with app.state.session_factory() as session:
        row = session.scalars(select(Playlist).where(Playlist.mbid == WE)).one()
        assert row.status == "partial"
        signals = session.scalars(select(Signal).order_by(Signal.position)).all()
        assert [x.resolution_status for x in signals] == ["ignored", "ignored", "pending"]
    monkeypatch.setattr(disc, "resolve_recording", real)
    # The crash also stopped the tick before the third listed playlist was recorded.
    assert await _tick(client) == {"ingested": 1, "unchanged": 1, "ignored": 1}
    with app.state.session_factory() as session:
        assert len(session.scalars(select(Signal)).all()) == 3
        acqs = session.scalars(select(Acquisition)).all()
        assert len(acqs) == 1
        assert not [e for e in acqs[0].events if e.message.startswith("discovered again")]


def test_one_active_acquisition_per_target_is_a_database_fact(client: TestClient) -> None:
    """Review M6: the dedupe rule holds across processes because the database enforces it;
    NULL targets (unresolved requests) and terminal rows are exempt."""
    from sqlalchemy.exc import IntegrityError

    from hermes.domain.models import AlbumTarget, Origin
    from hermes.domain.state import AcquisitionState as S

    app = client.app
    with app.state.session_factory() as session:
        target = AlbumTarget(release_group_mbid="y" * 36, artist_name="A", title="B")
        session.add(target)
        session.add(Acquisition(album_target=target, state=S.RESOLVED, origin=Origin.MANUAL))
        session.commit()
        session.add(Acquisition(album_target=target, state=S.DISCOVERED, origin=Origin.AUTO))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(Acquisition(album_target=target, state=S.CANCELLED, origin=Origin.AUTO))
        session.add(Acquisition(album_target=None, state=S.FAILED, origin=Origin.MANUAL))
        session.add(Acquisition(album_target=None, state=S.FAILED, origin=Origin.MANUAL))
        session.commit()
