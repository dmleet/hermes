from __future__ import annotations

import pytest

from hermes.services.matching import match, strip_edition
from hermes.services.title_parser import parse_title


def _m(title: str, **target):
    base = dict(artist="Radiohead", title="OK Computer", year=1997)
    base.update(target)
    return match(parse_title(title), **base)


def test_exact_match_scores_high() -> None:
    m = _m("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]")
    assert m.score > 0.99 and m.year_ok is True


def test_edition_suffix_is_stripped_for_title_comparison() -> None:
    assert strip_edition("OK Computer (Deluxe Edition)") == "OK Computer"
    assert strip_edition("OK Computer [2017 Remaster]") == "OK Computer"
    assert strip_edition("OK Computer") == "OK Computer"
    m = _m("Radiohead - OK Computer (Deluxe Edition) (1997) [Album] [FLAC Lossless / WEB]")
    assert m.title_similarity > 0.99 and "edition suffix" in m.notes[0]


def test_wrong_album_scores_low() -> None:
    m = _m("Radiohead - Kid A (2000) [Album] [FLAC Lossless / WEB]")
    assert m.score < 0.7


def test_year_mismatch_penalised_unless_remaster() -> None:
    plain = _m("Radiohead - OK Computer (2017) [Album] [FLAC Lossless / WEB]")
    assert plain.year_ok is False and plain.score < 0.9
    remaster = _m(
        "Radiohead - OK Computer (2017) [Album] [OKNOTOK 1997 2017 2017] [FLAC Lossless / WEB]"
    )
    assert remaster.year_ok is True and remaster.score > 0.99


def test_unknown_year_is_neutral() -> None:
    m = _m("Radiohead - OK Computer [FLAC Lossless]")
    assert m.year_ok is None and m.score > 0.99


def test_soundtrack_subtitle_does_not_block_the_match() -> None:
    """Captured from tracker A: every Interstellar release was rejected because MusicBrainz
    calls the album 'Interstellar: Original Motion Picture Soundtrack'."""
    ost = dict(artist="Hans Zimmer", title="Interstellar: Original Motion Picture Soundtrack")
    plain = "Hans Zimmer - Interstellar (2014) [Soundtrack] [FLAC Lossless / WEB]"
    r = match(parse_title(plain), year=2014, **ost)
    assert r.title_similarity == 1.0 and r.score >= 0.95
    assert any("subtitle" in n for n in r.notes)
    performed = (
        "Hans Zimmer performed by Hans Zimmer - Interstellar: Original Motion Picture "
        "Soundtrack (2014) [Soundtrack] [FLAC 24bit Lossless / Vinyl / Cue]"
    )
    r = match(parse_title(performed), year=2014, **ost)
    assert r.artist_similarity == 1.0 and r.title_similarity == 1.0
    assert any("performed by" in n for n in r.notes)
    # A different album that merely shares the head word still scores low.
    other = "Hans Zimmer - Interstellar Overdrive (2014) [Album] [FLAC Lossless / WEB]"
    assert match(parse_title(other), year=2014, **ost).title_similarity < 0.85


def test_parenthesised_alternative_title_matches() -> None:
    """Seen live on tracker A: 'Gorillaz - पर्वत (The Mountain) (2026)' against the
    MusicBrainz group title 'The Mountain' scored 0.64 and missed the threshold."""
    r = match(
        parse_title(
            "Gorillaz - \u092a\u0930\u094d\u0935\u0924 (The Mountain) (2026) [Album] "
            "[FLAC Lossless / CD / Log (100%) / Cue]"
        ),
        artist="Gorillaz",
        title="The Mountain",
        year=2026,
    )
    assert r.title_similarity == 1.0 and r.score >= 0.95
    assert any("parenthesis" in n for n in r.notes)
    # The other way round: MusicBrainz carries the native title, the tracker the English.
    r = match(
        parse_title("Gorillaz - The Mountain (2026) [Album] [FLAC Lossless / WEB]"),
        artist="Gorillaz",
        title="\u092a\u0930\u094d\u0935\u0924 (The Mountain)",
        year=2026,
    )
    assert r.title_similarity == 1.0


@pytest.mark.parametrize(
    ("target", "tracker"),
    [
        ("Seven Days Walking: Day One", "Seven Days Walking: Day Two"),
        ("Cowboy Bebop: Vitaminless", "Cowboy Bebop: No Disc"),
        ("Phantomime (Volume 1)", "Phantomime (Volume 2)"),
        ("Rival Dealer", "Rival Dealer: Remixes"),
        ("Homogenic", "Homogenic - Live"),
        ("Interstellar: Original Motion Picture Soundtrack", "Interstellar Overdrive"),
    ],
)
def test_subtitle_handling_does_not_match_a_different_release(target: str, tracker: str) -> None:
    """Review M11: reducing both titles to their heads matched sequels, remix albums and
    live records. A tail only the tracker has, or tails that disagree, is a different album."""
    r = match(
        parse_title(f"Artist - {tracker} (2019) [Album] [FLAC Lossless / WEB]"),
        artist="Artist",
        title=target,
        year=2019,
    )
    assert r.title_similarity < 0.85, (target, tracker, r)


@pytest.mark.parametrize(
    ("target", "tracker"),
    [
        ("Interstellar: Original Motion Picture Soundtrack", "Interstellar"),
        ("The Mountain", "\u092a\u0930\u094d\u0935\u0924 (The Mountain)"),
        ("Seven Days Walking: Day One", "Seven Days Walking: Day One"),
        ("Phantomime (Volume 1)", "Phantomime (Volume 1)"),
        ("Dummy", "Dummy (Original Soundtrack)"),
    ],
)
def test_subtitle_handling_still_matches_the_same_release(target: str, tracker: str) -> None:
    r = match(
        parse_title(f"Artist - {tracker} (2019) [Album] [FLAC Lossless / WEB]"),
        artist="Artist",
        title=target,
        year=2019,
    )
    assert r.title_similarity >= 0.95, (target, tracker, r)


def test_unparsed_title_scores_zero() -> None:
    m = _m("random garbage")
    assert m.score == 0.0 and m.notes == ["title did not parse"]
