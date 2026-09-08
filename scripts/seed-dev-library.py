#!/usr/bin/env python3
"""Seed the compose stack's beets library with Creative Commons albums (no audio files).

Runs inside the beets container with beets' own interpreter:

    docker compose exec beets beets-python /scripts/seed-dev-library.py

Every album here is Creative Commons or artist-permitted, exists on PandaCD (the dev
tracker) and in MusicBrainz, so the same titles can be resolved, searched, downloaded and
imported for real in later milestones. Rows are created through beets' Library API:

    Josh Woodward - Dirty Wings                 FLAC 16/44.1  -> owned
    Chris Zabriskie - Vendaface                 FLAC 24/44.1  -> owned (hi-res)
    Chris Zabriskie - Stunt Island              MP3 320       -> owned_lossy
    Nine Inch Nails - The Slip                  not seeded    -> missing (270 MB FLAC on PandaCD)
    Josh Woodward - Addressed to the Stars      not seeded    -> missing

Idempotent: albums whose release group is already present are skipped.
"""

from __future__ import annotations

import os
import sys

from beets import config
from beets.dbcore import query as dbq
from beets.library import Item, Library

ALBUMS = [
    {
        "albumartist": "Josh Woodward",
        "album": "Dirty Wings",
        "year": 2007,
        "mb_releasegroupid": "41a91477-454e-3f19-8165-caf1be27be50",
        "mb_albumid": "fd49d30e-a297-458e-b20f-738777202f08",
        "tracks": 14,
        "format": "FLAC",
        "bitdepth": 16,
        "samplerate": 44100,
        "bitrate": 850000,
    },
    {
        "albumartist": "Chris Zabriskie",
        "album": "Vendaface",
        "year": 2010,
        "mb_releasegroupid": "2f4f9924-dfcd-474d-9d04-1a4b4d9c2502",
        "mb_albumid": "2cc95c59-b2c5-40b9-8638-923ce83e82c9",
        "tracks": 6,
        "format": "FLAC",
        "bitdepth": 24,
        "samplerate": 44100,
        "bitrate": 1400000,
    },
    {
        "albumartist": "Chris Zabriskie",
        "album": "Stunt Island",
        "year": 2011,
        "mb_releasegroupid": "314d6e40-6e9d-4850-8262-b9fde9b3fe12",
        "mb_albumid": "dcee23d7-818d-425f-906f-a1615e57c6d3",
        "tracks": 5,
        "format": "MP3",
        "bitdepth": 0,
        "samplerate": 44100,
        "bitrate": 320000,
    },
]


def main() -> int:
    config.read()
    lib_path = os.path.expanduser(str(config["library"].as_filename()))
    lib = Library(lib_path)
    added = 0
    for spec in ALBUMS:
        present = list(lib.albums(dbq.MatchQuery("mb_releasegroupid", spec["mb_releasegroupid"])))
        if present:
            print(f"skip  {spec['albumartist']} - {spec['album']} (already in library)")
            continue
        items = []
        for n in range(1, spec["tracks"] + 1):
            item = Item(
                title=f"Track {n:02d}",
                track=n,
                tracktotal=spec["tracks"],
                artist=spec["albumartist"],
                albumartist=spec["albumartist"],
                album=spec["album"],
                year=spec["year"],
                original_year=spec["year"],
                mb_releasegroupid=spec["mb_releasegroupid"],
                mb_albumid=spec["mb_albumid"],
                format=spec["format"],
                bitdepth=spec["bitdepth"],
                samplerate=spec["samplerate"],
                bitrate=spec["bitrate"],
                channels=2,
                length=180.0,
            )
            ext = spec["format"].lower()
            item.path = (
                f"/music/{spec['albumartist']}/{spec['album']}/{n:02d} - Track {n:02d}.{ext}"
            ).encode()
            items.append(item)
        lib.add_album(items)
        added += 1
        label = f"{spec['albumartist']} - {spec['album']}"
        print(f"added {label} ({spec['format']}, {spec['tracks']} tracks)")
    print(f"library {lib_path}: {added} album(s) added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
