from __future__ import annotations

import httpx
import pytest
import respx

from hermes.integrations.beets import BeetsClient

BASE = "http://beets.test:8338"
ALBUM = {
    "id": 7,
    "mb_albumid": "76df3287-6cda-33eb-8e9a-044b5e15ffdd",
    "mb_releasegroupid": "48140466-cff6-3222-bd55-63c27e43190d",
    "albumartist": "Portishead",
    "album": "Dummy",
    "year": 1994,
    "original_year": 1994,
    "path": "/music/Portishead/Dummy",
    "added": 1700000000.0,
    "hermes_acquisition": None,
    "quality": {
        "items": 11,
        "formats": ["FLAC"],
        "min_bitdepth": 16,
        "min_samplerate": 44100,
        "lossy_items": 0,
    },
}

pytestmark = pytest.mark.respx(assert_all_called=False)


async def test_release_group_parses_typed_albums(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{BASE}/library/release-group/{ALBUM['mb_releasegroupid']}").respond(
        json={"albums": [ALBUM]}
    )
    client = BeetsClient(BASE)
    try:
        (album,) = await client.albums_in_release_group(ALBUM["mb_releasegroupid"])
    finally:
        await client.aclose()
    assert album.album == "Dummy" and album.quality.lossless is True
    assert album.quality.min_bitdepth == 16


async def test_search_sends_only_given_params(respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{BASE}/library/search", params={"artist": "Portishead"}).respond(
        json={"albums": []}
    )
    client = BeetsClient(BASE)
    try:
        assert await client.search(artist="Portishead") == []
    finally:
        await client.aclose()
    assert route.called and "album" not in str(route.calls.last.request.url)


async def test_locked_library_is_retried_then_succeeds(respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{BASE}/library/acquisition/42")
    route.side_effect = [
        httpx.Response(503, json={"error": "database is locked", "retry": True}),
        httpx.Response(200, json={"albums": [{**ALBUM, "hermes_acquisition": "42"}]}),
    ]
    client = BeetsClient(BASE, retry_delay=0.0)
    try:
        (album,) = await client.albums_for_acquisition(42)
    finally:
        await client.aclose()
    assert album.hermes_acquisition == "42" and route.call_count == 2


async def test_non_retryable_error_raises(respx_mock: respx.Router) -> None:
    respx_mock.get(f"{BASE}/library/release/x").respond(500, json={"error": "boom"})
    client = BeetsClient(BASE, retry_delay=0.0)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.albums_for_release("x")
    finally:
        await client.aclose()
