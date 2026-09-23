"""UI behaviour: requested names on unresolved rows, the review picker, notices, HTML error
pages, inline queue actions, the request/queue/history split and the triage walk
(docs/ui-plan.md)."""

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
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])

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

    queue = _html(client, "/queue").text
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


def test_queue_offers_search_for_resolved_rows(
    respx_mock: respx.Router, client: TestClient
) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    client.post(
        "/requests", data={"artist": "Portishead", "title": "Dummy"}, follow_redirects=False
    )
    queue = _html(client, "/queue").text
    assert "NO_MATCH" in queue and 'action="/acquisitions/1/search?back=queue"' in queue
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
    assert f'action="/acquisitions/{acq_id}/retry"' in _html(client, "/queue").text

    route.side_effect = None
    route.respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=[])
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
    resp = client.post(f"/acquisitions/{acq_id}/retry", follow_redirects=False)
    assert resp.status_code == 303 and f"/acquisitions/{acq_id + 1}" in resp.headers["location"]
    old = client.get(f"/api/acquisitions/{acq_id}").json()
    assert old["state"] == "CANCELLED"  # closed, so it leaves the active list
    assert old["events"][-1]["message"] == f"retried as acquisition {acq_id + 1}"
    old_page = _html(client, f"/acquisitions/{acq_id}").text
    assert f'Continued as <a href="/acquisitions/{acq_id + 1}">' in old_page
    assert f'action="/acquisitions/{acq_id}/retry"' not in _html(client, "/queue").text
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
    queue = _html(client, "/queue").text
    assert f'action="/acquisitions/{acq_id}/cancel?back=queue"' in queue  # the only way out
    assert 'name="mbid"' in page and "musicbrainz.org/search" in page
    assert f'action="/acquisitions/{acq_id}/retry"' not in queue
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
    respx_mock.get(f"{PROWLARR}/api/v1/indexerstatus").respond(json=[])
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
    assert "Approve will fetch</h2>" in page and "Dry run is on: approving records" in page
    # The target card: what the click fetches and where it goes, as a list, not prose.
    assert "<dt>Quality</dt><dd>FLAC" in page and "<dt>Deluge</dt>" in page
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
    # The queue's best-candidate column links to the same page: that is the list a human
    # actually works from, and it is the title they want to look up before approving.
    assert 'href="https://pandacd.io/release/1-the-slip/#t2"' in _html(client, "/queue").text


