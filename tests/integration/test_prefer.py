"""Phase C of docs/ui-plan.md: a human puts a candidate in front of the ranker's choice.
Prefer only reorders; the grab still goes through Approve and its preview."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import Acquisition, AlbumTarget, Candidate, GrabAttempt
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import approval
from hermes.services.submit import next_candidate
from tests.fixtures import load
from tests.integration.test_ui import BEETS, PROWLARR, _html, _slip_search

pytestmark = pytest.mark.respx(assert_all_called=False)


def _candidate(session: Session, acq: Acquisition, n: int, **kw: object) -> Candidate:
    fields: dict[str, object] = {
        "prowlarr_guid": f"guid-{n}",
        "indexer_id": 1,
        "indexer_name": "PandaCD",
        "title": f"Artist - Album [FLAC {n}]",
        "size_bytes": 300_000_000,
        "seeders": 3,
        "rank": n,
    }
    fields.update(kw)
    c = Candidate(acquisition=acq, **fields)  # type: ignore[arg-type]
    session.add(c)  # a backref no longer adds to the session (SQLAlchemy 2.0)
    return c


@pytest.fixture
def acq(session: Session) -> Acquisition:
    target = AlbumTarget(release_group_mbid="rg-1", artist_name="Artist", title="Album")
    a = Acquisition(album_target=target, state=S.AWAITING_APPROVAL, origin="manual")
    session.add(a)
    for n in (1, 2, 3):
        _candidate(session, a, n)
    cut = "acceptable, but beyond keep_candidates (3)"
    _candidate(session, a, 4, rank=None, rejected_reason=cut)
    _candidate(session, a, 5, rank=None, rejected_reason="MP3 is not FLAC")
    _candidate(session, a, 6, rank=None, rejected_reason="match 0.40 below 0.85")
    session.commit()
    return a


def _order(a: Acquisition) -> list[int]:
    return [
        int(c.prowlarr_guid.split("-")[1])
        for c in sorted((c for c in a.candidates if c.rank is not None), key=lambda c: c.rank or 0)
    ]


def test_prefer_reorders_and_next_candidate_follows(session: Session, acq: Acquisition) -> None:
    by_guid = {c.prowlarr_guid: c for c in acq.candidates}
    assert _order(acq) == [1, 2, 3]
    assert set(approval.preferable(acq)) == {
        by_guid["guid-2"].id,
        by_guid["guid-3"].id,
        by_guid["guid-4"].id,
    }
    assert approval.preferable(acq)[by_guid["guid-4"].id] == "Use anyway"

    approval.prefer(session, acq, by_guid["guid-3"], by="test")
    assert _order(acq) == [3, 1, 2]
    top = next_candidate(acq)
    assert top is not None and top.prowlarr_guid == "guid-3"
    assert approval.preferred_id(acq) == by_guid["guid-3"].id
    last = acq.events[-1]
    assert (
        last.message == "preferred Artist - Album [FLAC 3] over Artist - Album [FLAC 1] (by test)"
    )
    assert last.data == {
        "candidate_id": by_guid["guid-3"].id,
        "previous_candidate_id": by_guid["guid-1"].id,
    }
    # The top row is not offered again; preferring it is a no-op with no event.
    assert by_guid["guid-3"].id not in approval.preferable(acq)
    n_events = len(acq.events)
    approval.prefer(session, acq, by_guid["guid-3"], by="test")
    assert len(acq.events) == n_events and _order(acq) == [3, 1, 2]

    # A keep_candidates cut can be used anyway: it becomes ranked, in front.
    approval.prefer(session, acq, by_guid["guid-4"], by="test")
    assert _order(acq) == [4, 3, 1, 2] and by_guid["guid-4"].rejected_reason is None

    # A later search re-ranks from scratch, so the choice no longer stands.
    acq.state = S.CANDIDATES_READY
    transition(session, acq, S.SEARCHING, "searching again")
    session.commit()
    assert approval.preferred_id(acq) is None


def test_prefer_refusals(session: Session, acq: Acquisition) -> None:
    by_guid = {c.prowlarr_guid: c for c in acq.candidates}
    with pytest.raises(ValueError, match="rejected"):
        approval.prefer(session, acq, by_guid["guid-5"], by="test")  # quality rejection
    with pytest.raises(ValueError, match="rejected"):
        approval.prefer(session, acq, by_guid["guid-6"], by="test")  # match rejection

    # A torrent already tried: next_candidate would skip it, so refuse rather than
    # promote something Approve will not fetch.
    session.add(
        GrabAttempt(
            acquisition=acq,
            candidate_id=by_guid["guid-2"].id,
            deluge_instance="a",
            infohash="0" * 40,
            download_location="/p/1",
            completed_location="/c/1",
            outcome="failed",
        )
    )
    session.commit()
    assert by_guid["guid-2"].id not in approval.preferable(acq)
    with pytest.raises(ValueError, match="already tried"):
        approval.prefer(session, acq, by_guid["guid-2"], by="test")

    other = Acquisition(state=S.AWAITING_APPROVAL, origin="manual")
    session.add(other)
    session.commit()
    with pytest.raises(ValueError, match="another acquisition"):
        approval.prefer(session, other, by_guid["guid-3"], by="test")

    acq.state = S.DOWNLOADING
    session.commit()
    assert approval.preferable(acq) == {}
    with pytest.raises(ValueError, match="cannot prefer"):
        approval.prefer(session, acq, by_guid["guid-3"], by="test")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """The UI test client with no format rule, so the captured PandaCD search ranks several
    encodes and the page has rows to prefer."""
    from alembic import command

    from hermes.cli import alembic_config

    policy = Policy.model_validate(
        {
            "beets": {"agent_url": BEETS},
            "quality": {"max_sample_rate_khz": None, "require_format": "", "keep_candidates": 2},
            "deluge": {"instances": {"a": {"url": "http://deluge-a.test", "indexers": []}}},
        }
    )
    command.upgrade(alembic_config(settings), "head")
    clients = Clients.from_config(settings, policy)
    clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
    app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
    with TestClient(app) as c:
        yield c


def test_prefer_from_the_page_and_the_api(respx_mock: respx.Router, client: TestClient) -> None:
    respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
    respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
    respx_mock.get(f"{PROWLARR}/api/v1/search").respond(json=load("prowlarr/search_pandacd_nin"))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    body = client.get("/api/acquisitions/1").json()
    assert body["state"] == "AWAITING_APPROVAL"
    ranked = [c for c in body["candidates"] if c["rank"]]
    cut = [c for c in body["candidates"] if (c["rejected_reason"] or "").startswith("acceptable")]
    assert len(ranked) == 2 and cut, "keep_candidates 2 leaves cuts to use anyway"
    assert "FLAC" in ranked[0]["title"]

    page = _html(client, "/acquisitions/1?walk=1").text
    assert page.count(">Prefer<") == 1 and page.count(">Use anyway<") == len(cut)
    assert "preferred by you" not in page
    assert 'action="/acquisitions/1/prefer?walk=1"' in page  # keeps the walk context

    # Prefer the second ranked row: the target card now shows it, nothing was grabbed.
    resp = client.post(
        "/acquisitions/1/prefer?walk=1",
        data={"candidate_id": ranked[1]["id"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/acquisitions/1?walk=1")
    page = _html(client, resp.headers["location"]).text
    assert "Preferred Nine Inch Nails - The Slip [2008] [MP3 320]; Approve will fetch it." in page
    assert "preferred by you" in page and "a new search resets this" in page
    card = page.split("Approve will fetch</h2>")[1].split("</dl>")[0]
    assert "[MP3 320]" in card and "rank #1" in card
    assert client.get("/api/acquisitions/1").json()["state"] == "AWAITING_APPROVAL"

    # Use anyway on a keep_candidates cut, through the API.
    resp = client.post("/api/acquisitions/1/prefer", json={"candidate_id": cut[0]["id"], "by": "t"})
    assert resp.status_code == 200
    top = next(c for c in resp.json()["candidates"] if c["rank"] == 1)
    assert top["id"] == cut[0]["id"] and top["rejected_reason"] is None
    assert resp.json()["events"][-1]["message"].endswith("(by t)")
    assert client.post("/api/acquisitions/1/prefer", json={"candidate_id": 9999}).status_code == 404
    rejected = [
        c
        for c in body["candidates"]
        if c["rejected_reason"] and not c["rejected_reason"].startswith("acceptable")
    ]
    if rejected:
        resp = client.post("/api/acquisitions/1/prefer", json={"candidate_id": rejected[0]["id"]})
        assert resp.status_code == 409
