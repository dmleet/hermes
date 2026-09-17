"""M3: approval, budget, submit to Deluge, observer, and the UI, with MusicBrainz, beets,
Prowlarr and Deluge all faked behind respx."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import Acquisition, AlbumTarget, Candidate, Origin, utcnow
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.services import observer
from tests.fakes import FakeDeluge
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


def _policy(**overrides) -> Policy:
    base = {
        "dry_run": False,
        "approval": {"timid": False},
        "beets": {"agent_url": BEETS},
        "quality": {"max_sample_rate_khz": None},
        "deluge": {
            "instances": {"dev": {"url": DELUGE, "indexers": ["PandaCD"]}},
            "pending_root": "/downloads/pending/hermes",
            "completed_root": "/downloads/complete/hermes",
            "stall_hours": 2,
        },
        "paths": {"deluge_root": "/downloads/complete", "beets_root": "/downloads"},
    }
    base.update(overrides)
    return Policy.model_validate(base)


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
def stack(respx_mock: respx.Router, settings: Settings, tmp_path: Path):
    """A running app plus the fake Deluge, parameterised by policy via `stack.app(policy)`."""

    class Stack:
        def __init__(self) -> None:
            self.deluge = FakeDeluge(respx_mock, DELUGE)
            respx_mock.get(f"{MB}/release-group/").respond(json=_slip_search())
            respx_mock.get(url__regex=rf"{BEETS}/library/.*").respond(json={"albums": []})
            respx_mock.get(f"{PROWLARR}/api/v1/search").respond(
                json=load("prowlarr/search_pandacd_nin")
            )
            respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(
                content=TORRENT.read_bytes()
            )
            self.client: TestClient | None = None

        def app(self, policy: Policy) -> TestClient:
            from alembic import command

            from hermes.cli import alembic_config

            command.upgrade(alembic_config(settings), "head")
            clients = Clients.from_config(settings, policy)
            clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
            app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
            self.client = TestClient(app)
            self.client.__enter__()
            return self.client

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


async def _tick(client: TestClient, *, hours_later: float = 0) -> dict:
    app = client.app
    with app.state.session_factory() as session:
        return await observer.tick(
            session, app.state.context, now=utcnow() + timedelta(hours=hours_later)
        )


# -- submit ------------------------------------------------------------------------


def test_manual_request_is_submitted_to_deluge(stack, settings: Settings) -> None:
    client = stack.app(_policy())
    body = _request(client)
    assert body["state"] == "SUBMITTED", body["events"]
    (attempt,) = body["attempts"]
    assert attempt["infohash"] == INFOHASH and attempt["deluge_instance"] == "dev"
    assert attempt["download_location"] == f"/downloads/pending/hermes/{body['id']}"
    assert attempt["completed_location"] == f"/downloads/complete/hermes/{body['id']}"
    (added,) = stack.deluge.added
    assert added["options"]["move_completed"] is True
    assert added["options"]["move_completed_path"] == attempt["completed_location"]
    assert stack.deluge.torrents[INFOHASH]["label"] == "hermes"
    # Hermes keeps no torrent file: the announce URL would carry a private tracker's passkey.
    assert not (settings.hermes_data_dir / "torrents").exists()
    assert attempt["download_shape"] == {"audio_files": 3, "layout": [3], "cd_rip": False}
    messages = [e["message"] for e in body["events"]]
    assert any(m.startswith("approved: manual request") for m in messages)
    assert any(m.startswith("added to Deluge dev") for m in messages)
    assert body["approved_by"] == "policy"


def test_interrupted_submit_is_recovered_by_adopting_the_torrent(
    stack, respx_mock: respx.Router
) -> None:
    """A submit that died after the Deluge add but before the attempt commit: the next
    submit finds the torrent at this acquisition's directory and adopts it, without a
    second fetch from the tracker."""
    client = stack.app(_policy(dry_run=True))
    body = _request(client)  # dry run: candidates ready, nothing added
    assert body["state"] == "CANDIDATES_READY"
    # Deluge already holds the torrent at the acquisition's pending path.
    stack.deluge.torrents[INFOHASH] = {
        "name": "gettysburg_shurtagal_librivox",
        "state": "Downloading",
        "progress": 0.0,
        "is_finished": False,
        "save_path": f"/downloads/pending/hermes/{body['id']}",
        "move_completed_path": f"/downloads/complete/hermes/{body['id']}",
        "move_completed": True,
        "total_done": 0,
        "total_wanted": 10,
        "num_seeds": 1,
        "num_peers": 0,
        "download_payload_rate": 0,
        "label": "",
    }
    fetches = respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(
        content=TORRENT.read_bytes()
    )
    before = fetches.call_count
    client.app.state.policy = _policy()  # dry run off for the approval
    client.app.state.context.policy = client.app.state.policy
    # Without a candidate carrying that infohash nothing is adopted: the fetch goes ahead and
    # Deluge's "already in session" answer records the attempt against the fetched candidate.
    body = client.post(f"/api/acquisitions/{body['id']}/approve").json()
    assert body["state"] == "SUBMITTED", body["events"]
    assert fetches.call_count == before + 1
    assert any(
        "matches no candidate's infohash; not adopted" in e["message"] for e in body["events"]
    )
    (attempt,) = body["attempts"]
    assert attempt["infohash"] == INFOHASH


def test_interrupted_submit_is_adopted_when_a_candidate_names_the_infohash(
    stack, respx_mock: respx.Router
) -> None:
    """Review L7: adoption identifies the torrent by a candidate's reported infohash, never
    by guessing the top candidate; a torrent at /hermes/10 is not the one for /hermes/1."""
    from sqlalchemy import select

    from hermes.domain.models import Candidate

    client = stack.app(_policy(dry_run=True))
    body = _request(client)
    acq_id = body["id"]
    with client.app.state.session_factory() as session:
        top = session.scalars(
            select(Candidate).where(Candidate.acquisition_id == acq_id, Candidate.rank == 1)
        ).one()
        top.parsed_quality = {**top.parsed_quality, "info_hash": INFOHASH.upper()}
        session.commit()
        top_id = top.id

    def torrent_at(path: str) -> dict:
        return {
            "name": "gettysburg_shurtagal_librivox",
            "state": "Downloading",
            "progress": 0.0,
            "is_finished": False,
            "save_path": path,
            "move_completed_path": path,
            "move_completed": True,
            "total_done": 0,
            "total_wanted": 10,
            "num_seeds": 1,
            "num_peers": 0,
            "download_payload_rate": 0,
            "label": "",
        }

    # A torrent for another acquisition whose directory shares this one's prefix.
    stack.deluge.torrents["f" * 40] = torrent_at(f"/downloads/pending/hermes/{acq_id}0")
    stack.deluge.torrents[INFOHASH] = torrent_at(f"/downloads/pending/hermes/{acq_id}")
    fetches = respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(
        content=TORRENT.read_bytes()
    )
    before = fetches.call_count
    client.app.state.policy = _policy()
    client.app.state.context.policy = client.app.state.policy
    body = client.post(f"/api/acquisitions/{acq_id}/approve").json()
    assert body["state"] == "SUBMITTED", body["events"]
    (attempt,) = body["attempts"]
    assert attempt["infohash"] == INFOHASH and attempt["candidate_id"] == top_id
    assert fetches.call_count == before and stack.deluge.added == []
    assert any(e["message"].startswith("adopted gettysburg") for e in body["events"])


def test_adoption_with_no_candidates_does_not_raise(stack) -> None:
    from hermes.services.submit import _candidate_for

    client = stack.app(_policy(dry_run=True))
    body = _request(client)
    with client.app.state.session_factory() as session:
        from hermes.domain.models import Acquisition

        acq = session.get(Acquisition, body["id"])
        for c in list(acq.candidates):
            session.delete(c)
        session.commit()
        assert _candidate_for(acq, "0" * 40) is None


def test_kept_torrent_files_are_removed_at_startup(stack, settings: Settings) -> None:
    """Review M16: only the old Hermes naming goes; a data directory that happens to be
    the download share keeps the torrent client's own files and the directory itself."""
    kept = settings.hermes_data_dir / "torrents"
    kept.mkdir()
    ours = kept / f"1-{INFOHASH}.torrent"
    ours.write_bytes(b"d8:announce3:urle")
    theirs = kept / "some-album.torrent"
    theirs.write_bytes(b"d8:announce3:urle")
    (kept / "state").mkdir()
    stack.app(_policy())
    assert not ours.exists()
    assert theirs.exists() and (kept / "state").is_dir() and kept.exists()