def test_queue_filters_and_paging(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    respx_mock.get(f"{MB}/release-group/").respond(json={"count": 0, "release-groups": []})
    client.post("/requests", data={"artist": "Nobody", "title": "Nothing"})

    queue = _html(client, "/queue").text
    # Seven stable chips: three state groups by who has the ball, plus origin. FAILED is a
    # person's decision, so it counts as "needs you"; the exact states are a text line.
    assert 'href="/queue?state=attention"' in queue
    assert 'needs you <span class="n ">2</span>' in queue
    assert 'href="/queue?state=inflight"' in queue
    assert 'in flight <span class="n zero">0</span>' in queue  # a zero count is muted
    assert 'href="/queue?state=NO_MATCH"' in queue and "not found" in queue
    assert queue.count('<span class="group">') == 2 and "\u00b7</span>" not in queue
    assert 'href="/queue?origin=auto"' in queue and 'href="/queue?state=FAILED"' not in queue
    assert '<p class="muted">needs approval 1 · failed 1</p>' in queue
    filtered = _html(client, "/queue?state=attention").text
    assert "The Slip" in filtered and "Nobody - Nothing" in filtered
    assert "Nothing matches this filter" in _html(client, "/queue?state=inflight").text
    # An exact state still filters from the query string; it just has no chip.
    filtered = _html(client, "/queue?state=FAILED").text
    assert "Nobody - Nothing" in filtered and "The Slip" not in filtered
    assert "Nothing matches this filter" in _html(client, "/queue?origin=auto").text
    assert "Working…" in queue  # submit feedback script is on the page
    # Old bookmarks of the queue and its finished list, which used to live on /, are sent on.
    resp = client.get("/?state=FAILED", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/queue?state=FAILED"
    resp = client.get("/?page=2", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/history?page=2"

    # Cancel the failed one from the queue: back to the queue, and it moves to history.
    resp = client.post("/acquisitions/2/cancel?back=queue&state=FAILED", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/queue?state=FAILED")
    history = _html(client, "/history").text
    assert 'History <span class="muted">(1)</span>' in history and "page 1 of" not in history
    assert "Nobody - Nothing" in history and "The Slip" not in history
    assert _html(client, "/history?page=9").status_code == 200  # clamps to the last page
    assert "Nobody - Nothing" in _html(client, "/history?q=nobod").text
    assert "Nothing matches" in _html(client, "/history?q=slip").text
    assert "Nobody - Nothing" in _html(client, "/history?state=CANCELLED&origin=manual").text
    assert "Nothing matches" in _html(client, "/history?state=REJECTED").text


def test_request_page_is_the_landing_page(respx_mock: respx.Router, client: TestClient) -> None:
    """The quick-add case: two required fields and one button, the MBID form tucked away,
    and the last few requests so the one just added can be seen to have gone in."""
    page = _html(client, "/").text
    assert 'name="artist"' in page and 'name="title"' in page and 'name="mbid"' in page
    assert page.count("<form") == 2, "name form and MBID form are separate, so required works"
    assert (
        'required autofocus autocapitalize="words" autocomplete="off" enterkeyhint="next"' in page
    )
    assert "<summary>or paste a MusicBrainz ID</summary>" in page
    assert "Recent requests" not in page and "need you" not in page
    assert 'href="/manifest.webmanifest"' in page
    assert client.get("/manifest.webmanifest").json()["start_url"] == "/"
    assert client.get("/static/icon-192.png").headers["content-type"] == "image/png"

    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    page = _html(client, "/").text
    assert "Recent requests" in page and "Nine Inch Nails – The Slip" in page
    assert "<b>1</b> item needs you → Queue" in page
    # The header badge on every page with a session; the error page has none and still renders.
    assert '<span class="badge" title="need you">1</span>' in page
    assert '<span class="badge" title="need you">1</span>' in _html(client, "/history").text
    err = _html(client, "/acquisitions/999")
    assert err.status_code == 404 and 'class="badge"' not in err.text
    assert 'href="/queue">queue</a>' in err.text


def test_mobile_layout_rules(respx_mock: respx.Router, client: TestClient) -> None:
    """CSS does the collapsing, so a test can only check the classes and the rule: secondary
    columns carry `opt`, the stylesheet hides them below 640px, the primary action is
    repeated in the fixed bar, and a cancelled confirm() no longer sticks on Working."""
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    queue = _html(client, "/queue").text
    assert "@media (max-width: 640px)" in queue and ".opt { display:none !important; }" in queue
    assert '<th class="opt">Best candidate</th>' in queue and 'class="num"' not in queue
    assert '<span class="art" aria-hidden="true">N</span>' in queue
    assert "if (e.defaultPrevented) return;" in queue
    assert 'aria-current="page">Queue' in queue and 'aria-current="page">Request' not in queue
    detail = _html(client, "/acquisitions/1").text
    assert '<div class="actionbar">' in detail and detail.count(">Approve (dry run)<") == 2
    assert '<th class="opt">Indexer</th>' in detail and '<span class="art big"' in detail


def test_triage_walk_advances_only_when_the_item_leaves_the_list(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """Prev/next follow the queue's order inside the filter the page was opened with. An
    action moves on only when it took the item out of that list and not into FAILED: a
    dry-run approval stays, so the screen that explains dry run is seen; a reject advances;
    the last one goes back to the queue (docs/ui-plan.md 3.3)."""
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})  # #1
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    client.post("/requests", data={"artist": "Portishead", "title": "Third"})  # #2, review
    assert client.get("/api/acquisitions/1").json()["state"] == "AWAITING_APPROVAL"
    assert client.get("/api/acquisitions/2").json()["state"] == "NEEDS_REVIEW"

    # A deep link has no walk: no bar, and actions come back to the same page.
    plain = _html(client, "/acquisitions/1").text
    assert "next →" not in plain and 'action="/acquisitions/1/approve"' in plain

    queue = _html(client, "/queue?state=attention").text
    assert 'href="/acquisitions/1?walk=1&amp;state=attention"' in queue
    first = _html(client, "/acquisitions/1?walk=1&state=attention").text
    assert "1 of 2" in first and 'href="/acquisitions/2?walk=1&amp;state=attention">next →' in first
    assert "← previous" not in first
    assert 'action="/acquisitions/1/approve?walk=1&amp;state=attention"' in first
    second = _html(client, "/acquisitions/2?walk=1&state=attention").text
    assert "2 of 2" in second and "next →" not in second
    assert 'href="/acquisitions/1?walk=1&amp;state=attention">← previous' in second

    # Dry-run approve: still AWAITING_APPROVAL, still in "needs you", so stay put.
    resp = client.post("/acquisitions/1/approve?walk=1&state=attention", follow_redirects=False)
    assert resp.headers["location"].startswith("/acquisitions/1?walk=1&state=attention&notice=")
    assert "Dry%20run%20is%20on" in resp.headers["location"]

    # Reject: out of the list, so on to #2, with a way back to #1.
    resp = client.post("/acquisitions/1/reject?walk=1&state=attention", follow_redirects=False)
    location = resp.headers["location"]
    assert location.startswith("/acquisitions/2?walk=1&state=attention&notice=Rejected%20%231.")
    assert location.endswith("&prev=1")
    page = _html(client, location).text
    assert '<div class="notice">Rejected #1. <a href="/acquisitions/1">#1</a></div>' in page
    assert "1 of 1" in page

    # The last one: back to the (now empty) list.
    resp = client.post("/acquisitions/2/reject?walk=1&state=attention", follow_redirects=False)
    assert resp.headers["location"] == (
        "/queue?state=attention&notice=Rejected%20%232.%20Nothing%20else%20in%20this%20list."
    )
    assert "Nothing matches this filter" in _html(client, resp.headers["location"]).text
    # Terminal rows point at history, not the queue, and have no walk.
    done = _html(client, "/acquisitions/2?walk=1&state=attention").text
    assert 'href="/history">← history</a>' in done and "of 0" not in done


def test_walk_order_is_state_first_then_cancel_advances_under_a_filter(
    respx_mock: respx.Router, client: TestClient
) -> None:
    """The lower id has the lower-priority state, so id order and queue order disagree: the
    queue number, the walk's position and prev/next must all follow QUEUE_ORDER. A cancel
    from a walk under an origin filter advances like a reject does."""
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    respx_mock.get(f"{MB}/release-group/").respond(json=load("musicbrainz/search_dummy"))
    client.post("/requests", data={"artist": "Portishead", "title": "Third"})  # #1, review
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})  # #2
    assert client.get("/api/acquisitions/1").json()["state"] == "NEEDS_REVIEW"
    assert client.get("/api/acquisitions/2").json()["state"] == "AWAITING_APPROVAL"

    queue = _html(client, "/queue?origin=manual").text
    rows = queue.split("<tr>")[2:]
    assert "The Slip" in rows[0] and "Portishead - Third" in rows[1]
    second = _html(client, "/acquisitions/2?walk=1&origin=manual").text
    assert "1 of 2" in second and 'href="/acquisitions/1?walk=1&amp;origin=manual">next' in second
    first = _html(client, "/acquisitions/1?walk=1&origin=manual").text
    assert "2 of 2" in first
    assert 'href="/acquisitions/2?walk=1&amp;origin=manual">← previous' in first

    resp = client.post("/acquisitions/2/cancel?walk=1&origin=manual", follow_redirects=False)
    assert resp.headers["location"].startswith("/acquisitions/1?walk=1&origin=manual&notice=")
    assert resp.headers["location"].endswith("&prev=2")
    assert client.get("/api/acquisitions/2").json()["state"] == "CANCELLED"
    # A bogus filter value is dropped rather than carried into an empty walk.
    page = _html(client, "/acquisitions/1?walk=1&state=bogus&origin=nope").text
    assert "1 of 1" in page and "state=bogus" not in page


def test_history_pages_past_the_first(client: TestClient) -> None:
    from hermes.domain.models import Acquisition

    with client.app.state.session_factory() as session:
        for _ in range(51):
            session.add(Acquisition(state="CANCELLED", origin="manual"))
        session.commit()
    page1 = _html(client, "/history").text
    assert "page 1 of 2" in page1 and page1.count("<tr>") == 51  # header + 50
    page2 = _html(client, "/history?page=2").text
    assert "page 2 of 2" in page2 and page2.count("<tr>") == 2
    assert 'href="/history?page=1">newer' in page2 or 'href="/history">newer' in page2
