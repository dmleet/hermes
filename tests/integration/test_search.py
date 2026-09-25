"""Search stage through the API with the captured PandaCD/Prowlarr fixture."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

BEETS = "http://beets.test:8338"
PROWLARR = "http://prowlarr.test"
SLIP_RG = "5990b23f-6412-3eaf-989c-a4831f4f95f7"


def _slip_search() -> dict:
    """A MusicBrainz search response for The Slip built from the real release-group data."""
    return {
        "count": 1,
        "release-groups": [
            {
                "id": SLIP_RG,
                "score": 100,
                "title": "The Slip",
                "primary-type": "Album",
                "first-release-date": "2008-05-05",
                "artist-credit": [
                    {
                        "name": "Nine Inch Nails",
                        "artist": {
                            "id": "b7ffd2af-418f-4be2-bdd1-22f8b48613da",
                            "name": "Nine Inch Nails",
                        },
                    }
                ],
                "releases": [
                    {
                        "id": "d3252f73-5a42-4ab7-b20a-4d65debd8b10",
                        "title": "The Slip",
                        "status": "Official",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def client(settings: Settings, policy: Policy) -> Iterator[TestClient]:
    from alembic import command

    from hermes.cli import alembic_config

    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
    app = create_app(settings=settings, policy=policy, clients=clients)
    with TestClient(app) as c:
        yield c


def _mock_upstreams(router: respx.Router) -> respx.Route:
    router.get(f"{MB}/release-group/").respond(json=_slip_search())
    router.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    return router.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))


def test_missing_album_is_searched_and_ranked(respx_mock: respx.Router, client: TestClient) -> None:
    search = _mock_upstreams(respx_mock)
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "AWAITING_APPROVAL", body["events"]
    params = search.calls.last.request.url.params
    assert params["query"] == "Nine Inch Nails Slip" and params["type"] == "music"
    accepted = [c for c in body["candidates"] if c["rank"]]
    rejected = [c for c in body["candidates"] if not c["rank"]]
    assert (
        len(accepted) == 1
        and accepted[0]["title"] == "Nine Inch Nails - The Slip [2008] [FLAC Lossless]"
    )
    assert accepted[0]["freeleech"] is True and accepted[0]["seeders"] == 2
    assert {c["rejected_reason"] for c in rejected} == {
        "MP3 is not FLAC",
        "Ogg Vorbis is not FLAC",
        "Opus is not FLAC",
    }
    messages = [e["message"] for e in body["events"]]
    assert any(m.startswith("searching Prowlarr") for m in messages)
    assert "dry_run is on: nothing will be grabbed" in messages
    assert messages[-1] == "waiting for approval (timid mode)"
    ready = next(e for e in body["events"] if e["message"].startswith("1 of 6 results acceptable"))
    assert ready["data"]["top"][0]["rank"] == 1


def test_query_drops_punctuation_and_noise_words(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """MusicBrainz titles carry U+2019 and articles; a Gazelle index wants the pieces only."""
    mb = _slip_search()
    mb["release-groups"][0]["title"] = "The Slip’s"
    respx_mock.get(f"{MB}/release-group/").respond(json=mb)
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    search = respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip’s"}
    ).json()
    assert body["state"] == "NO_MATCH", body["events"]
    queries = [c.request.url.params["query"] for c in search.calls]
    assert queries == ["Nine Inch Nails Slip s", "Nine Inch Nails"]


def test_empty_search_falls_back_to_the_artist_alone(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """A title the uploader spelled differently is found through the artist's catalogue."""
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    search = respx_mock.get(f"{PROWLARR}/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=load("prowlarr/search_pandacd_nin")),
        ]
    )
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "AWAITING_APPROVAL", body["events"]
    first, second = (c.request.url.params for c in search.calls)
    assert first["query"] == "Nine Inch Nails Slip" and first["limit"] == "100"
    assert second["query"] == "Nine Inch Nails" and second["limit"] == "500"
    messages = [e["message"] for e in body["events"]]
    assert "nothing for 'Nine Inch Nails Slip'; searching the artist alone" in messages
    assert [c["title"] for c in body["candidates"] if c["rank"] == 1] == [
        "Nine Inch Nails - The Slip [2008] [FLAC Lossless]"
    ]


def test_empty_search_tries_the_title_head_before_the_artist(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """A subtitle the listing drops is left out of the query before the artist's whole
    catalogue is searched, which on a large one is a page of the newest uploads."""
    mb = _slip_search()
    mb["release-groups"][0]["title"] = "The Slip: Halo 27"
    respx_mock.get(f"{MB}/release-group/").respond(json=mb)
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    search = respx_mock.get(f"{PROWLARR}/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=load("prowlarr/search_pandacd_nin")),
        ]
    )
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip: Halo 27"}
    ).json()
    assert body["state"] == "AWAITING_APPROVAL", body["events"]
    queries = [c.request.url.params["query"] for c in search.calls]
    assert queries == ["Nine Inch Nails Slip Halo 27", "Nine Inch Nails Slip"]
    messages = [e["message"] for e in body["events"]]
    assert (
        "nothing for 'Nine Inch Nails Slip Halo 27'; searching the title before its subtitle"
        in messages
    )
    assert not any("artist alone" in m for m in messages)
    assert [c["title"] for c in body["candidates"] if c["rank"] == 1] == [
        "Nine Inch Nails - The Slip [2008] [FLAC Lossless]"
    ]


