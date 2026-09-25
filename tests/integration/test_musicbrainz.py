from __future__ import annotations

import httpx
import pytest
import respx

from hermes.integrations.musicbrainz import (
    DEFAULT_BASE_URL,
    MusicBrainzClient,
    NotFound,
    RateLimiter,
    lucene_escape,
    lucene_terms,
)
from tests.fixtures import load

pytestmark = pytest.mark.respx(assert_all_called=False)

DUMMY_RG = "48140466-cff6-3222-bd55-63c27e43190d"


def _client() -> MusicBrainzClient:
    return MusicBrainzClient("test@example.com", min_interval=0.0, busy_retries=2)


async def test_search_parses_release_groups(respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/").respond(
        json=load("musicbrainz/search_dummy")
    )
    client = _client()
    try:
        results = await client.search_release_groups("Portishead", "Dummy")
    finally:
        await client.aclose()
    request = route.calls.last.request
    assert (
        request.headers["User-Agent"].startswith("hermes/")
        and "test@example.com" in request.headers["User-Agent"]
    )
    assert request.url.params["query"] == 'artist:"Portishead" AND releasegroup:"Dummy"'
    assert request.url.params["fmt"] == "json"
    assert [r.id for r in results][:1] == [DUMMY_RG]
    first = results[0]
    assert first.artist_name == "Portishead" and first.primary_type == "Album"
    assert first.score == 100 and first.first_release_year == 1994
    assert len(first.releases) == 20 and first.releases[0].status == "Official"
    assert results[1].secondary_types == ["Compilation"]


async def test_lookup_release_group_and_release(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/{DUMMY_RG}").respond(
        json=load("musicbrainz/rg_dummy")
    )
    respx_mock.get(f"{DEFAULT_BASE_URL}/release/76df3287-6cda-33eb-8e9a-044b5e15ffdd").respond(
        json=load("musicbrainz/release_dummy")
    )
    client = _client()
    try:
        rg = await client.release_group(DUMMY_RG)
        rel = await client.release("76df3287-6cda-33eb-8e9a-044b5e15ffdd")
    finally:
        await client.aclose()
    assert (
        rg.title == "Dummy"
        and rg.releases[0].date == "1994-08-22"
        and rg.releases[0].country == "XE"
    )
    assert rel.release_group.id == DUMMY_RG and rel.artist_credits[0].mbid
    # Genres ride along on the lookup: most votes first, ties by name.
    assert [(g.name, g.count) for g in rg.genres[:4]] == [
        ("trip hop", 12),
        ("electronic", 5),
        ("downtempo", 4),
        ("alternative rock", 1),
    ]


async def test_not_found_raises(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/nope").respond(
        404, json=load("musicbrainz/rg_missing")
    )
    client = _client()
    try:
        with pytest.raises(NotFound):
            await client.release_group("nope")
    finally:
        await client.aclose()