def test_submit_skips_a_torrent_whose_files_exceed_the_sample_rate_limit(stack) -> None:
    """The size check at search time is an estimate; the fetched torrent's per-file sizes
    are the real test. Track lengths that make the fixture's files look like 192 kHz
    audio get every candidate skipped with the reason, and the acquisition fails over."""
    from sqlalchemy import select

    from hermes.domain.models import AlbumTarget

    client = stack.app(_policy(dry_run=True))
    body = _request(client)
    with client.app.state.session_factory() as session:
        target = session.scalars(select(AlbumTarget)).one()
        target.track_lengths = {"release": "x", "ms": [1000, 1000, 1000]}  # one second each
        session.commit()
    client.app.state.policy = _policy(quality={"max_sample_rate_khz": 96})
    client.app.state.context.policy = client.app.state.policy
    body = client.post(f"/api/acquisitions/{body['id']}/approve").json()
    assert body["state"] == "FAILED", body["events"]
    reasons = [c["rejected_reason"] or "" for c in body["candidates"]]
    assert any("skipped at submit: about 192 kHz" in r for r in reasons), reasons
    assert stack.deluge.added == []


def test_dry_run_stops_before_submit(stack) -> None:
    client = stack.app(_policy(dry_run=True))
    body = _request(client)
    assert body["state"] == "CANDIDATES_READY"
    assert any(e["message"].startswith("dry_run: would submit #1") for e in body["events"])
    assert stack.deluge.added == []
    # A human approval in dry run is recorded but still does not grab.
    body = client.post(f"/api/acquisitions/{body['id']}/approve", json={"by": "tester"}).json()
    assert body["state"] == "CANDIDATES_READY" and body["approved_by"] == "tester"
    assert stack.deluge.added == []