def test_empty_head_search_still_falls_back_to_the_artist(
    respx_mock: respx.Router, client: TestClient
) -> None:
    mb = _slip_search()
    mb["release-groups"][0]["title"] = "The Slip: Halo 27"
    respx_mock.get(f"{MB}/release-group/").respond(json=mb)
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    search = respx_mock.get(f"{PROWLARR}/api/v1/search").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[]),
            httpx.Response(200, json=load("prowlarr/search_pandacd_nin")),
        ]
    )
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip: Halo 27"}
    ).json()
    assert body["state"] == "AWAITING_APPROVAL", body["events"]
    queries = [c.request.url.params["query"] for c in search.calls]
    assert queries == ["Nine Inch Nails Slip Halo 27", "Nine Inch Nails Slip", "Nine Inch Nails"]
    messages = [e["message"] for e in body["events"]]
    assert "nothing for 'Nine Inch Nails Slip'; searching the artist alone" in messages


def test_empty_search_with_a_disabled_indexer_fails_retryably(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """Prowlarr skips an indexer it has disabled; that empty answer is not a NO_MATCH."""
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    _mock_indexers(respx_mock)
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(
        json=[
            {"indexerId": 2, "disabledTill": "2099-01-01T10:30:00Z"},
            {"indexerId": 3, "disabledTill": "2099-01-01T10:30:00Z"},  # disabled by hand
            {"indexerId": 1, "disabledTill": "2000-01-01T00:00:00Z"},  # long recovered
        ]
    )
    search = respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "FAILED", body["events"]
    assert body["error"] == (
        "no results for 'Nine Inch Nails Slip', and Prowlarr has disabled "
        "Tracker B until 2099-01-01 10:30 UTC"
    )
    assert len(search.calls) == 1  # no artist fallback while an indexer is out


def test_no_match_when_policy_rejects_everything(
    respx_mock: respx.Router, client: TestClient
) -> None:
    _mock_upstreams(respx_mock)
    client.app.state.policy.quality.require_format = "ALAC"  # nothing on PandaCD is ALAC
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "NO_MATCH"
    assert all(c["rank"] is None for c in body["candidates"])
    client.app.state.policy.quality.require_format = "FLAC"

    # A re-search from NO_MATCH is allowed and replaces the candidate rows.
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "AWAITING_APPROVAL" and len(body["candidates"]) == 6


def test_prowlarr_outage_fails_retryably(respx_mock: respx.Router, client: TestClient) -> None:
    _mock_upstreams(respx_mock)
    respx_mock.get(f"{PROWLARR}/api/v1/search").mock(side_effect=httpx.ConnectError("refused"))
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "FAILED" and "Prowlarr unavailable" in body["error"]
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "AWAITING_APPROVAL"


def test_an_unexpected_prowlarr_reply_fails_retryably_not_stranded(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """A 200 that is not JSON (a proxy's error page): FAILED and retryable, never left in
    SEARCHING, where nothing would move it on."""
    _mock_upstreams(respx_mock)
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(200, text="<html>bad gateway</html>")
    body = client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()
    assert body["state"] == "FAILED" and "search failed" in body["error"]
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "AWAITING_APPROVAL"


def test_search_endpoint_refuses_unresolved(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_nothing"))
    body = client.post("/api/requests", json={"artist": "Nobody", "title": "Nothing"}).json()
    assert body["state"] == "FAILED"
    resp = client.post(f"/api/acquisitions/{body['id']}/search")
    assert resp.status_code == 409
    assert client.post("/api/acquisitions/9999/search").status_code == 404


def _mock_indexers(router: respx.Router) -> respx.Route:
    return router.get(f"{PROWLARR}/api/v1/indexer").respond(
        json=[
            {"id": 1, "name": "PandaCD", "enable": True},
            {"id": 2, "name": "Tracker B", "enable": True},
            {"id": 3, "name": "Sleepy", "enable": False},
        ]
    )


def test_search_indexers_restricts_the_prowlarr_query(
    respx_mock: respx.Router, client: TestClient
) -> None:
    search = _mock_upstreams(respx_mock)
    _mock_indexers(respx_mock)
    policy = client.app.state.policy
    policy.search.indexers = ["pandacd", "Tracker B", "Sleepy", "Renamed"]
    try:
        body = client.post(
            "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
        ).json()
    finally:
        policy.search.indexers = []
    assert body["state"] == "AWAITING_APPROVAL", body["events"]
    params = search.calls.last.request.url.params
    assert params.get_list("indexerIds") == ["1", "2"]  # case-insensitive; disabled left out
    warnings = [e["message"] for e in body["events"] if e["level"] == "warning"]
    assert warnings == [
        "search.indexers names 'Sleepy', which is disabled in Prowlarr",
        "search.indexers names 'Renamed', which Prowlarr does not have",
    ]


def test_search_indexers_none_known_fails_retryably(
    respx_mock: respx.Router, client: TestClient
) -> None:
    search = _mock_upstreams(respx_mock)
    _mock_indexers(respx_mock)
    policy = client.app.state.policy
    policy.search.indexers = ["Renamed"]
    try:
        body = client.post(
            "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
        ).json()
    finally:
        policy.search.indexers = []
    assert body["state"] == "FAILED" and "none of search.indexers exists" in body["error"]
    assert not search.called  # nothing is searched rather than everything
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "AWAITING_APPROVAL"  # unrestricted again after the fix


def test_search_indexers_empty_means_all(respx_mock: respx.Router, client: TestClient) -> None:
    search = _mock_upstreams(respx_mock)
    indexers = _mock_indexers(respx_mock)
    client.post("/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"})
    assert "indexerIds" not in search.calls.last.request.url.params
    assert not indexers.called
