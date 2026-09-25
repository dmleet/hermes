"""Minimal bencode (BEP 3) decoder/encoder, enough to read a .torrent and compute its
infohash. No third-party dependency; torrent files are small."""

from __future__ import annotations

import hashlib
from typing import Any


class BencodeError(ValueError):
    pass


MAX_DEPTH = 64


def decode(data: bytes) -> Any:
    """Decode a bencoded value. Every malformed input raises BencodeError."""
    try:
        value, end = _decode(data, 0, 0)
    except (ValueError, IndexError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, BencodeError):
            raise
        # The type only: the exception's text can quote the torrent's bytes, which carry the
        # announce URL and so the account passkey, and this message reaches the database.
        raise BencodeError(f"malformed bencode ({type(exc).__name__})") from exc
    if end != len(data):
        raise BencodeError(f"trailing data after position {end}")
    return value


def _decode(b: bytes, i: int, depth: int) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise BencodeError("nesting too deep")
    if i >= len(b):
        raise BencodeError("unexpected end of data")
    c = b[i : i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1 : j]), j + 1
    if c == b"l":
        i += 1
        out: list[Any] = []
        while b[i : i + 1] != b"e":
            if i >= len(b):
                raise BencodeError("unterminated list")
            v, i = _decode(b, i, depth + 1)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        d: dict[bytes, Any] = {}
        while b[i : i + 1] != b"e":
            if i >= len(b):
                raise BencodeError("unterminated dictionary")
            k, i = _decode(b, i, depth + 1)
            if not isinstance(k, bytes):
                raise BencodeError("dictionary key is not a string")
            v, i = _decode(b, i, depth + 1)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        j = b.index(b":", i)
        n = int(b[i:j])
        start = j + 1
        if n < 0 or start + n > len(b):
            raise BencodeError("string length runs past the end of data")
        return b[start : start + n], start + n
    raise BencodeError(f"invalid token {c!r} at {i}")


def encode(value: Any) -> bytes:
    if isinstance(value, bool):
        raise BencodeError("bencode has no booleans")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return encode(value.encode("utf-8"))
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k if isinstance(k, bytes) else k.encode(), v) for k, v in value.items())
        return b"d" + b"".join(encode(k) + encode(v) for k, v in items) + b"e"
    raise BencodeError(f"cannot bencode {type(value).__name__}")


AUDIO_EXTENSIONS = frozenset(
    {
        "flac",
        "mp3",
        "m4a",
        "aac",
        "ogg",
        "oga",
        "opus",
        "wav",
        "aif",
        "aiff",
        "ape",
        "wv",
        "dsf",
        "alac",
    }
)


class TorrentInfo:
    def __init__(self, data: bytes) -> None:
        meta = decode(data)
        if not isinstance(meta, dict) or not isinstance(meta.get(b"info"), dict):
            raise BencodeError("not a torrent file (no info dictionary)")
        self.raw = data
        self.meta = meta
        info = meta[b"info"]
        try:
            self.infohash = hashlib.sha1(encode(info)).hexdigest()
            name = info.get(b"name", b"")
            self.name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
            files = info.get(b"files")
            if files:
                self.total_size = sum(int(f[b"length"]) for f in files)
                self.file_count = len(files)
                self.paths = [
                    "/".join(p.decode("utf-8", "replace") for p in f.get(b"path", []))
                    for f in files
                ]
                self.sizes = [int(f[b"length"]) for f in files]
            else:
                self.total_size = int(info.get(b"length", 0))
                self.file_count = 1
                self.paths = [self.name]
                self.sizes = [self.total_size]
            self.single_file = files is None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BencodeError(f"malformed torrent info ({type(exc).__name__})") from exc

    @property
    def audio_file_count(self) -> int:
        """How many tracks the download holds, by file extension (logs, cue sheets and
        artwork excluded): the number a MusicBrainz release should agree with."""
        return sum(self.audio_layout())

    def audio_layout(self) -> list[int]:
        """Audio files per directory, in directory order: a two-disc rip in CD1/ and CD2/
        reads [18, 18], which is the shape of a two-medium release."""
        per_dir: dict[str, int] = {}
        for p in self.paths:
            if p.rsplit(".", 1)[-1].lower() in AUDIO_EXTENSIONS:
                folder = p.rsplit("/", 1)[0] if "/" in p else ""
                per_dir[folder] = per_dir.get(folder, 0) + 1
        return [per_dir[k] for k in sorted(per_dir)]

    def audio_sizes(self) -> list[int]:
        """Sizes of the audio files in path order (a track listing's order)."""
        return [
            size
            for p, size in sorted(zip(self.paths, self.sizes, strict=True))
            if p.rsplit(".", 1)[-1].lower() in AUDIO_EXTENSIONS
        ]

    @property
    def looks_like_cd_rip(self) -> bool:
        """A ripper's log or a cue sheet travels with CD rips and nothing else."""
        return any(p.rsplit(".", 1)[-1].lower() in ("log", "cue") for p in self.paths)
