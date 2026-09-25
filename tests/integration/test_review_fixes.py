"""Regression tests for the findings of the 2026-09-07 code review. Each test names the
finding it pins so the next reviewer can see what was already caught."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import utcnow
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import importer, observer
from tests.fakes import FakeBeetsAgent, FakeDeluge
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

BEETS = "http://beets.test:8338"
PROWLARR = "http://prowlarr.test"
DELUGE = "http://deluge-dev.test"
SLIP_RG = "5990b23f-6412-3eaf-989c-a4831f4f95f7"
TORRENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "torrents"
    / "gettysburg-audio.torrent"
)
INFOHASH = "7838b1f9b3ab320d9e1b265dff0334bcb6238577"
SECRET = "0123456789abcdef0123456789abcdef"


def _policy(**overrides) -> Policy:
    base = {
        "dry_run": False,
        "approval": {"timid": False},
        "beets": {"agent_url": BEETS},
        "quality": {"max_sample_rate_khz": None},
        "deluge": {
            "instances": {"dev": {"url": DELUGE, "indexers": ["PandaCD"]}},
            "stall_hours": 2,
        },
    }
    base.update(overrides)
    return Policy.model_validate(base)


def _slip() -> dict:
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
                "releases": [
                    {
                        "id": "rel-slip",
                        "title": "The Slip",
                        "status": "Official",
                        "date": "2008-05-05",
                        "country": "XW",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def stack(respx_mock: respx.Router, settings: Settings):
    from alembic import command

    from hermes.cli import alembic_config

    class Stack:
        def __init__(self) -> None:
            self.deluge = FakeDeluge(respx_mock, DELUGE)
            self.agent = FakeBeetsAgent(respx_mock, BEETS)
            respx_mock.get(f"{MB}/release-group/").respond(json=_slip())
            respx_mock.get(url__regex=rf"{MB}/release/[^/?]+").respond(json={"media": []})
            respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(
                json=_slip()["release-groups"][0]
            )
            respx_mock.get(f"{PROWLARR}/api/v1/search").respond(
                json=load("prowlarr/search_pandacd_nin")
            )
            self.download = respx_mock.get(
                url__regex=r"http://localhost:9696/1/download.*"
            ).respond(content=TORRENT.read_bytes())
            command.upgrade(alembic_config(settings), "head")
            self.client: TestClient | None = None

        def app(self, policy: Policy) -> TestClient:
            clients = Clients.from_config(settings, policy)
            clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
            app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
            self.client = TestClient(app)
            self.client.__enter__()
            return self.client

        async def tick(self, *, hours_later: float = 0, minutes_later: float = 0) -> dict:
            app = self.client.app
            with app.state.session_factory() as session:
                now = utcnow() + timedelta(hours=hours_later, minutes=minutes_later)
                return await observer.tick(session, app.state.context, now=now)

        async def import_tick(self) -> dict:
            app = self.client.app
            with app.state.session_factory() as session:
                return await importer.tick(session, app.state.context)

        def get(self, acq_id: int) -> dict:
            return self.client.get(f"/api/acquisitions/{acq_id}").json()

        def close(self) -> None:
            if self.client:
                self.client.__exit__(None, None, None)

    s = Stack()
    yield s
    s.close()


def _request(client: TestClient) -> dict:
    return client.post(
        "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
    ).json()


# -- safety: fallback respects the approval policy --------------------------------------


async def test_stall_fallback_waits_for_approval_in_timid_mode(stack) -> None:
    client = stack.app(_policy(approval={"timid": True}))
    body = _request(client)
    assert body["state"] == "AWAITING_APPROVAL"
    body = client.post(f"/api/acquisitions/{body['id']}/approve").json()
    assert body["state"] == "SUBMITTED"
    stack.deluge.progress(INFOHASH, 1_000)
    await stack.tick()
    await stack.tick(hours_later=3)
    body = stack.get(body["id"])
    assert body["state"] == "STALLED", "no second grab without a human"
    assert body["events"][-1]["message"].startswith("next candidate needs approval")
    assert len(stack.deluge.added) == 1
    # A human may approve the fallback from STALLED.
    body = client.post(f"/api/acquisitions/{body['id']}/approve", json={"by": "you"}).json()
    assert body["state"] in ("SUBMITTED", "FAILED")  # FAILED only if no candidate is left


async def test_stall_fallback_waits_in_dry_run(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    assert body["state"] == "SUBMITTED"
    client.app.state.policy.dry_run = True  # operator flips dry run after the grab
    stack.deluge.progress(INFOHASH, 1_000)
    await stack.tick()
    await stack.tick(hours_later=3)
    assert stack.get(body["id"])["state"] == "STALLED" and len(stack.deluge.added) == 1


# -- secrets ------------------------------------------------------------------------------


def test_prowlarr_key_never_reaches_events(stack, respx_mock: respx.Router) -> None:
    client = stack.app(_policy())
    respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(429, text="slow down")
    body = _request(client)
    blob = str(body)
    assert SECRET not in blob and "apikey" not in blob
    assert body["state"] == "FAILED" and "HTTP 429" in body["error"]


# -- re-search after a grab attempt ---------------------------------------------------


async def test_research_after_attempt_keeps_referenced_candidates(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    acq_id = body["id"]
    stack.deluge.remove(INFOHASH)
    await stack.tick()
    await stack.tick(hours_later=0.3)  # missing past the observer's grace
    assert stack.get(acq_id)["state"] == "FAILED"
    body = client.post(f"/api/acquisitions/{acq_id}/search").json()
    assert body["state"] != "SEARCHING", "never stranded"
    superseded = [
        c for c in body["candidates"] if c["rejected_reason"] == "superseded by a later search"
    ]
    assert len(superseded) == 1, "the attempted candidate row survives for the attempt's sake"
    # The already-tried torrent is not offered again: same guid, so no second attempt on it.
    assert body["state"] == "FAILED" and body["error"] == "no candidate left to submit"
    assert len(body["attempts"]) == 1


# -- observer: finished states and the move grace period ----------------------------------


async def test_finished_but_queued_counts_as_done(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    stack.deluge.finish(INFOHASH)
    stack.deluge.torrents[INFOHASH]["state"] = "Queued"  # over the seeding slot limit
    await stack.tick()
    assert stack.get(body["id"])["state"] == "READY_FOR_BEETS"


async def test_unmoved_finished_torrent_is_accepted_after_grace_without_event_spam(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    stack.deluge.finish(INFOHASH, moved=False)
    for _ in range(5):
        await stack.tick(minutes_later=1)
    events = stack.get(body["id"])["events"]
    waiting = [e for e in events if e["message"].startswith("finished; waiting")]
    assert len(waiting) == 1, "one event, not one per tick"
    await stack.tick(minutes_later=15)
    body = stack.get(body["id"])
    assert body["state"] == "READY_FOR_BEETS"
    assert any("did not move" in e["message"] for e in body["events"])
    assert body["attempts"][0]["completed_path"].startswith(
        f"/downloads/pending/hermes/{body['id']}"
    )


async def test_label_failure_still_records_the_attempt(stack) -> None:
    client = stack.app(_policy())
    stack.deluge.plugins = []  # label.set_torrent will fail (KeyError in the fake -> error)
    stack.deluge.labels = set()

    original = stack.deluge._dispatch

    def dispatch(method, params):
        if method == "label.set_torrent":
            raise RuntimeError("Label plugin not enabled")
        return original(method, params)

    stack.deluge._dispatch = dispatch
    body = _request(client)
    assert body["state"] == "SUBMITTED"
    assert len(body["attempts"]) == 1
    assert any("could not label" in e["message"] for e in body["events"])


# -- importer: interrupted or dirty imports are never called done ----------------------------


async def test_import_with_error_or_bad_exit_needs_review_even_if_album_exists(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    stack.deluge.finish(INFOHASH)
    await stack.tick()
    await stack.import_tick()
    album = {
        "id": 1,
        "albumartist": "Nine Inch Nails",
        "album": "The Slip",
        "path": "/downloads/hermes/1/x",  # still pointing at the seeded files
        "quality": {"items": 10, "formats": ["FLAC"], "lossy_items": 0},
    }
    stack.agent.finish("job1", exit_code=1, imported_album=album)
    await stack.import_tick()
    body = stack.get(body["id"])
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    assert "did not finish cleanly" in body["events"][-1]["message"]


async def test_import_whose_album_is_outside_the_library_needs_review(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    stack.deluge.finish(INFOHASH)
    await stack.tick()
    await stack.import_tick()
    album = {
        "id": 1,
        "albumartist": "Nine Inch Nails",
        "album": "The Slip",
        "path": "/downloads/hermes/1/Nine Inch Nails - The Slip",
        "quality": {"items": 10, "formats": ["FLAC"], "lossy_items": 0},
    }
    stack.agent.finish("job1", exit_code=0, imported_album=album)
    await stack.import_tick()
    body = stack.get(body["id"])
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    assert "outside /music" in body["events"][-1]["message"]


# -- repeated request, UI guards ------------------------------------------------------------


def test_repeated_request_does_not_approve_in_timid_mode(stack) -> None:
    client = stack.app(_policy(approval={"timid": True}))
    first = _request(client)
    second = _request(client)
    assert first["id"] == second["id"] and second["state"] == "AWAITING_APPROVAL"
    assert stack.deluge.added == []


def test_ui_cancel_on_uncancellable_state_is_409_not_500(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)  # SUBMITTED: not cancellable
    resp = client.post(f"/acquisitions/{body['id']}/cancel", follow_redirects=False)
    assert resp.status_code == 409
    page = client.get(f"/acquisitions/{body['id']}").text
    assert "/cancel" not in page, "the button is not rendered when the state forbids it"


# -- ranking: release type agreement and year mismatch --------------------------------------


def test_single_does_not_stand_in_for_album() -> None:
    from hermes.config import QualityPolicy
    from hermes.integrations.prowlarr import ReleaseResult
    from hermes.services.ranking import evaluate

    def r(title: str, guid: str) -> ReleaseResult:
        return ReleaseResult(
            guid=guid, indexer_id=1, indexer="Tracker B", title=title, size=300_000_000, seeders=9
        )

    ranked = evaluate(
        [
            r("Massive Attack - Protection (1994) [Single] [FLAC Lossless / WEB]", "single"),
            r(
                "Massive Attack - Protection (1994) [Album] [FLAC Lossless / CD / Log / Cue]",
                "album",
            ),
            r("Massive Attack - Protection (2019) [Album] [FLAC Lossless / WEB]", "wrongyear"),
        ],
        artist="Massive Attack",
        title="Protection",
        year=1994,
        policy=QualityPolicy(),
        target_types={"Album"},
    )
    by_guid = {c.release.guid: c for c in ranked}
    assert by_guid["album"].rank == 1
    assert "does not match the target" in (by_guid["single"].rejected_reason or "")
    assert "different release" in " ".join(by_guid["wrongyear"].match.notes)
    assert by_guid["wrongyear"].rejected_reason is not None


def test_secondary_type_targets_accept_matching_tracker_types() -> None:
    from hermes.services.ranking import type_mismatch

    assert type_mismatch("Live album", {"Album", "Live"}) is None
    assert type_mismatch("Soundtrack", {"Album", "Soundtrack"}) is None
    assert type_mismatch("Live album", {"Album"}) is not None
    assert type_mismatch(None, {"Album"}) is None  # PandaCD titles carry no type


# -- resolution and text ----------------------------------------------------------------------


def test_confident_ep_is_not_hidden_by_a_weak_album() -> None:
    from hermes.config import ResolutionPolicy
    from hermes.integrations.musicbrainz import parse_release_group
    from hermes.services.resolution import choose, score_candidates

    ep = {
        "id": "ep",
        "score": 100,
        "title": "Rival Dealer",
        "primary-type": "EP",
        "first-release-date": "2013",
        "artist-credit": [{"name": "Burial", "artist": {"id": "b", "name": "Burial"}}],
    }
    album = {
        "id": "al",
        "score": 95,
        "title": "Rival Dealer (Remixes)",
        "primary-type": "Album",
        "first-release-date": "2014",
        "artist-credit": [{"name": "Burial", "artist": {"id": "b", "name": "Burial"}}],
    }
    policy = ResolutionPolicy()
    res = choose(
        score_candidates(
            [parse_release_group(album), parse_release_group(ep)], "Burial", "Rival Dealer", policy
        ),
        policy,
    )
    assert res.outcome == "resolved" and res.target.id == "ep"


def test_punctuation_only_titles_match_themselves() -> None:
    from hermes.services.text import similarity

    assert similarity("( )", "( )") == 1.0
    assert similarity("†", "†") == 1.0
    assert similarity("( )", "Dummy") == 0.0


# -- config validation --------------------------------------------------------------------------


def test_policy_validators_reject_unsafe_deluge_settings() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="default_instance"):
        Policy.model_validate({"deluge": {"default_instance": "nope"}})
    with pytest.raises(ValidationError, match="must differ"):
        Policy.model_validate({"deluge": {"pending_root": "/d/x", "completed_root": "/d/x/"}})
    with pytest.raises(ValidationError):
        Policy.model_validate({"deluge": {"label": "Hermes Label"}})


def test_path_mapping_respects_segment_boundaries() -> None:
    from hermes.config import PathsPolicy

    paths = PathsPolicy(deluge_root="/downloads/complete/", beets_root="/downloads")
    assert paths.to_beets("/downloads/complete/hermes/12") == "/downloads/hermes/12"
    assert paths.to_beets("/downloads/completed/x") == "/downloads/completed/x"
    assert paths.to_beets("/downloads/complete") == "/downloads"


def test_bencode_rejects_junk_with_bencode_error_only() -> None:
    from hermes.bencode import BencodeError, TorrentInfo

    for junk in (
        b"i12",
        b"3:ab",
        b"d1:ke",
        b"l" * 5000,
        b"d4:infoi1ee",
        b"d4:infod4:name3:abc5:filesl3:xxxeee",
    ):
        with pytest.raises(BencodeError):
            TorrentInfo(junk)


def test_hermes_check_reports_effective_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes.app import effective_policy

    settings = Settings(_env_file=None, hermes_dry_run=False, hermes_data_dir=tmp_path)  # type: ignore[call-arg]
    assert effective_policy(settings, Policy()).dry_run is False
