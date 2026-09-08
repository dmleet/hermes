"""Ask beets whether an album target is already owned (docs/plan.md 5.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes.domain.models import LibraryStatus
from hermes.integrations.beets import BeetsClient, LibraryAlbum
from hermes.services.matching import strip_edition
from hermes.services.text import similarity

FUZZY_MIN_SIMILARITY = 0.9


@dataclass
class LibraryCheck:
    status: LibraryStatus
    albums: list[LibraryAlbum] = field(default_factory=list)
    matched_by: str | None = None  # release_group | release | fuzzy

    @property
    def owned(self) -> bool:
        return self.status in (LibraryStatus.OWNED, LibraryStatus.OWNED_LOSSY)


def classify(albums: list[LibraryAlbum]) -> LibraryStatus:
    albums = [a for a in albums if a.quality.items > 0]  # an album row with no items is noise
    if not albums:
        return LibraryStatus.MISSING
    if any(a.quality.lossless for a in albums):
        return LibraryStatus.OWNED
    return LibraryStatus.OWNED_LOSSY


def _fuzzy_matches(
    albums: list[LibraryAlbum], artist: str, title: str, year: int | None
) -> list[LibraryAlbum]:
    out = []
    for a in albums:
        if similarity(artist, a.albumartist) < FUZZY_MIN_SIMILARITY:
            continue
        if max(similarity(title, a.album), similarity(title, strip_edition(a.album))) < (
            FUZZY_MIN_SIMILARITY
        ):
            continue
        candidate_year = a.original_year or a.year
        if year and candidate_year and abs(candidate_year - year) > 1:
            continue
        out.append(a)
    return out


async def check_library(
    beets: BeetsClient,
    *,
    release_group_mbid: str,
    release_mbids: list[str],
    artist: str,
    title: str,
    year: int | None,
) -> LibraryCheck:
    albums = await beets.albums_in_release_group(release_group_mbid)
    if albums:
        return LibraryCheck(classify(albums), albums, "release_group")
    for mbid in release_mbids:
        albums = await beets.albums_for_release(mbid)
        if albums:
            return LibraryCheck(classify(albums), albums, "release")
    albums = _fuzzy_matches(await beets.search(album=title), artist, title, year)
    if albums:
        return LibraryCheck(classify(albums), albums, "fuzzy")
    return LibraryCheck(LibraryStatus.MISSING)
