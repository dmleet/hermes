#!/usr/bin/env python3
"""Build a web-seeded fixture torrent from files already on disk. Stdlib only.

Why: archive.org's own torrents include metadata files that change after the torrent is
made, so pieces touching them never verify and the torrent never finishes. A torrent that
lists only the stable audio files, with the same web-seed base URLs, downloads to
completion from archive.org with no peers at all.

Example (the committed fixture):

    python scripts/make-fixture-torrent.py \
        --source dev/downloads/pending/hermes/fixture/gettysburg_shurtagal_librivox \
        --name gettysburg_shurtagal_librivox \
        --webseed https://archive.org/download/ \
        --out tests/fixtures/torrents/gettysburg-audio.torrent \
        Gettysburg_Address_Lincoln.mp3 Gettysburg_Address_Lincoln.ogg \
        Gettysburg_Address_Lincoln_64kb.mp3
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

PIECE_LENGTH = 256 * 1024


def bencode(value: object) -> bytes:
    if isinstance(value, bool):
        raise TypeError("bencode has no booleans")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return bencode(value.encode("utf-8"))
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")


def build(source: Path, name: str, files: list[str], webseeds: list[str]) -> tuple[bytes, str]:
    entries = []
    hasher = hashlib.sha1()
    pieces = bytearray()
    buffered = 0
    for rel in files:
        path = source / rel
        entries.append({"length": path.stat().st_size, "path": rel.split("/")})
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                # Feed bytes into fixed-size pieces that span file boundaries (BEP 3).
                while chunk:
                    take = min(len(chunk), PIECE_LENGTH - buffered)
                    hasher.update(chunk[:take])
                    buffered += take
                    chunk = chunk[take:]
                    if buffered == PIECE_LENGTH:
                        pieces += hasher.digest()
                        hasher = hashlib.sha1()
                        buffered = 0
    if buffered:
        pieces += hasher.digest()
    info = {
        "name": name,
        "piece length": PIECE_LENGTH,
        "pieces": bytes(pieces),
        "files": entries,
    }
    torrent = {
        "info": info,
        "url-list": webseeds,
        "created by": "hermes scripts/make-fixture-torrent.py",
        "comment": "Hermes development fixture; public-domain LibriVox audio via web seeds",
    }
    return bencode(torrent), hashlib.sha1(bencode(info)).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--source", type=Path, required=True, help="directory holding the files")
    ap.add_argument("--name", required=True, help="torrent name (top-level directory)")
    ap.add_argument(
        "--webseed", action="append", required=True, help="web seed base URL (repeatable)"
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("files", nargs="+", help="paths relative to --source, in torrent order")
    args = ap.parse_args()
    data, infohash = build(args.source, args.name, args.files, args.webseed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    total = sum((args.source / f).stat().st_size for f in args.files)
    print(f"wrote {args.out} ({len(data)} bytes): {len(args.files)} files, {total} bytes")
    print(f"infohash {infohash}")


if __name__ == "__main__":
    main()
