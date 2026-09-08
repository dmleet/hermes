from __future__ import annotations

from hermes.domain.models import LibraryStatus
from hermes.integrations.beets import LibraryAlbum
from hermes.services.library_check import _fuzzy_matches, classify


def _album(**kw) -> LibraryAlbum:
    base = dict(
        id=1,
        albumartist="Portishead",
        album="Dummy",
        year=1994,
        quality={
            "items": 11,
            "formats": ["FLAC"],
            "min_bitdepth": 16,
            "min_samplerate": 44100,
            "lossy_items": 0,
        },
    )
    base.update(kw)
    return LibraryAlbum.model_validate(base)


def test_classify() -> None:
    assert classify([]) == LibraryStatus.MISSING
    assert classify([_album()]) == LibraryStatus.OWNED
    lossy = _album(quality={"items": 11, "formats": ["MP3"], "lossy_items": 11})
    assert classify([lossy]) == LibraryStatus.OWNED_LOSSY
    assert classify([lossy, _album()]) == LibraryStatus.OWNED  # any lossless copy counts


def test_fuzzy_matching_requires_artist_title_and_year() -> None:
    albums = [
        _album(id=1),
        _album(id=2, albumartist="Portishead", album="Dummy", year=2008),  # wrong year
        _album(id=3, albumartist="Massive Attack", album="Dummy"),  # wrong artist
        _album(id=4, albumartist="portishead", album="DUMMY", year=1995),  # case + year within 1
    ]
    hits = _fuzzy_matches(albums, "Portishead", "Dummy", 1994)
    assert [a.id for a in hits] == [1, 4]
    assert [a.id for a in _fuzzy_matches(albums, "Portishead", "Dummy", None)] == [1, 2, 4]
