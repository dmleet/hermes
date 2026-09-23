"""Suggestions for the request form (services/suggest.py): artists for typed text, then an
artist's official albums, through the shared MusicBrainz client with one attempt and a
short timeout, one at a time, cached; an empty list for anything but a good answer."""

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
from hermes.services import suggest
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

SIGUR = "f6f2326f-6b25-4170-b89d-e235b25508e8"
VARIOUS = suggest.VARIOUS_ARTISTS_MBID


@pytest.fixture
def client(settings: Settings, policy: Policy) -> Iterator[TestClient]:
    from alembic import command

    from hermes.cli import alembic_config

    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0, busy_retries=2)
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    suggest.clear_caches()
    with TestClient(app) as c:
        yield c
    suggest.clear_caches()


def test_artists_are_bare_terms_ranked_by_musicbrainz(
    respx_mock: respx.Router, client: TestClient
) -> None:
    route = respx_mock.get(f"{MB}/artist/").respond(json=load("musicbrainz/artist_search_sigur"))
    resp = client.get("/api/suggest/artists", params={"q": "  Sigur  Ro "})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, max-age=300"
    names = [a["name"] for a in resp.json()]
    assert names[:2] == ["Sigur Rós", "Sigur"] and len(names) == 4
    assert resp.json()[0] == {
        "mbid": SIGUR,
        "name": "Sigur Rós",
        "disambiguation": "Icelandic post\u2010rock band",
        "country": "IS",
        "type": "Group",
    }
    params = route.calls.last.request.url.params
    assert params["query"] == "sigur ro" and params["limit"] == "9"  # no wildcard, no fuzzy
    assert route.call_count == 1
    # Retyping the same text (spacing and case aside) is a cache hit, not a request.
    assert client.get("/api/suggest/artists", params={"q": "sigur ro"}).json() == resp.json()
    assert route.call_count == 1


def test_short_text_and_operators(respx_mock: respx.Router, client: TestClient) -> None:
    route = respx_mock.get(f"{MB}/artist/").respond(json={"artists": []})
    assert client.get("/api/suggest/artists", params={"q": "U2"}).json() == []
    assert not route.called  # under the n-gram minimum: nothing to ask
    client.get("/api/suggest/artists", params={"q": 'a* OR *:* "x" (y)'})
    # Operators stripped, and casefolded so a typed OR is a word, not an operator.
    assert route.calls.last.request.url.params["query"] == "a or x y"


def test_various_artists_is_never_suggested(respx_mock: respx.Router, client: TestClient) -> None:
    body = load("musicbrainz/artist_search_sigur")
    body["artists"].insert(0, {"id": VARIOUS, "name": "Various Artists", "score": 100})
    respx_mock.get(f"{MB}/artist/").respond(json=body)
    names = [a["name"] for a in client.get("/api/suggest/artists", params={"q": "var"}).json()]
    assert "Various Artists" not in names and names[0] == "Sigur Rós"
    assert client.get("/api/suggest/albums", params={"artist": VARIOUS}).json() == []


def test_musicbrainz_trouble_is_an_empty_list_with_one_attempt(
    respx_mock: respx.Router, client: TestClient
) -> None:
    route = respx_mock.get(f"{MB}/artist/").respond(503, json={"error": "busy"})
    assert client.get("/api/suggest/artists", params={"q": "sigur"}).json() == []
    assert route.call_count == 1  # the pipeline's retries do not apply to a suggestion
    respx_mock.get(f"{MB}/artist/").mock(side_effect=httpx.ReadTimeout("slow"))
    assert client.get("/api/suggest/artists", params={"q": "godspeed"}).json() == []
    assert respx_mock.calls.last.request.extensions["timeout"]["read"] == suggest.TIMEOUT


async def test_one_suggestion_at_a_time(respx_mock: respx.Router, client: TestClient) -> None:
    """A second request while one is in flight is answered empty rather than queued
    behind it (and the pipeline) on the limiter."""
    route = respx_mock.get(f"{MB}/artist/").respond(json=load("musicbrainz/artist_search_sigur"))
    ctx = client.app.state.context
    async with suggest._slot:
        assert await suggest.artists(ctx, "sigur ro") == []
        assert await suggest.albums(ctx, SIGUR) == []
    assert not route.called
    assert [a["name"] for a in await suggest.artists(ctx, "sigur ro")][:1] == ["Sigur Rós"]


def test_albums_are_official_typed_marked_and_newest_first(
    respx_mock: respx.Router, client: TestClient
) -> None:
    route = respx_mock.get(f"{MB}/release-group/").respond(
        json=load("musicbrainz/rg_official_sigur")
    )
    resp = client.get("/api/suggest/albums", params={"artist": SIGUR})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, max-age=3600"
    rows = resp.json()
    assert [(r["title"], r["year"], r["type"]) for r in rows][:4] == [
        ("Takk…", 2025, "EP"),
        ("ÁTTA", 2023, "Album"),
        ("22° Lunar Halo", 2019, "Album"),
        ("Kveikur", 2019, "EP / Live"),
    ]
    by_title = {r["title"]: r for r in rows}
    assert by_title["Kveikur"]["rejected"] is None  # the 2013 album: allowed
    assert "Live" in [r for r in rows if r["type"] == "EP / Live"][0]["rejected"]
    assert "Soundtrack" in by_title["Hlemmur"]["rejected"]
    params = route.calls.last.request.url.params
    assert params["query"] == f"arid:{SIGUR} AND status:official AND primarytype:(album OR ep)"
    assert params["limit"] == "100" and params["offset"] == "0"
    # Cached per artist: the second form fill costs nothing.
    client.get("/api/suggest/albums", params={"artist": SIGUR})
    assert route.call_count == 1


def test_albums_page_to_the_cap(respx_mock: respx.Router, client: TestClient) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        groups = [
            {"id": f"rg-{offset + i}", "title": f"Album {offset + i}", "primary-type": "Album"}
            for i in range(100)
        ]
        return httpx.Response(200, json={"count": 1000, "offset": offset, "release-groups": groups})

    route = respx_mock.get(f"{MB}/release-group/").mock(side_effect=page)
    rows = client.get("/api/suggest/albums", params={"artist": SIGUR}).json()
    assert len(rows) == 300 and route.call_count == 3


def test_albums_bad_mbid_and_types_from_policy(
    respx_mock: respx.Router, client: TestClient
) -> None:
    route = respx_mock.get(f"{MB}/release-group/").respond(json={"count": 0, "release-groups": []})
    assert client.get("/api/suggest/albums", params={"artist": "not-an-mbid"}).json() == []
    assert not route.called
    ctx = client.app.state.context
    ctx.policy.resolution.allow_ep = False
    ctx.policy.resolution.allow_single = True
    assert client.get("/api/suggest/albums", params={"artist": SIGUR}).json() == []
    assert "primarytype:(album OR single)" in route.calls.last.request.url.params["query"]


def test_request_page_carries_the_widget_and_the_form_still_posts_names(
    respx_mock: respx.Router, client: TestClient
) -> None:
    page = client.get("/", headers={"accept": "text/html"}).text
    assert '<script src="/static/autoComplete-10.2.10.min.js"></script>' in page
    assert 'id="picked-mbid" name="mbid" value=""' in page
    assert page.count('autocomplete="off"') == 2
    js = client.get("/static/autoComplete-10.2.10.min.js")
    assert js.status_code == 200 and "autoComplete" in js.text
    assert "Apache License" in client.get("/static/autoComplete-LICENSE.txt").text