def test_unrouted_indexer_is_skipped_then_failed(stack) -> None:
    policy = _policy()
    policy.deluge.instances["dev"].indexers = ["Tracker B"]  # PandaCD no longer routes
    client = stack.app(policy)
    body = _request(client)
    assert body["state"] == "FAILED" and body["error"] == "no candidate left to submit"
    skipped = [
        c
        for c in body["candidates"]
        if (c["rejected_reason"] or "").startswith("skipped at submit")
    ]
    assert (
        len(skipped) == 1
        and "no Deluge instance for indexer PandaCD" in skipped[0]["rejected_reason"]
    )
    assert stack.deluge.added == []


def test_deluge_add_failure_is_retryable(stack) -> None:
    client = stack.app(_policy())
    stack.deluge.fail_add = "disk full"
    body = _request(client)
    assert body["state"] == "FAILED" and "Deluge dev unavailable" in body["error"]
    stack.deluge.fail_add = None
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "SUBMITTED"


# -- approval and budget ---------------------------------------------------------------


def _auto_acquisition(client: TestClient) -> int:
    """An automated-origin acquisition with candidates, as discovery (M5) will create."""
    # Its own target: the database allows one active acquisition per target, and the
    # manual request for The Slip may still be in flight.
    auto_rg = "a0000000-0000-4000-8000-000000000001"
    with client.app.state.session_factory() as session:
        target = session.scalar(
            select(AlbumTarget).where(AlbumTarget.release_group_mbid == auto_rg)
        ) or AlbumTarget(release_group_mbid=auto_rg, artist_name="Nine Inch Nails", title="Ghosts")
        acq = Acquisition(album_target=target, state=S.CANDIDATES_READY, origin=Origin.AUTO)
        session.add(acq)
        session.flush()
        for rank, (title, size, free) in enumerate(
            [
                ("Nine Inch Nails - The Slip [2008] [FLAC Lossless]", 270_000_000, True),
                ("Nine Inch Nails - The Slip [2008] [FLAC 24bit Lossless]", 900_000_000, False),
            ],
            start=1,
        ):
            session.add(
                Candidate(
                    acquisition=acq,
                    prowlarr_guid=f"g{rank}",
                    indexer_id=1,
                    indexer_name="PandaCD",
                    title=title,
                    download_url="http://localhost:9696/1/download?link=abc",
                    size_bytes=size,
                    seeders=2,
                    freeleech=free,
                    rank=rank,
                )
            )
        session.commit()
        return acq.id


