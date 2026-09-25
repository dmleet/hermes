from __future__ import annotations

from pathlib import Path

import pytest

from hermes.bencode import BencodeError, TorrentInfo, decode, encode

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "torrents"
    / "gettysburg-audio.torrent"
)


def test_round_trip() -> None:
    value = {b"a": [1, b"x", {b"n": -3}], b"s": b"", b"z": 0}
    assert decode(encode(value)) == value
    assert encode({"k": "v"}) == b"d1:k1:ve"


def test_fixture_torrent_infohash_and_layout() -> None:
    info = TorrentInfo(FIXTURE.read_bytes())
    assert info.infohash == "7838b1f9b3ab320d9e1b265dff0334bcb6238577"
    assert info.name == "gettysburg_shurtagal_librivox"
    assert info.file_count == 3 and info.single_file is False
    assert info.total_size == 3306502


@pytest.mark.parametrize("junk", [b"", b"i12", b"3:ab", b"d1:ke", b"de extra", b"le", b"x"])
def test_bad_input_raises(junk: bytes) -> None:
    with pytest.raises((BencodeError, ValueError, IndexError, KeyError)):
        TorrentInfo(junk)


def test_a_malformed_torrent_error_never_quotes_its_bytes() -> None:
    # A wrong length prefix makes int() quote the bytes that follow: here, a passkey.
    data = b"d8:announce4x:http://tracker.example/8e7d6c5b4a39281706aabbccdd/announce4:infod4:name1:aee"
    with pytest.raises(BencodeError) as err:
        decode(data)
    assert "8e7d6c5b4a" not in str(err.value) and "announce" not in str(err.value)
