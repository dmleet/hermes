"""Manual request pipeline through the API: MusicBrainz and beets mocked with fixtures."""

from __future__ import annotations

from collections.abc import Iterator

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
DUMMY_RG = "48140466-cff6-3222-bd55-63c27e43190d"
RAM_RG = "aa997ea0-2936-40bd-884d-3af8a0e064dc"
OWNED_DUMMY = {
    "id": 7,
    "mb_albumid": "76df3287-6cda-33eb-8e9a-044b5e15ffdd",
    "mb_releasegroupid": DUMMY_RG,
    "albumartist": "Portishead",
    "album": "Dummy",
    "year": 1994,
    "path": "/music/Portishead/Dummy",
    "quality": {
        "items": 11,
        "formats": ["FLAC"],
        "min_bitdepth": 16,
        "min_samplerate": 44100,
        "lossy_items": 0,
    },
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


def _mock_beets_empty(router: respx.Router) -> None:
    """Empty library and an empty tracker: missing albums end in NO_MATCH."""
    router.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    router.get(f"{PROWLARR}/api/v1/search").respond(json=[])


def test_owned_album_ends_already_owned(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(f"{BEETS}/library/release-group/{DUMMY_RG}").respond(
        json={"albums": [OWNED_DUMMY]}
    )
    resp = client.post("/api/requests", json={"artist": "Portishead", "title": "Dummy"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "ALREADY_OWNED"
    assert body["target"]["release_group_mbid"] == DUMMY_RG
    assert body["target"]["library_status"] == "owned"
    messages = [e["message"] for e in body["events"]]
    assert messages[0].startswith("resolved to Portishead - Dummy")
    assert "already in the library" in messages[1] and "release_group" in messages[1]


def test_missing_album_stays_resolved_and_dedupes(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_ram"))
    _mock_beets_empty(respx_mock)
    first = client.post(
        "/api/requests", json={"artist": "Daft Punk", "title": "Random Access Memories"}
    ).json()
    assert first["state"] == "NO_MATCH" and first["target"]["library_status"] == "missing"
    assert first["target"]["release_group_mbid"] == RAM_RG
    assert "not in the library; ready for search" in [e["message"] for e in first["events"]]

    second = client.post(
        "/api/requests", json={"artist": "Daft Punk", "title": "Random Access Memories"}
    ).json()
    assert second["id"] == first["id"]
    assert second["events"][-1]["message"] == "manual request attached to existing acquisition"

    fetched = client.get(f"/api/acquisitions/{first['id']}").json()
    # No dry-run note after NO_MATCH: there was nothing to grab.
    assert fetched["state"] == "NO_MATCH" and len(fetched["events"]) == 5


def test_ambiguous_request_needs_review_with_candidates(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    _mock_beets_empty(respx_mock)
    body = client.post("/api/requests", json={"artist": "Portishead", "title": "Third"}).json()
    assert body["state"] == "NEEDS_REVIEW" and body["target"] is None
    candidates = body["events"][0]["data"]["candidates"]
    assert candidates[0]["title"] == "Dummy" and candidates[0]["policy_ok"] is True


def test_unknown_album_fails(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_nothing"))
    body = client.post("/api/requests", json={"artist": "Nobody", "title": "Nothing"}).json()
    assert body["state"] == "FAILED" and "no release groups" in body["error"]


def test_mbid_request_pins_release(respx_mock: respx.Router, client: TestClient) -> None:
    release = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"
    respx_mock.get(f"{MB}/release-group/{release}").respond(
        404, json=load("musicbrainz/rg_missing")
    )
    respx_mock.get(f"{MB}/release/{release}").respond(json=load("musicbrainz/release_dummy"))
    respx_mock.get(f"{MB}/release-group/{DUMMY_RG}").respond(json=load("musicbrainz/rg_dummy"))
    _mock_beets_empty(respx_mock)
    body = client.post("/api/requests", json={"mbid": release}).json()
    assert body["state"] == "NO_MATCH"
    assert body["target"]["preferred_release_mbid"] == release
    assert body["target"]["release_group_mbid"] == DUMMY_RG


def test_request_validation() -> None:
    from pydantic import ValidationError

    from hermes.api.schemas import ManualRequest

    with pytest.raises(ValidationError):
        ManualRequest(artist="only artist")
    assert ManualRequest(mbid="x").mbid == "x"


def test_musicbrainz_outage_fails_cleanly(respx_mock: respx.Router, client: TestClient) -> None:
    import httpx

    respx_mock.get(f"{MB}/release-group/").mock(side_effect=httpx.ConnectError("refused"))
    resp = client.post("/api/requests", json={"artist": "Portishead", "title": "Dummy"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "FAILED" and "MusicBrainz unavailable" in body["error"]
    assert body["events"][0]["data"]["retryable"] is True


def test_exact_miss_falls_back_to_loose_search(
    respx_mock: respx.Router, client: TestClient
) -> None:
    import httpx

    route = respx_mock.get(f"{MB}/release-group/")
    route.side_effect = [
        httpx.Response(200, json=load("musicbrainz/search_nothing")),
        httpx.Response(200, json=load("musicbrainz/search_loose_gybe")),
    ]
    _mock_beets_empty(respx_mock)
    body = client.post(
        "/api/requests",
        json={
            "artist": "Godspeed You! Black Emperor",
            "title": "Lift Your Skinny Fists Like Antennas to Heaven",
        },
    ).json()
    assert body["state"] == "NO_MATCH", body
    assert body["target"]["title"] == "Lift Yr. Skinny Fists Like Antennas to Heaven!"
    assert route.call_count == 2
    assert "releasegroup:(" in str(route.calls[1].request.url.params["query"])