async def _decide(client: TestClient, acq_id: int) -> dict:
    from hermes.services.approval import decide

    app = client.app
    with app.state.session_factory() as session:
        acq = session.get(Acquisition, acq_id)
        await decide(session, app.state.context, acq)
    return client.get(f"/api/acquisitions/{acq_id}").json()


async def test_automated_waits_for_approval_then_human_approves(stack) -> None:
    client = stack.app(_policy())
    acq_id = _auto_acquisition(client)
    body = await _decide(client, acq_id)
    assert body["state"] == "AWAITING_APPROVAL"
    assert "no auto-approve rule" in body["events"][-1]["message"]
    # Reject is available; cancel too. Approve submits.
    body = client.post(f"/api/acquisitions/{acq_id}/approve", json={"by": "you"}).json()
    assert body["state"] == "SUBMITTED" and body["approved_by"] == "you"
    assert len(stack.deluge.added) == 1


async def test_auto_approve_rule(stack) -> None:
    policy = _policy(
        approval={
            "timid": False,
            "auto_approve": [{"freeleech": True, "max_bytes": 500_000_000}],
        }
    )
    client = stack.app(policy)
    first = await _decide(client, _auto_acquisition(client))
    assert first["state"] == "SUBMITTED"
    assert any("auto-approve rule matched" in e["message"] for e in first["events"])


async def test_timid_mode_waits_for_everything(stack) -> None:
    """The default: manual and automated acquisitions alike wait for a human."""
    client = stack.app(_policy(approval={"timid": True, "auto_approve": [{"freeleech": True}]}))
    body = _request(client)
    assert body["state"] == "AWAITING_APPROVAL"
    assert body["events"][-1]["message"] == "waiting for approval (timid mode)"
    assert stack.deluge.added == []
    auto = await _decide(client, _auto_acquisition(client))
    assert auto["state"] == "AWAITING_APPROVAL"  # the matching rule is ignored in timid mode
    body = client.post(f"/api/acquisitions/{body['id']}/approve", json={"by": "you"}).json()
    assert body["state"] == "SUBMITTED" and len(stack.deluge.added) == 1


def test_fetch_failure_defers_the_indexer(stack, respx_mock: respx.Router) -> None:
    """Tokens exhausted (or a tracker outage) must not discard ranked candidates."""
    import httpx

    client = stack.app(_policy())
    respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").mock(
        side_effect=httpx.ConnectError("tracker refused")
    )
    body = _request(client)
    assert body["state"] == "FAILED" and body["error"].startswith(
        "torrent fetch failed, retry later"
    )
    ranked = [c for c in body["candidates"] if c["rank"]]
    assert len(ranked) == 1, "the candidate keeps its rank for a later retry"
    assert any("deferring its candidates" in e["message"] for e in body["events"])
    assert stack.deluge.added == []
    # Once the fetch works again a re-search submits it.
    respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(
        content=TORRENT.read_bytes()
    )
    body = client.post(f"/api/acquisitions/{body['id']}/search").json()
    assert body["state"] == "SUBMITTED"


