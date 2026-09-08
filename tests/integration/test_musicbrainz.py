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