async def test_busy_is_retried(respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes.integrations.musicbrainz as mb

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(mb.asyncio, "sleep", no_sleep)
    route = respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/")
    route.side_effect = [
        httpx.Response(503, json=load("musicbrainz/error_busy")),
        httpx.Response(200, json=load("musicbrainz/search_nothing")),
    ]
    client = _client()
    try:
        assert await client.search_release_groups("x", "y") == []
    finally:
        await client.aclose()
    assert route.call_count == 2


async def test_timeout_is_retried_like_busy(
    respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes.integrations.musicbrainz as mb

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(mb.asyncio, "sleep", no_sleep)
    route = respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/")
    route.side_effect = [
        httpx.ReadTimeout("stalled"),
        httpx.Response(200, json=load("musicbrainz/search_nothing")),
    ]
    client = _client()
    try:
        assert await client.search_release_groups("x", "y") == []
    finally:
        await client.aclose()
    assert route.call_count == 2
    # A gateway error (seen live: 502 Bad Gateway on a release lookup) is retried too.
    route.side_effect = [
        httpx.Response(502, text="<html>502 Bad Gateway</html>"),
        httpx.Response(200, json=load("musicbrainz/search_nothing")),
    ]
    client = _client()
    try:
        assert await client.search_release_groups("x", "y") == []
    finally:
        await client.aclose()
    # Persistent stalls still surface as the timeout they are.
    route.side_effect = [httpx.ReadTimeout("stalled")] * 3
    client = _client()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.search_release_groups("x", "y")
    finally:
        await client.aclose()


async def test_rate_limiter_spaces_calls() -> None:
    now = [100.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(1.0, clock=lambda: now[0], sleep=fake_sleep)
    async with limiter:
        pass
    now[0] += 0.25
    async with limiter:
        pass
    async with limiter:
        pass
    assert [round(s, 2) for s in slept] == [0.75, 1.0]


def test_lucene_escape() -> None:
    assert lucene_escape('Say "Hi" \\ Bye') == 'Say \\"Hi\\" \\\\ Bye'


async def test_loose_search_uses_bare_terms(respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{DEFAULT_BASE_URL}/release-group/").respond(
        json=load("musicbrainz/search_loose_gybe")
    )
    client = _client()
    try:
        results = await client.search_release_groups(
            "Godspeed You! Black Emperor",
            "Lift Your Skinny Fists (Like) Antennas: Heaven!",
            loose=True,
        )
    finally:
        await client.aclose()
    query = route.calls.last.request.url.params["query"]
    assert query == (
        'artist:"Godspeed You! Black Emperor" AND '
        "releasegroup:(Lift Your Skinny Fists Like Antennas Heaven)"
    )
    assert results[0].title == "Lift Yr. Skinny Fists Like Antennas to Heaven!"


def test_lucene_terms_strips_operators() -> None:
    assert lucene_terms('a+b -c !d (e) "f" g:h ~i j/k') == "a b c d e f g h i j k"
    assert lucene_terms("!!!") == ""


# -- the release to pin: counted as beets counts it --------------------------------------

_SILENT_ALARM_RG = "f3f82b80-b2c5-3151-be53-5cb5803860e0"


def _medium(n: int, *, pregap: bool = False, video: int = 0) -> dict:
    tracks = [{"recording": {"title": f"t{i}", "video": False}} for i in range(n)]
    tracks += [{"recording": {"title": f"v{i}", "video": True}} for i in range(video)]
    medium: dict = {"format": "CD", "track-count": n + video, "tracks": tracks}
    if pregap:
        medium["pregap"] = {"recording": {"title": "hidden", "video": False}}
    return medium


def test_beets_counts_a_pregap_track_and_not_a_video() -> None:
    from hermes.integrations.musicbrainz import count_beets_tracks

    assert count_beets_tracks({"media": [_medium(13)]}) == 13
    assert count_beets_tracks({"media": [_medium(13, pregap=True)]}) == 14
    assert count_beets_tracks({"media": [_medium(12), _medium(17)]}) == 29
    assert count_beets_tracks({"media": [_medium(13, video=2)]}) == 13


@respx.mock
async def test_a_release_with_a_hidden_pregap_track_is_not_pinned_for_a_rip_without_it() -> None:
    """Acquisition 69: a 13-file CD rip was pinned to the US CD, which the group's list
    shows as 13 tracks but which hides a 14th in the pregap. beets saw a missing track and
    quiet mode skipped the import. The next candidate that really has 13 is pinned."""
    from types import SimpleNamespace

    from hermes.domain.models import AlbumTarget
    from hermes.services.importer import DownloadShape, choose_search_id

    def rel(mbid: str, country: str) -> dict:
        return {
            "id": mbid,
            "title": "Silent Alarm",
            "status": "Official",
            "date": "2005-03-14" if country == "US" else "2005-02-14",
            "country": country,
            "media": [{"format": "CD", "track-count": 13}],
        }

    respx.get(f"{DEFAULT_BASE_URL}/release-group/{_SILENT_ALARM_RG}").respond(
        json={
            "id": _SILENT_ALARM_RG,
            "title": "Silent Alarm",
            "primary-type": "Album",
            "first-release-date": "2005-02-14",
            "artist-credit": [{"name": "Bloc Party", "artist": {"id": "x", "name": "Bloc Party"}}],
            "releases": [rel("us-vice", "US"), rel("gb-wichita", "GB")],
        }
    )
    us = respx.get(f"{DEFAULT_BASE_URL}/release/us-vice").respond(
        json={"media": [_medium(13, pregap=True)]}
    )
    gb = respx.get(f"{DEFAULT_BASE_URL}/release/gb-wichita").respond(json={"media": [_medium(13)]})
    mb = MusicBrainzClient("t@example.com", min_interval=0.0)
    ctx = SimpleNamespace(musicbrainz=mb)
    target = AlbumTarget(
        release_group_mbid=_SILENT_ALARM_RG, artist_name="Bloc Party", title="Silent Alarm"
    )
    shape = DownloadShape(track_count=13, layout=[13], media="CD")
    try:
        # The ranking alone prefers the US release (US before GB); the count moves it on.
        assert await choose_search_id(ctx, target, 2005, shape) == "gb-wichita"  # type: ignore[arg-type]
        assert us.called and gb.called
        # A 14-file rip of the US CD, hidden track included, keeps the US release.
        shape14 = DownloadShape(track_count=14, layout=[14], media="CD")
        assert await choose_search_id(ctx, target, 2005, shape14) == "us-vice"  # type: ignore[arg-type]
    finally:
        await mb.aclose()
