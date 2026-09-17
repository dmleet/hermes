"""Read endpoints against a real beets Library seeded through beets' own API."""

import json
import socket
import sys
import threading
import urllib.parse
import urllib.request

import pytest
from beets import config
from beets.library import Item, Library

from beetsplug.hermes import AgentServer, JobStore

DUMMY_RG = "48140466-cff6-3222-bd55-63c27e43190d"
DUMMY_RELEASE = "76df3287-6cda-33eb-8e9a-044b5e15ffdd"
OK_RG = "e9f1e0f5-3b6f-3c6d-9a51-4f6f1f9e0a11"


MUSIC: str = "/music"  # set per test by the `lib` fixture


def _item(**kw):
    base = dict(
        title="t",
        artist="Portishead",
        albumartist="Portishead",
        album="Dummy",
        mb_releasegroupid=DUMMY_RG,
        mb_albumid=DUMMY_RELEASE,
        year=1994,
        format="FLAC",
        bitdepth=16,
        samplerate=44100,
        length=1.0,
    )
    base.update(kw)
    item = Item(**base)
    ext = base["format"].lower()
    item.path = f"{MUSIC}/{base['albumartist']}/{base['album']}/{base['title']}.{ext}".encode()
    return item


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("BEETSDIR", str(tmp_path))
    config.clear()
    config.read(user=False, defaults=True)
    # The items live under the library directory, so beets >= 2.14 stores their paths
    # relative to it; the agent must bind the music dir on its worker threads to hand back
    # absolute paths (the cluster returned "Tool/Fear Inoculum" before it did).
    global MUSIC
    MUSIC = str(tmp_path / "music")
    lib = Library(str(tmp_path / "library.db"), directory=MUSIC)
    # Dummy: 16-bit FLAC, two tracks.
    lib.add_album([_item(title="Mysterons"), _item(title="Sour Times")])
    # A mixed-quality album with a slash in the artist name and a flexible field set.
    ok = lib.add_album(
        [
            _item(
                title="Airbag",
                artist="Radiohead / Friends",
                albumartist="Radiohead / Friends",
                album="OK Computer: Special",
                mb_releasegroupid=OK_RG,
                mb_albumid="",
                year=1997,
                bitdepth=24,
                samplerate=96000,
            ),
            _item(
                title="Paranoid Android",
                artist="Radiohead / Friends",
                albumartist="Radiohead / Friends",
                album="OK Computer: Special",
                mb_releasegroupid=OK_RG,
                mb_albumid="",
                year=1997,
                format="MP3",
                bitdepth=0,
                samplerate=44100,
            ),
        ]
    )
    ok["hermes_acquisition"] = "42"
    ok.store()
    return lib


@pytest.fixture
def base(lib, tmp_path):
    store = JobStore(tmp_path / "jobs", [sys.executable, "-c", "pass"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = AgentServer(("127.0.0.1", port), store, lib=lib)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_release_group_lookup_with_quality(base):
    status, body = get(f"{base}/library/release-group/{DUMMY_RG}")
    assert status == 200
    (album,) = body["albums"]
    assert album["albumartist"] == "Portishead" and album["album"] == "Dummy"
    assert album["mb_albumid"] == DUMMY_RELEASE and album["year"] == 1994
    assert album["path"].replace("\\", "/") == f"{MUSIC}/Portishead/Dummy".replace("\\", "/")
    assert album["quality"] == {
        "items": 2,
        "formats": ["FLAC"],
        "min_bitdepth": 16,
        "min_samplerate": 44100,
        "lossy_items": 0,
    }
    assert album["hermes_acquisition"] is None


def test_release_lookup_and_unknown_ids(base):
    assert get(f"{base}/library/release/{DUMMY_RELEASE}")[1]["albums"][0]["album"] == "Dummy"
    assert get(f"{base}/library/release-group/not-a-real-id")[1] == {"albums": []}
    assert get(f"{base}/library/release/")[0] == 404
    assert get(f"{base}/library/nope/x")[0] == 404


def test_acquisition_flexible_field_and_mixed_quality(base):
    status, body = get(f"{base}/library/acquisition/42")
    assert status == 200
    (album,) = body["albums"]
    assert album["hermes_acquisition"] == "42"
    assert album["mb_releasegroupid"] == OK_RG and album["mb_albumid"] is None
    assert album["quality"]["formats"] == ["FLAC", "MP3"]
    assert album["quality"]["lossy_items"] == 1
    assert album["quality"]["min_bitdepth"] == 24  # the MP3 has no bit depth
    assert album["quality"]["min_samplerate"] == 44100
    assert get(f"{base}/library/acquisition/99")[1] == {"albums": []}


def test_search_is_literal_and_case_insensitive(base):
    q = urllib.parse.urlencode({"artist": "radiohead / friends", "album": "ok computer:"})
    status, body = get(f"{base}/library/search?{q}")
    assert status == 200 and [a["album"] for a in body["albums"]] == ["OK Computer: Special"]
    q = urllib.parse.urlencode({"album": "Dummy"})
    assert [a["albumartist"] for a in get(f"{base}/library/search?{q}")[1]["albums"]] == [
        "Portishead"
    ]
    assert get(f"{base}/library/search")[0] == 400
