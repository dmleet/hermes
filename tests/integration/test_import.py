"""M4: READY_FOR_BEETS -> IMPORTING -> IMPORTED | IMPORT_NEEDS_REVIEW through the fake agent,
with the download produced by the M3 flow against the fake Deluge."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hermes.app import Clients, create_app
from hermes.config import Policy, Settings
from hermes.domain.models import (
    Acquisition,
    AlbumTarget,
    Event,
    GrabAttempt,
    GrabOutcome,
    utcnow,
)
from hermes.domain.state import AcquisitionState as S
from hermes.integrations.musicbrainz import DEFAULT_BASE_URL as MB
from hermes.integrations.musicbrainz import MusicBrainzClient, parse_release_group
from hermes.integrations.navidrome import NavidromeClient
from hermes.services import importer, observer
from hermes.services.importer import DownloadShape, download_shape, pick_release
from tests.fakes import FakeBeetsAgent, FakeDeluge
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

BEETS = "http://beets.test:8338"
PROWLARR = "http://prowlarr.test"
DELUGE = "http://deluge-dev.test"
NAVIDROME = "http://navidrome.test"
SLIP_RG = "5990b23f-6412-3eaf-989c-a4831f4f95f7"
TORRENT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "torrents"
    / "gettysburg-audio.torrent"
)
INFOHASH = "7838b1f9b3ab320d9e1b265dff0334bcb6238577"
IMPORTED = {
    "id": 9,
    "mb_albumid": "d3252f73-5a42-4ab7-b20a-4d65debd8b10",
    "mb_releasegroupid": SLIP_RG,
    "albumartist": "Nine Inch Nails",
    "album": "The Slip",
    "year": 2008,
    "path": "/music/Nine Inch Nails/The Slip",
    "quality": {
        "items": 10,
        "formats": ["FLAC"],
        "min_bitdepth": 16,
        "min_samplerate": 44100,
        "lossy_items": 0,
    },
}


def _policy() -> Policy:
    return Policy.model_validate(
        {
            "dry_run": False,
            "approval": {"timid": False},
            "beets": {"agent_url": BEETS, "import_timeout_seconds": 600},
            "quality": {"max_sample_rate_khz": None},
            "deluge": {"instances": {"dev": {"url": DELUGE, "indexers": ["PandaCD"]}}},
            "paths": {"deluge_root": "/downloads/complete", "beets_root": "/downloads"},
        }
    )


def _slip_rg() -> dict:
    return {
        "id": SLIP_RG,
        "title": "The Slip",
        "primary-type": "Album",
        "first-release-date": "2008-05-05",
        "artist-credit": [
            {"name": "Nine Inch Nails", "artist": {"id": "x", "name": "Nine Inch Nails"}}
        ],
        "releases": [
            {
                "id": "rel-promo",
                "title": "The Slip",
                "status": "Promotion",
                "date": "2008-05-05",
                "country": "US",
            },
            {
                "id": "rel-jp-2009",
                "title": "The Slip",
                "status": "Official",
                "date": "2009-01-01",
                "country": "JP",
            },
            {
                "id": "rel-xw-2008",
                "title": "The Slip",
                "status": "Official",
                "date": "2008-05-05",
                "country": "XW",
            },
            {
                "id": "rel-us-2008",
                "title": "The Slip",
                "status": "Official",
                "date": "2008-07-22",
                "country": "US",
            },
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
            respx_mock.get(f"{MB}/release-group/").respond(
                json={"count": 1, "release-groups": [{**_slip_rg(), "score": 100}]}
            )
            respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(json=_slip_rg())
            respx_mock.get(f"{PROWLARR}/api/v1/search").respond(
                json=load("prowlarr/search_pandacd_nin")
            )
            respx_mock.get(url__regex=r"http://localhost:9696/1/download.*").respond(
                content=TORRENT.read_bytes()
            )
            self.scan_calls = respx_mock.get(f"{NAVIDROME}/rest/startScan").respond(
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "scanStatus": {"scanning": True, "count": 0},
                    }
                }
            )
            self.scanning = False  # what getScanStatus answers
            self.navidrome_down = False
            respx_mock.get(f"{NAVIDROME}/rest/getScanStatus").mock(side_effect=self._scan_status)
            command.upgrade(alembic_config(settings), "head")
            policy = _policy()
            clients = Clients.from_config(settings, policy)
            clients.musicbrainz = MusicBrainzClient("t@example.com", min_interval=0.0)
            clients.navidrome = NavidromeClient(NAVIDROME, "user", "pw")
            app = create_app(settings=settings, policy=policy, clients=clients, scheduler=False)
            self.client = TestClient(app)
            self.client.__enter__()

        async def download(self) -> int:
            """Request The Slip, let the fake Deluge finish it, observe to READY_FOR_BEETS."""
            body = self.client.post(
                "/api/requests", json={"artist": "Nine Inch Nails", "title": "The Slip"}
            ).json()
            assert body["state"] == "SUBMITTED", body["events"]
            self.deluge.finish(INFOHASH)
            await self.observe()
            assert (
                self.client.get(f"/api/acquisitions/{body['id']}").json()["state"]
                == "READY_FOR_BEETS"
            )
            return int(body["id"])

        def _scan_status(self, request: httpx.Request) -> httpx.Response:
            if self.navidrome_down:
                return httpx.Response(502)
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "scanStatus": {"scanning": self.scanning, "count": 12},
                    }
                },
            )

        def ready_twin(self, acq_id: int) -> int:
            """A second acquisition of the same target whose download has also finished:
            the batch case, without a second torrent fixture."""
            app = self.client.app
            with app.state.session_factory() as session:
                first = session.get(Acquisition, acq_id)
                assert first is not None
                attempt = first.grab_attempts[0]
                target = first.album_target
                assert target is not None
                twin = Acquisition(
                    state=S.READY_FOR_BEETS,
                    origin="manual",
                    # One acquisition per target; a pinned release spares a MusicBrainz call.
                    album_target=AlbumTarget(
                        release_group_mbid="twin-" + target.release_group_mbid[5:],
                        artist_name=target.artist_name,
                        title=target.title + " (twin)",
                        preferred_release_mbid="rel-xw-2008",
                    ),
                )
                session.add(twin)
                session.add(
                    GrabAttempt(
                        acquisition=twin,
                        candidate_id=attempt.candidate_id,
                        deluge_instance=attempt.deluge_instance,
                        infohash="1" * 40,
                        download_location=attempt.download_location,
                        completed_location=attempt.completed_location,
                        completed_path=attempt.completed_path + "-twin",
                        completed_at=utcnow(),
                        outcome=GrabOutcome.COMPLETED,
                    )
                )
                session.commit()
                return int(twin.id)

        async def observe(self) -> dict:
            app = self.client.app
            with app.state.session_factory() as session:
                return await observer.tick(session, app.state.context)

        async def import_tick(self, *, hours_later: float = 0) -> dict:
            app = self.client.app
            with app.state.session_factory() as session:
                return await importer.tick(
                    session, app.state.context, now=utcnow() + timedelta(hours=hours_later)
                )

        def get(self, acq_id: int) -> dict:
            return self.client.get(f"/api/acquisitions/{acq_id}").json()

        def messages(self, acq_id: int) -> list[str]:
            return [e["message"] for e in self.get(acq_id)["events"]]

    s = Stack()
    yield s
    s.client.__exit__(None, None, None)


def test_pick_release_prefers_the_downloads_track_count() -> None:
    """Seen live: the Ghosts I-IV group holds a 9-track 'Ghosts I' and the 36-track album,
    both official, 2008, worldwide; the 9-track one won the tie and beets skipped 36 files."""
    raw = _slip_rg()
    raw["releases"] = [
        {
            "id": "rel-ghosts-i",
            "title": "Ghosts I",
            "status": "Official",
            "date": "2008-03-02",
            "country": "XW",
            "media": [{"format": "Digital Media", "track-count": 9}],
        },
        {
            "id": "rel-ghosts-i-iv",
            "title": "Ghosts I-IV",
            "status": "Official",
            "date": "2008-03-02",
            "country": "XW",
            "media": [{"format": "Digital Media", "track-count": 36}],
        },
        {
            "id": "rel-hidden",
            "title": "Ghosts I-IV",
            "status": "Official",
            "date": "2008-04-08",
            "country": "XW",
            "disambiguation": "incl. hidden multitracks",
            "media": [{"format": "Digital Media", "track-count": 38}],
        },
    ]
    raw["releases"].append(
        {
            "id": "rel-us-cd",
            "title": "Ghosts I-IV",
            "status": "Official",
            "date": "2008-04-08",
            "country": "US",
            "media": [{"format": "CD", "track-count": 18}, {"format": "CD", "track-count": 18}],
        }
    )
    rg = parse_release_group(raw)
    assert [r.track_count for r in rg.releases] == [9, 36, 38, 36]
    assert rg.releases[3].media_track_counts == [18, 18] and rg.releases[3].formats == ["CD"]
    shape = DownloadShape(track_count=36)
    assert pick_release(rg, 2008, shape) == "rel-ghosts-i-iv"  # count first, then XW
    assert pick_release(rg, 2008, DownloadShape(track_count=9)) == "rel-ghosts-i"
    assert pick_release(rg, 2008, None) == "rel-ghosts-i"  # unknown shape: earlier rules
    assert pick_release(rg, 2008, DownloadShape(track_count=50)) == "rel-ghosts-i"  # plain wins
    # Two directories of 18 with a rip log: the two-CD release, not the 36-track download.
    # (Seen live: beets scored the digital release 0.093 against these files and skipped;
    # the US CD scored 0.017.)
    shape = DownloadShape(track_count=36, layout=[18, 18], media="CD")
    assert pick_release(rg, 2008, shape) == "rel-us-cd"
    assert pick_release(rg, 2008, DownloadShape(track_count=36, layout=[18, 18])) == "rel-us-cd"
    assert pick_release(rg, 2008, DownloadShape(track_count=36, media="WEB")) == "rel-ghosts-i-iv"


def test_download_shape_comes_from_the_attempt_not_a_file() -> None:
    from hermes.domain.models import GrabAttempt

    attempt = GrabAttempt(download_shape={"audio_files": 36, "layout": [18, 18], "cd_rip": True})
    shape = download_shape(attempt, None)
    assert (shape.track_count, shape.layout, shape.media) == (36, [18, 18], "CD")
    assert download_shape(attempt, "WEB").media == "WEB"  # the title's word wins
    assert download_shape(GrabAttempt(download_shape={}), None) == DownloadShape()


def test_torrent_audio_file_count_ignores_logs_and_art() -> None:
    from hermes.bencode import TorrentInfo, encode

    info = {
        b"name": b"Album",
        b"piece length": 16384,
        b"pieces": b"",
        b"files": [
            {b"length": 1, b"path": [b"CD1", b"01.flac"]},
            {b"length": 1, b"path": [b"CD1", b"02.FLAC"]},
            {b"length": 1, b"path": [b"CD1", b"rip.log"]},
            {b"length": 1, b"path": [b"cover.jpg"]},
            {b"length": 1, b"path": [b"album.cue"]},
            {b"length": 1, b"path": [b"CD2", b"01.flac"]},
        ],
    }
    torrent = TorrentInfo(encode({b"info": info}))
    assert torrent.audio_file_count == 3
    assert torrent.audio_layout() == [2, 1] and torrent.looks_like_cd_rip
    single = {b"name": b"a.mp3", b"length": 1, b"piece length": 16384, b"pieces": b""}
    assert TorrentInfo(encode({b"info": single})).audio_layout() == [1]


def test_pick_release_prefers_official_matching_year_and_worldwide() -> None:
    rg = parse_release_group(_slip_rg())
    assert pick_release(rg, 2008) == "rel-xw-2008"
    assert pick_release(rg, 2009) == "rel-jp-2009"
    assert pick_release(rg, None) == "rel-xw-2008"  # official + worldwide beats promo
    rg.releases = []
    assert pick_release(rg, 2008) is None


async def test_import_end_to_end(stack) -> None:
    acq_id = await stack.download()
    assert (await stack.import_tick()) == {"started": 1}
    body = stack.get(acq_id)
    assert body["state"] == "IMPORTING"
    (submitted,) = stack.agent.submitted
    assert submitted["path"] == f"/downloads/hermes/{acq_id}/gettysburg_shurtagal_librivox"
    assert submitted["acquisition_id"] == str(acq_id)
    assert submitted["search_id"] == "rel-xw-2008"
    target = stack.get(acq_id)["target"]
    assert submitted["release_group_id"] == target["release_group_mbid"]
    job_id = body["attempts"][0]["import_job_id"]
    assert job_id == "job1"

    assert (await stack.import_tick()) == {"polled": 1}  # still running
    assert stack.get(acq_id)["state"] == "IMPORTING"

    stack.agent.finish(job_id, imported_album=IMPORTED)
    assert (await stack.import_tick()) == {"polled": 1, "scan_requested": 1}
    body = stack.get(acq_id)
    assert body["state"] == "IMPORTED"
    assert body["target"]["library_status"] == "owned"
    messages = [e["message"] for e in body["events"]]
    assert any(m.startswith("imported: Nine Inch Nails - The Slip (10 items)") for m in messages)
    # The folder is recorded in beets' incremental history so a manual import skips it.
    assert stack.agent.history == [f"/downloads/hermes/{acq_id}/gettysburg_shurtagal_librivox"]
    assert any(m.startswith("recorded in beets' incremental history") for m in messages)
    assert messages[-1] == "Navidrome scan requested" and stack.scan_calls.called
    assert (await stack.import_tick()) == {}


async def test_quiet_skip_needs_review_then_retry(stack) -> None:
    acq_id = await stack.download()
    await stack.import_tick()
    stack.agent.finish("job1", exit_code=0)  # beets ran but skipped (no album carries the id)
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    review = body["events"][-1]
    assert "no album carries this acquisition" in review["message"]
    assert review["data"]["log_tail"] == ["import log line"]

    body = stack.client.post(f"/api/acquisitions/{acq_id}/retry-import").json()
    assert body["state"] == "IMPORTING" and body["attempts"][0]["import_job_id"] == "job2"
    stack.agent.finish("job2", imported_album=IMPORTED)
    await stack.import_tick()
    assert stack.get(acq_id)["state"] == "IMPORTED"


async def test_refused_import_needs_review_then_retry(stack) -> None:
    """The agent refused: the files' MusicBrainz tags name another release group. Nothing
    was imported, so the review says why and does not ask to check a library entry."""
    acq_id = await stack.download()
    await stack.import_tick()
    stack.agent.refuse("job1", "the files are tagged as MusicBrainz release group rg-other")
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    review = body["events"][-1]
    assert review["message"] == (
        "beets import refused: the files are tagged as MusicBrainz release group rg-other; "
        "if these are the right files, import them by hand"
    )
    assert review["level"] == "warning"

    body = stack.client.post(f"/api/acquisitions/{acq_id}/retry-import").json()
    assert body["state"] == "IMPORTING"
    stack.agent.finish("job2", imported_album=IMPORTED)
    await stack.import_tick()
    assert stack.get(acq_id)["state"] == "IMPORTED"


async def test_import_timeout_and_lost_job(stack) -> None:
    acq_id = await stack.download()
    await stack.import_tick()
    await stack.import_tick(hours_later=1)
    # Queued behind other imports on the agent's single worker: not late, however long.
    assert stack.get(acq_id)["state"] == "IMPORTING"
    stack.agent.start("job1")
    await stack.import_tick(hours_later=1)  # running: the 600 s timeout counts from here
    body = stack.get(acq_id)
    assert (
        body["state"] == "IMPORT_NEEDS_REVIEW" and "still running" in body["events"][-1]["message"]
    )

    # A retry whose job vanishes (agent restarted) goes back to review.
    stack.client.post(f"/api/acquisitions/{acq_id}/retry-import")
    del stack.agent.jobs["job2"]
    await stack.import_tick()
    body = stack.get(acq_id)
    assert (
        body["state"] == "IMPORT_NEEDS_REVIEW"
        and "no longer knows job" in body["events"][-1]["message"]
    )


async def test_unsafe_config_and_invisible_path(stack) -> None:
    acq_id = await stack.download()
    stack.agent.config_problems = ["import.copy is false"]
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "READY_FOR_BEETS"  # health said not ready: wait, do not fail
    assert body["events"][-1]["message"].startswith("beets agent not ready")
    await stack.import_tick()
    assert len([e for e in stack.get(acq_id)["events"] if "not ready" in e["message"]]) == 1

    stack.agent.config_problems = []
    stack.agent.missing_paths.add(f"/downloads/hermes/{acq_id}/gettysburg_shurtagal_librivox")
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "IMPORT_NEEDS_REVIEW" and "cannot see" in body["events"][-1]["message"]


async def test_agent_unreachable_waits(stack, respx_mock: respx.Router) -> None:
    acq_id = await stack.download()
    respx_mock.get(f"{BEETS}/healthz").mock(side_effect=httpx.ConnectError("down"))
    assert (await stack.import_tick()) == {"not_started": 1}
    assert stack.get(acq_id)["state"] == "READY_FOR_BEETS"


def test_navidrome_client_auth_and_errors(respx_mock: respx.Router) -> None:
    import asyncio

    ping = respx_mock.get(f"{NAVIDROME}/rest/ping").respond(
        json={"subsonic-response": {"status": "ok", "serverVersion": "0.57.0", "type": "navidrome"}}
    )
    respx_mock.get(f"{NAVIDROME}/rest/startScan").respond(
        json={
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 40, "message": "Wrong username or password"},
            }
        }
    )
    client = NavidromeClient(NAVIDROME, "user", "pw")

    async def run() -> None:
        try:
            health = await client.health()
            assert health.ok and health.data["server_version"] == "0.57.0"
            params = ping.calls.last.request.url.params
            assert params["u"] == "user" and len(params["t"]) == 32 and params["f"] == "json"
            with pytest.raises(Exception, match="Wrong username"):
                await client.start_scan()
        finally:
            await client.aclose()

    asyncio.run(run())


async def test_history_marking_failure_is_a_warning_not_a_failure(stack) -> None:
    """The import is done once verified; not being able to record it for the user's manual
    runs is worth a warning event, nothing more."""
    acq_id = await stack.download()
    assert (await stack.import_tick()) == {"started": 1}
    stack.agent.history_down = True
    stack.agent.finish("job1", imported_album=IMPORTED)
    assert (await stack.import_tick()) == {"polled": 1, "scan_requested": 1}
    body = stack.get(acq_id)
    assert body["state"] == "IMPORTED"
    warning = [e for e in body["events"] if e["message"].startswith("could not record")]
    assert warning and warning[0]["level"] == "warning"
    assert stack.agent.history == []


async def test_scan_waits_for_the_import_queue_to_drain(stack) -> None:
    """Two downloads back to back: the scan for the first would walk the second's folder
    while beets is still writing it (a track read mid-scrub has no tags, and with source
    mtimes preserved Navidrome never re-reads it). One scan, once nothing is importing."""
    first = await stack.download()
    assert (await stack.import_tick()) == {"started": 1}
    second = stack.ready_twin(first)

    stack.agent.finish("job1", imported_album=IMPORTED)
    counts = await stack.import_tick()
    assert counts == {"polled": 1, "started": 1, "scan_deferred": 1}
    assert stack.get(first)["state"] == "IMPORTED"
    assert stack.get(second)["state"] == "IMPORTING"
    assert not stack.scan_calls.called
    assert not any(m.startswith("Navidrome") for m in stack.messages(first))

    # Still importing: still no scan.
    assert (await stack.import_tick()) == {"polled": 1, "scan_deferred": 1}
    assert not stack.scan_calls.called

    twin_group = stack.get(second)["target"]["release_group_mbid"]
    stack.agent.finish("job2", imported_album={**IMPORTED, "mb_releasegroupid": twin_group})
    assert (await stack.import_tick()) == {"polled": 1, "scan_requested": 1}
    assert stack.scan_calls.call_count == 1
    for acq_id in (first, second):
        assert stack.messages(acq_id)[-1] == "Navidrome scan requested for 2 imports"
    assert (await stack.import_tick()) == {}
    assert stack.scan_calls.call_count == 1


async def test_import_waits_while_navidrome_is_scanning(stack) -> None:
    """The other half of the gate: a scan in progress (the nightly one, a manual one) holds
    a ready import for a tick rather than starting beets under it."""
    acq_id = await stack.download()
    stack.scanning = True
    assert (await stack.import_tick()) == {"waiting_for_scan": 1}
    assert stack.get(acq_id)["state"] == "READY_FOR_BEETS"
    assert stack.agent.submitted == []
    assert (await stack.import_tick()) == {"waiting_for_scan": 1}
    waits = [m for m in stack.messages(acq_id) if m.startswith("Navidrome is scanning")]
    assert len(waits) == 1  # noted once, not once per tick

    stack.scanning = False
    assert (await stack.import_tick()) == {"started": 1}
    assert stack.get(acq_id)["state"] == "IMPORTING"

    # A human's retry meets the same gate.
    stack.agent.finish("job1", exit_code=1)
    await stack.import_tick()
    assert stack.get(acq_id)["state"] == "IMPORT_NEEDS_REVIEW"
    stack.scanning = True
    resp = stack.client.post(f"/api/acquisitions/{acq_id}/retry-import")
    assert resp.status_code == 409 and "scanning" in resp.json()["detail"]
    assert stack.get(acq_id)["state"] == "IMPORT_NEEDS_REVIEW"
    stack.scanning = False
    assert stack.client.post(f"/api/acquisitions/{acq_id}/retry-import").status_code == 200
    assert stack.get(acq_id)["state"] == "IMPORTING"


async def test_navidrome_being_down_never_holds_an_import(stack, respx_mock: respx.Router) -> None:
    """Navidrome is optional: no answer to the status question means go ahead, and a failed
    scan request is a warning on each import it covered, not a retry every tick."""
    acq_id = await stack.download()
    stack.navidrome_down = True
    assert (await stack.import_tick()) == {"started": 1}

    respx_mock.get(f"{NAVIDROME}/rest/startScan").respond(status_code=502)
    stack.agent.finish("job1", imported_album=IMPORTED)
    assert (await stack.import_tick()) == {"polled": 1, "scan_failed": 1}
    body = stack.get(acq_id)
    assert body["state"] == "IMPORTED"
    warning = [e for e in body["events"] if e["message"].startswith("Navidrome scan request")]
    assert len(warning) == 1 and warning[0]["level"] == "warning"
    assert "502" in warning[0]["message"] and "t=" not in warning[0]["message"]
    assert (await stack.import_tick()) == {}  # not owed any more


async def test_restart_still_owes_the_scan(stack) -> None:
    """Hermes restarted between an import and the drain: the first tick of the new process
    reads what is owed from the events (imports after the last scan request)."""
    acq_id = await stack.download()
    await stack.import_tick()
    stack.agent.finish("job1", imported_album=IMPORTED)
    assert (await stack.import_tick()) == {"polled": 1, "scan_requested": 1}
    ctx = stack.client.app.state.context

    # A fresh process, nothing new since the scan: nothing to ask for.
    ctx.navidrome_scan_owed = None
    assert (await stack.import_tick()) == {}
    assert stack.scan_calls.call_count == 1

    # The same, but the scan request never happened (the process died first).
    with stack.client.app.state.session_factory() as session:
        for event in session.query(Event).filter(Event.message.like("Navidrome scan%")):
            session.delete(event)
        session.commit()
    ctx.navidrome_scan_owed = None
    assert (await stack.import_tick()) == {"scan_requested": 1}
    assert stack.scan_calls.call_count == 2
    assert stack.messages(acq_id)[-1] == "Navidrome scan requested"


async def test_musicbrainz_down_waits_instead_of_importing_unpinned(
    stack, respx_mock: respx.Router
) -> None:
    acq_id = await stack.download()
    respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(503)
    await stack.import_tick()
    await stack.import_tick()
    assert stack.get(acq_id)["state"] == "READY_FOR_BEETS"
    assert stack.agent.submitted == []
    waits = [m for m in stack.messages(acq_id) if m.startswith("MusicBrainz unavailable")]
    assert len(waits) == 1

    respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(json=_slip_rg())
    assert (await stack.import_tick()) == {"started": 1}
    assert stack.agent.submitted[0]["search_id"]


async def test_a_group_musicbrainz_no_longer_knows_goes_to_a_human(
    stack, respx_mock: respx.Router
) -> None:
    acq_id = await stack.download()
    respx_mock.get(f"{MB}/release-group/{SLIP_RG}").respond(404)
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    assert "no release to pin" in body["events"][-1]["message"]
    assert stack.agent.submitted == []


async def test_an_album_from_another_release_group_is_not_called_imported(stack) -> None:
    acq_id = await stack.download()
    await stack.import_tick()
    stack.agent.finish("job1", imported_album={**IMPORTED, "mb_releasegroupid": "rg-other"})
    await stack.import_tick()
    body = stack.get(acq_id)
    assert body["state"] == "IMPORT_NEEDS_REVIEW"
    assert "from release group rg-other" in body["events"][-1]["message"]
    assert body["target"]["library_status"] != "owned"


async def test_a_scan_that_never_ends_holds_imports_only_so_long(stack) -> None:
    acq_id = await stack.download()
    stack.scanning = True
    assert (await stack.import_tick()) == {"waiting_for_scan": 1}
    ctx = stack.client.app.state.context
    ctx.navidrome_scanning_since = utcnow() - importer.SCAN_HOLD_LIMIT - timedelta(minutes=1)
    assert (await stack.import_tick()) == {"started": 1}
    assert any("importing anyway" in m for m in stack.messages(acq_id))
    stack.scanning = False
    await stack.import_tick()
    assert ctx.navidrome_scanning_since is None  # a finished scan resets the clock


async def test_one_failing_row_does_not_stop_the_others(stack, monkeypatch) -> None:
    first = await stack.download()
    second = stack.ready_twin(first)
    real = importer.start_import

    async def flaky(session, ctx, acq):
        if acq.id == first:
            raise KeyError("unexpected shape")
        return await real(session, ctx, acq)

    monkeypatch.setattr(importer, "start_import", flaky)
    counts = await stack.import_tick()
    assert counts == {"errors": 1, "started": 1}
    assert stack.get(second)["state"] == "IMPORTING"
    assert any("import step failed: KeyError" in m for m in stack.messages(first))


async def test_import_review_can_be_cancelled(stack) -> None:
    acq_id = await stack.download()
    await stack.import_tick()
    stack.agent.refuse("job1", "the files are tagged as another release group")
    await stack.import_tick()
    assert stack.get(acq_id)["state"] == "IMPORT_NEEDS_REVIEW"
    resp = stack.client.post(f"/api/acquisitions/{acq_id}/cancel", json={"by": "me"})
    assert resp.status_code == 200 and resp.json()["state"] == "CANCELLED"
