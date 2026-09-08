# ruff: noqa: E501  (corpus titles are long by nature)
from __future__ import annotations

from pathlib import Path

import pytest

from hermes.services.title_parser import parse_title
from tests.fixtures import FIXTURES

CORPUS = [
    line.strip()
    for path in sorted((FIXTURES / "titles").glob("*.txt"))
    for line in Path(path).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]

CURATED = {
    "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        artist="Radiohead",
        album="OK Computer",
        year=1997,
        release_type="Album",
        format="FLAC",
        encoding="lossless",
        media="CD",
        has_log=True,
        log_score=100,
        has_cue=True,
    ),
    "Radiohead - OK Computer (1997) [Album] [OKNOTOK 1997 2017 2017] [FLAC 24bit Lossless / WEB]": dict(
        year=1997,
        remaster_title="OKNOTOK 1997 2017",
        remaster_year=2017,
        format="FLAC",
        encoding="lossless24",
        encoding_detail="24bit Lossless",
        media="WEB",
        has_log=False,
        has_cue=False,
    ),
    "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD / Log / Cue]": dict(
        has_log=True, log_score=None, has_cue=True, media="CD"
    ),
    "Radiohead - OK Computer (1997) [Album] [MP3 V0 (VBR) / CD]": dict(
        format="MP3", encoding="lossy", encoding_detail="V0 (VBR)", media="CD"
    ),
    "Radiohead - OK Computer (1997) [Album] [AAC 256 / WEB]": dict(
        format="AAC", encoding="lossy", encoding_detail="256", media="WEB"
    ),
    "Portishead - Dummy (1994) [Album] [FLAC Lossless / Vinyl]": dict(
        media="Vinyl", encoding="lossless"
    ),
    "Portishead - Dummy (1994) [Album] [Remastered 2014] [FLAC 24bit Lossless / WEB]": dict(
        remaster_title="Remastered", remaster_year=2014, edition_flags=["remaster"]
    ),
    "Various Artists - Pulp Fiction (1994) [Soundtrack] [MP3 320 / CD]": dict(
        artist="Various Artists", release_type="Soundtrack", format="MP3", encoding_detail="320"
    ),
    "Burial - Rival Dealer (2013) [EP] [FLAC Lossless / WEB]": dict(release_type="EP"),
    "Portishead - Roseland NYC Live (1998) [Live album] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        release_type="Live album"
    ),
    "Nine Inch Nails - The Slip (2008) [Album] [Deluxe Edition 2008] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        remaster_title="Deluxe Edition", remaster_year=2008, edition_flags=["deluxe"]
    ),
    "Sigur Rós - ( ) (2002) [Album] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        artist="Sigur Rós", album="( )", year=2002
    ),
    "Godspeed You! Black Emperor - F♯ A♯ ∞ (1997) [Album] [FLAC Lossless / Vinyl]": dict(
        artist="Godspeed You! Black Emperor", album="F♯ A♯ ∞", year=1997
    ),
    "Bon Iver - Bon Iver, Bon Iver (2011) [Album] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        artist="Bon Iver", album="Bon Iver, Bon Iver"
    ),
    "Fleetwood Mac - Rumours (1977) [Album] [DTS 1536 / DVD]": dict(
        format="DTS", encoding="lossy", media="DVD"
    ),
    "Radiohead - OK Computer (1997) [Album] [WAV / CD]": dict(
        format="WAV", encoding="lossless", encoding_detail=None
    ),
    "Radiohead - OK Computer (1997) [Album] [Ogg Vorbis q10 (VBR) / WEB]": dict(
        format="Ogg Vorbis", encoding_detail="q10 (VBR)"
    ),
    "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB] [Scene]": dict(
        media="WEB", edition_flags=["scene"]
    ),
    "The Beatles - Abbey Road (1969) [Album] [2009 Remaster 2009] [FLAC Lossless / CD / Log (100%) / Cue]": dict(
        remaster_title="2009 Remaster", remaster_year=2009
    ),
    # PandaCD family
    "Nine Inch Nails - The Slip [2008] [FLAC Lossless]": dict(
        artist="Nine Inch Nails",
        album="The Slip",
        year=2008,
        format="FLAC",
        encoding="lossless",
        media=None,
        release_type=None,
    ),
    "Chris Zabriskie - Vendaface [2010] [FLAC 24bit Lossless]": dict(
        year=2010, encoding="lossless24"
    ),
    "Josh Woodward - Addressed to the Stars [2016] [MP3 V0 (VBR)]": dict(
        year=2016, format="MP3", encoding="lossy", encoding_detail="V0 (VBR)"
    ),
    "Nine Inch Nails - The Slip [2008] [Ogg Vorbis q8.x (VBR)]": dict(
        format="Ogg Vorbis", encoding="lossy", encoding_detail="q8.x (VBR)"
    ),
    "Nine Inch Nails - The Slip [2008] [Opus 128 (VBR)]": dict(
        format="Opus", encoding_detail="128 (VBR)"
    ),
}


@pytest.mark.parametrize(("title", "expected"), CURATED.items(), ids=list(CURATED))
def test_curated_titles(title: str, expected: dict[str, object]) -> None:
    parsed = parse_title(title)
    assert parsed.ok, parsed.problems
    for key, value in expected.items():
        assert getattr(parsed, key) == value, f"{key}: {getattr(parsed, key)!r} != {value!r}"


def test_corpus_is_large_enough() -> None:
    assert len(CORPUS) >= 100


@pytest.mark.parametrize("title", CORPUS, ids=lambda t: t[:60])
def test_every_corpus_title_parses(title: str) -> None:
    parsed = parse_title(title)
    assert parsed.ok, f"{title}: {parsed.problems}"
    assert parsed.encoding in {"lossless", "lossless24", "lossy"}
    assert not [p for p in parsed.problems if p.startswith("unrecognised")], parsed.problems


def test_garbage_does_not_raise() -> None:
    for junk in ("", "no dashes here", "[FLAC]", "A - B", "Artist - Album [2020] [Unicorn 320]"):
        parsed = parse_title(junk)
        assert parsed.ok is False
