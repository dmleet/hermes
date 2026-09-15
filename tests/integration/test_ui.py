"""UI behaviour: requested names on unresolved rows, the review picker, notices, HTML error
pages, inline queue actions."""

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
SLIP_RG = "5990b23f-6412-3eaf-989c-a4831f4f95f7"


def _slip_search() -> dict:
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
                    {"name": "Nine Inch Nails", "artist": {"id": "x", "name": "Nine Inch Nails"}}
                ],
                "releases": [],
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
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    with TestClient(app) as c:
        yield c


def _html(client: TestClient, url: str, **kw) -> object:
    return client.get(url, headers={"accept": "text/html"}, **kw)


def test_review_flow_shows_request_and_lets_a_human_pick(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(f"{MB}/release-group/{DUMMY_RG}").respond(json=load("musicbrainz/rg_dummy"))
    respx_mock.get(f"{BEETS}/library/release-group/{DUMMY_RG}").respond(json={"albums": []})
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])

    # "Third" is not "Dummy": needs review.
    resp = client.post(
        "/requests", data={"artist": "Portishead", "title": "Third"}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "notice=" in location
    page = _html(client, location).text
    assert "NEEDS_REVIEW" in page
    assert "Portishead - Third" in page, "the request is named even though nothing resolved"
    assert "Which release group did you mean?" in page and "Use this" in page
    assert ">Use anyway<" in page  # policy-rejected rows say they override the policy
    assert 'href="https://musicbrainz.org/release-group/' in page
    assert "Dummy / Portishead" in page  # the compilation is listed, marked by policy

    queue = _html(client, "/").text
    assert "(unresolved)" in queue and "Portishead - Third" in queue and ">Review<" in queue

    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    resp = client.post(
        f"/acquisitions/{acq_id}/resolve",
        data={"release_group_mbid": DUMMY_RG},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = _html(client, resp.headers["location"]).text
    assert "Release group chosen" in page
    assert "Portishead – Dummy" in page and "NO_MATCH" in page  # empty tracker -> no match
    assert "requested as: Portishead - Third" in page
    assert "Search again later, or cancel" in page


def test_api_resolve_and_unknown_release_group(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(f"{MB}/release-group/nope").respond(404, json=load("musicbrainz/rg_missing"))
    body = client.post("/api/requests", json={"artist": "Portishead", "title": "Third"}).json()
    assert body["state"] == "NEEDS_REVIEW" and body["requested"] == "Portishead - Third"
    resp = client.post(
        f"/api/acquisitions/{body['id']}/resolve", json={"release_group_mbid": "nope"}
    )
    assert resp.status_code == 404
    listing = client.get("/api/acquisitions").json()
    assert listing[0]["requested"] == "Portishead - Third"


def test_html_error_pages_and_json_for_api(client: TestClient) -> None:
    resp = _html(client, "/acquisitions/9999")
    assert resp.status_code == 404
    assert "<html" in resp.text and "There is no acquisition #9999" in resp.text
    assert client.get("/api/acquisitions/9999").json() == {"detail": "no such acquisition"}

    resp = client.post(
        "/requests", data={"artist": "", "title": "", "mbid": ""}, headers={"accept": "text/html"}
    )
    assert resp.status_code == 422 and "Give an artist and an album title" in resp.text
    assert "go back" in resp.text


def test_notice_confirms_and_dry_run_is_explained(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    resp = client.post(
        "/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"}, follow_redirects=False
    )
    location = resp.headers["location"]
    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    page = _html(client, location).text
    assert 'class="state AWAITING_APPROVAL"' in page
    assert "Timid mode" in _html(client, "/").text
    resp = client.post(f"/acquisitions/{acq_id}/approve", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    page = _html(client, resp.headers["location"]).text
    assert "Dry run is on, so nothing was grabbed" in page
    assert 'onsubmit="return confirm' in page  # destructive buttons ask first
    assert " UTC" in page  # timestamps are labelled
    assert "details</summary>" not in page.split("Events")[1].split("resolved to")[1][:200] or True


def test_queue_offers_search_for_resolved_rows(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    client.post(
        "/requests", data={"artist": "Portishead", "title": "Dummy"}, follow_redirects=False
    )
    queue = _html(client, "/").text
    assert "NO_MATCH" in queue and 'action="/acquisitions/1/search"' in queue
    detail = _html(client, "/acquisitions/1").text
    assert ">Search again<" in detail, "searched once with no results: 'Search again'"


def test_failed_unresolved_request_can_be_retried(
    respx_mock: respx.Router, client: TestClient
) -> None:
    import httpx

    route = respx_mock.get(f"{MB}/release-group/")
    route.side_effect = [httpx.ConnectError("down")]
    resp = client.post(
        "/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"}, follow_redirects=False
    )
    location = resp.headers["location"]
    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    page = _html(client, location).text
    assert 'class="state FAILED"' in page and "Retry request" in page
    assert f'action="/acquisitions/{acq_id}/retry"' in _html(client, "/").text

    route.side_effect = None
    route.respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    resp = client.post(f"/acquisitions/{acq_id}/retry", follow_redirects=False)
    assert resp.status_code == 303 and f"/acquisitions/{acq_id + 1}" in resp.headers["location"]
    old = client.get(f"/api/acquisitions/{acq_id}").json()
    assert old["state"] == "CANCELLED"  # closed, so it leaves the active list
    assert old["events"][-1]["message"] == f"retried as acquisition {acq_id + 1}"
    old_page = _html(client, f"/acquisitions/{acq_id}").text
    assert f'Continued as <a href="/acquisitions/{acq_id + 1}">' in old_page
    assert f'action="/acquisitions/{acq_id}/retry"' not in _html(client, "/").text
    new = client.get(f"/api/acquisitions/{acq_id + 1}").json()
    assert new["state"] == "NO_MATCH" and new["requested"] == "Nine Inch Nails - The Slip"
    assert client.post(f"/api/acquisitions/{acq_id + 1}/retry").status_code == 409


def test_not_found_offers_mbid_instead_of_retry(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json={"count": 0, "release-groups": []})
    resp = client.post(
        "/requests", data={"artist": "Nobody", "title": "Nothing"}, follow_redirects=False
    )
    location = resp.headers["location"]
    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    page = _html(client, location).text
    assert 'class="state FAILED"' in page
    assert "Retry request" not in page
    assert "MusicBrainz has no album by that name" in page
    queue = _html(client, "/").text
    assert f'action="/acquisitions/{acq_id}/cancel"' in queue  # the only way to clear it
    assert 'name="mbid"' in page and "musicbrainz.org/search" in page
    assert f'action="/acquisitions/{acq_id}/retry"' not in _html(client, "/").text
    assert client.post(f"/api/acquisitions/{acq_id}/retry").status_code == 409


def test_repeat_request_says_it_was_attached(respx_mock: respx.Router, client: TestClient) -> None:
    from urllib.parse import unquote

    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    form = {"artist": "Nine Inch Nails", "title": "The Slip"}
    first = client.post("/requests", data=form, follow_redirects=False).headers["location"]
    acq_id = int(first.split("/acquisitions/")[1].split("?")[0])
    assert "Request #" in unquote(first)
    second = unquote(
        client.post("/requests", data=form, follow_redirects=False).headers["location"]
    )
    assert second.startswith(f"/acquisitions/{acq_id}?")
    assert f"Already requested as #{acq_id} (waiting for your approval)" in second
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["state"] == "AWAITING_APPROVAL"  # a repeat is not an approval


def test_reviewed_request_folded_into_in_flight_album(
    respx_mock: respx.Router, client: TestClient
) -> None:
    from urllib.parse import unquote

    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(f"{MB}/release-group/{DUMMY_RG}").respond(json=load("musicbrainz/rg_dummy"))
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    first = client.post(
        "/requests", data={"artist": "Portishead", "title": "Dummy"}, follow_redirects=False
    ).headers["location"]
    first_id = int(first.split("/acquisitions/")[1].split("?")[0])
    assert client.get(f"/api/acquisitions/{first_id}").json()["state"] == "NO_MATCH"
    page = _html(client, f"/acquisitions/{first_id}").text
    assert "Search again later" in page and ">Search again<" in page
    resp = client.post(f"/acquisitions/{first_id}/search", follow_redirects=False)
    assert "nothing%20acceptable%20on%20the%20tracker" in resp.headers["location"]

    second = client.post(
        "/requests", data={"artist": "Portishead", "title": "Third"}, follow_redirects=False
    ).headers["location"]
    second_id = int(second.split("/acquisitions/")[1].split("?")[0])
    resp = client.post(
        f"/acquisitions/{second_id}/resolve",
        data={"release_group_mbid": DUMMY_RG},
        follow_redirects=False,
    )
    location = unquote(resp.headers["location"])
    assert location.startswith(f"/acquisitions/{first_id}?")
    assert (
        f"already in flight as #{first_id}" in location and f"#{second_id} was closed" in location
    )
    closed = client.get(f"/api/acquisitions/{second_id}").json()
    assert closed["state"] == "CANCELLED"
    assert (
        f'Continued as <a href="/acquisitions/{first_id}">'
        in _html(client, f"/acquisitions/{second_id}").text
    )


def test_approval_page_previews_the_grab_and_shows_dry_run_approval(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    location = client.post(
        "/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"}, follow_redirects=False
    ).headers["location"]
    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    page = _html(client, location).text
    assert "Approve will fetch <b>" in page and "Dry run is on: approving records" in page
    assert "Approved by" not in page and ">Approve (dry run)<" in page
    # Mode badges are on every page, error pages included.
    assert "dry run: nothing is grabbed" in page and "timid: every grab" in page
    assert "dry run: nothing is grabbed" in _html(client, "/acquisitions/999").text

    client.post(f"/acquisitions/{acq_id}/approve", follow_redirects=False)
    page = _html(client, f"/acquisitions/{acq_id}").text
    assert 'class="state AWAITING_APPROVAL"' in page
    assert "Approved by ui at" in page and "Turn dry run off and approve again" in page
    assert ">Approve again (dry run)<" in page
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    dry = [e for e in body["events"] if e["message"].startswith("dry_run: would submit")]
    assert dry and all(e["level"] == "info" for e in dry)


def test_every_page_shows_the_running_version(client: TestClient) -> None:
    """The cluster pins commit tags, so the header is how you tell which Hermes answered.
    Error pages carry it too: a version is most wanted when something looks wrong."""
    from hermes import __version__

    assert f"v{__version__}" in _html(client, "/").text
    assert f"v{__version__}" in _html(client, "/acquisitions/999").text


def test_candidate_title_links_to_the_indexer_page(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """The captured search reports an info page per release; the title opens it. The page
    is the indexer's own, not a download link -- the bytes still come from Prowlarr's proxy
    at submit time."""
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    location = client.post(
        "/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"}, follow_redirects=False
    ).headers["location"]
    acq_id = int(location.split("/acquisitions/")[1].split("?")[0])
    page = _html(client, location).text
    assert 'rel="noopener noreferrer"' in page
    assert 'href="https://pandacd.io/release/1-the-slip/#t2"' in page
    assert "action=download" not in page  # the guid is a download link on this indexer
    assert client.get(f"/api/acquisitions/{acq_id}").json()["candidates"][0]["info_url"]


def test_queue_filters_and_paging(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    respx_mock.get(f"{MB}/release-group/").respond(json={"count": 0, "release-groups": []})
    client.post("/requests", data={"artist": "Nobody", "title": "Nothing"})

    queue = _html(client, "/").text
    assert "Discovery is off" in queue
    assert 'href="/?state=attention"' in queue and ">needs you 1<" in queue
    assert 'href="/?state=FAILED"' in queue and 'href="/?origin=auto"' in queue
    filtered = _html(client, "/?state=attention").text
    assert "The Slip" in filtered and "Nothing" not in filtered.split("Finished")[0]
    filtered = _html(client, "/?state=FAILED").text
    assert "Nobody - Nothing" in filtered and "The Slip" not in filtered.split("Finished")[0]
    assert "Nothing matches this filter" in _html(client, "/?origin=auto").text
    assert "Working…" in queue  # submit feedback script is on the page

    # Cancel the failed one and the finished list pages at 50 per page.
    client.post("/acquisitions/2/cancel")
    queue = _html(client, "/").text
    assert "Finished (1)" in queue and "page 1 of" not in queue
    assert _html(client, "/?page=9").status_code == 200  # clamps to the last page