def test_reject_and_cancel_rules(stack) -> None:
    client = stack.app(_policy(dry_run=True))
    body = _request(client)  # CANDIDATES_READY in dry run
    assert (
        client.post(f"/api/acquisitions/{body['id']}/reject").status_code == 409
    )  # only from AWAITING/NEEDS_REVIEW
    cancelled = client.post(f"/api/acquisitions/{body['id']}/cancel", json={"by": "me"}).json()
    assert cancelled["state"] == "CANCELLED"
    assert client.post(f"/api/acquisitions/{body['id']}/approve").status_code == 409


# -- observer -----------------------------------------------------------------------


async def test_observer_follows_download_to_ready_for_beets(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    acq_id = body["id"]
    # Deluge reports the torrent as active immediately, so one tick moves it on.
    assert (await _tick(client)) == {"observed": 1}
    assert client.get(f"/api/acquisitions/{acq_id}").json()["state"] == "DOWNLOADING"

    stack.deluge.progress(INFOHASH, 1_000_000)
    await _tick(client)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["state"] == "DOWNLOADING" and body["attempts"][0]["last_total_done"] == 1_000_000

    stack.deluge.finish(INFOHASH, moved=False)
    await _tick(client)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["state"] == "DOWNLOADING"
    assert "waiting for Deluge to move" in body["events"][-1]["message"]

    stack.deluge.finish(INFOHASH, moved=True)
    await _tick(client)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["state"] == "READY_FOR_BEETS"
    (attempt,) = body["attempts"]
    assert attempt["outcome"] == "completed"
    assert (
        attempt["completed_path"]
        == f"/downloads/complete/hermes/{acq_id}/gettysburg_shurtagal_librivox"
    )
    done = body["events"][-1]
    assert done["data"]["beets_path"] == f"/downloads/hermes/{acq_id}/gettysburg_shurtagal_librivox"
    assert (await _tick(client)) == {}  # nothing active any more


async def test_stall_falls_back_to_next_candidate_then_fails(stack) -> None:
    client = stack.app(_policy())
    acq_id = _auto_acquisition(client)
    body = client.post(f"/api/acquisitions/{acq_id}/approve").json()
    assert body["state"] == "SUBMITTED"
    first_hash = body["attempts"][0]["infohash"]
    stack.deluge.progress(first_hash, 5_000_000)
    await _tick(client)  # DOWNLOADING, progress recorded now
    # Same torrent bytes for both candidates in this fake, so the second add collides:
    # Deluge says "already in session" and Hermes adopts it. Simulate that by removing
    # the first torrent from the fake before the fallback.
    await _tick(client, hours_later=3)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    outcomes = [a["outcome"] for a in body["attempts"]]
    assert outcomes[0] == "stalled"
    assert any("no progress for 2h" in e["message"] for e in body["events"])
    assert body["state"] == "SUBMITTED" and len(body["attempts"]) == 2

    # The second attempt adopted the same fake torrent: one tick records its progress,
    # then it stalls too and there is no third candidate.
    await _tick(client, hours_later=3.1)
    await _tick(client, hours_later=6)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["state"] == "FAILED" and body["error"] == "no candidate left to submit"


async def test_removed_torrent_fails_and_error_state_falls_back(stack) -> None:
    client = stack.app(_policy())
    body = _request(client)
    stack.deluge.remove(INFOHASH)
    await _tick(client)  # one miss is tolerated (a daemon still loading its session)
    body = client.get(f"/api/acquisitions/{body['id']}").json()
    assert body["state"] == "SUBMITTED" and "checking again" in body["events"][-1]["message"]
    await _tick(client)
    body = client.get(f"/api/acquisitions/{body['id']}").json()
    assert body["state"] == "FAILED" and "no longer in Deluge" in body["error"]
    assert body["attempts"][0]["outcome"] == "removed"

    acq_id = _auto_acquisition(client)
    client.post(f"/api/acquisitions/{acq_id}/approve")
    h = client.get(f"/api/acquisitions/{acq_id}").json()["attempts"][0]["infohash"]
    stack.deluge.error(h)
    await _tick(client)
    body = client.get(f"/api/acquisitions/{acq_id}").json()
    assert body["attempts"][0]["outcome"] == "failed"
    assert any("Deluge reports an error" in e["message"] for e in body["events"])


async def test_observer_survives_unreachable_deluge(stack, respx_mock: respx.Router) -> None:
    import httpx

    client = stack.app(_policy())
    body = _request(client)
    respx_mock.post(f"{DELUGE}/json").mock(side_effect=httpx.ConnectError("down"))
    assert (await _tick(client)) == {"unreachable": 1}
    assert client.get(f"/api/acquisitions/{body['id']}").json()["state"] == "SUBMITTED"


def test_observe_endpoint(stack) -> None:
    client = stack.app(_policy())
    _request(client)
    assert client.post("/api/jobs/observe").json() == {"observed": 1}


# -- UI ---------------------------------------------------------------------------------


def test_ui_pages_render_and_forms_work(stack) -> None:
    client = stack.app(_policy(dry_run=True))
    resp = client.post(
        "/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"}, follow_redirects=False
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/acquisitions/")
    detail = client.get(resp.headers["location"])
    assert detail.status_code == 200
    assert (
        "The Slip" in detail.text
        and "[FLAC Lossless]" in detail.text
        and "MP3 is not FLAC" in detail.text
    )
    assert "dry run" in detail.text
    home = client.get("/")
    assert (
        home.status_code == 200
        and "Nine Inch Nails" in home.text
        and "CANDIDATES_READY" in home.text
    )
    listing = client.get("/api/acquisitions").json()
    assert listing[0]["best_candidate"] == "Nine Inch Nails - The Slip [2008] [FLAC Lossless]"
    assert client.get("/acquisitions/9999").status_code == 404


def test_live_approve_advances_the_walk(stack) -> None:
    """With dry run off the approval reaches SUBMITTED, which is out of "needs you": the walk
    moves on. The dry-run case, which stays, is covered above."""
    client = stack.app(_policy(approval={"timid": True}))
    client.post("/requests", data={"artist": "Nine Inch Nails", "title": "The Slip"})
    assert client.get("/api/acquisitions/1").json()["state"] == "AWAITING_APPROVAL"
    # From the unfiltered queue too: the row is still listed, lower down, but it no
    # longer needs a human, so the walk moves on rather than staying on it.
    resp = client.post("/acquisitions/1/approve?walk=1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        "/queue?notice=Approved%20%231%20and%20sent%20to%20Deluge."
        "%20Nothing%20else%20in%20this%20list."
    )
    assert client.get("/api/acquisitions/1").json()["state"] == "SUBMITTED"


def test_flat_layout_lands_in_the_pool_and_skips_directory_adoption(stack) -> None:
    """`deluge.layout: flat`: the torrent goes straight into the roots beside every other
    download, and a stranger already sitting in the pool is not mistaken for ours."""
    policy = _policy(
        deluge={
            "instances": {"dev": {"url": DELUGE, "indexers": ["PandaCD"]}},
            "pending_root": "/downloads/pending",
            "completed_root": "/downloads/complete",
            "layout": "flat",
        }
    )
    client = stack.app(policy)
    stack.deluge.torrents["e" * 40] = {  # someone else's, already in the pool
        "name": "someone-elses-album",
        "state": "Seeding",
        "progress": 100.0,
        "is_finished": True,
        "save_path": "/downloads/pending",
        "move_completed_path": "/downloads/complete",
        "move_completed": True,
        "total_done": 10,
        "total_wanted": 10,
        "num_seeds": 1,
        "num_peers": 0,
        "download_payload_rate": 0,
        "label": "",
    }
    body = _request(client)
    assert body["state"] == "SUBMITTED", body["events"]
    (attempt,) = body["attempts"]
    assert attempt["infohash"] == INFOHASH
    assert attempt["download_location"] == "/downloads/pending"
    assert attempt["completed_location"] == "/downloads/complete"
    (added,) = stack.deluge.added
    assert added["options"]["download_location"] == "/downloads/pending"
    assert added["options"]["move_completed_path"] == "/downloads/complete"
    assert not any("not adopted" in e["message"] for e in body["events"])
