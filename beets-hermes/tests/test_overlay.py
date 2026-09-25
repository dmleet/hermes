"""The import overlay against beets' own scoring, on a real case."""

import pytest
from beets import config
from beets.autotag import AlbumInfo, TrackInfo
from beets.autotag.distance import distance
from beets.autotag.match import Recommendation, _recommendation, assign_items
from beets.autotag.source import Source
from beets.library import Item
from beets.util import cached_classproperty

from beetsplug.hermes import IMPORT_OVERLAY

# A 2-CD anthology as MusicBrainz has it (title, seconds), and as the uploader tagged it:
# each disc its own album ("(CD 1)"/"(CD 2)" in the title, every file disc 1 of 1), a few
# spellings apart. Interactively beets scored this 90.6% on the correct release.
RELEASE = [
    [
        ("Father Cannot Yell", 418),
        ("Soup", 163),
        ("Mother Sky", 396),
        ("She Brings the Rain", 247),
        ("Mushroom", 267),
        ("One More Night", 338),
        ("Outside My Door", 248),
        ("Spoon", 183),
        ("Halleluwah", 336),
        ("Aumngn", 432),
        ("Dizzy Dizzy", 207),
        ("You Doo Right", 1219),
    ],
    [
        ("Uphill", 386),
        ("Mother Upduff", 269),
        ("Doko e", 148),
        ("Musette", 134),
        ("Blue Bag", 75),
        ("TV Spot", 186),
        ("Half Past One", 279),
        ("Moonshake", 182),
        ("Future Days", 570),
        ("Cascade Waltz", 341),
        ("I Want More", 211),
        ("Animal Waves", 489),
        ("Don’t Say No", 395),
        ("Aspectacle", 188),
        ("Below This Level", 135),
        ("Hoolah Hoolah", 270),
        ("Last Night Sleep", 215),
    ],
]
TAGGED = {
    "She Brings the Rain": "She Brings The Rain",
    "Aumngn": "Aumgn",
    "You Doo Right": "Yoo Doo Right",
    "Doko e": "Doko E.",
    "Don’t Say No": "Don't Say No",
}


def _album() -> tuple[list[Item], AlbumInfo]:
    items, tracks, index = [], [], 0
    for disc, titles in enumerate(RELEASE, start=1):
        for number, (title, seconds) in enumerate(titles, start=1):
            index += 1
            tracks.append(
                TrackInfo(
                    title=title,
                    length=seconds,
                    index=index,
                    medium=disc,
                    medium_index=number,
                    track_id=f"t{index}",
                )
            )
            items.append(
                Item(
                    artist="Can",
                    albumartist="Can",
                    album=f"Anthology - 25 Years (CD {disc})",
                    title=TAGGED.get(title, title),
                    length=seconds,
                    track=number,
                    disc=1,
                    disctotal=1,
                )
            )
    info = AlbumInfo(
        tracks=tracks,
        album="Anthology: 25 Years",
        album_id="r1",
        artist="Can",
        artist_id="a1",
        mediums=2,
        media="CD",
        year=2007,
        country="GB",
        label="Spoon Records",
        catalognum="CDSPOON 30/31",
    )
    return items, info


def _rec(tmp_path, overlay: bool) -> tuple[float, Recommendation]:
    config.clear()
    config.read(user=False, defaults=True)
    if overlay:
        path = tmp_path / "overlay.yaml"
        path.write_text(IMPORT_OVERLAY)
        config.set_file(path)
    cached_classproperty.cache.clear()  # Distance caches its weights per process
    items, info = _album()
    pairs, extra_items, extra_tracks = assign_items(items, info.tracks)
    assert not extra_items and not extra_tracks
    dist = distance(Source.from_items(items).data, info, pairs, len(extra_items))

    class Match:  # the two attributes _recommendation reads
        distance = dist

    return dist.distance, _recommendation([Match])


@pytest.fixture(autouse=True)
def _reset_weights():
    yield
    config.clear()
    config.read(user=False, defaults=True)
    cached_classproperty.cache.clear()


def test_per_disc_album_tags_no_longer_cost_the_pinned_release(tmp_path):
    before, rec_before = _rec(tmp_path, overlay=False)
    after, rec_after = _rec(tmp_path, overlay=True)
    # Without the overlay this is the skip seen in the cluster: correct release, not strong.
    assert rec_before < Recommendation.strong and before == pytest.approx(0.094, abs=0.005)
    assert rec_after == Recommendation.strong and after < 0.04


def test_a_different_album_on_the_same_release_shape_still_fails(tmp_path):
    items, info = _album()
    for n, track in enumerate(info.tracks):
        track.title = f"Unrelated Song {n}"
    config.clear()
    config.read(user=False, defaults=True)
    path = tmp_path / "overlay.yaml"
    path.write_text(IMPORT_OVERLAY)
    config.set_file(path)
    cached_classproperty.cache.clear()
    pairs, _, _ = assign_items(items, info.tracks)
    dist = distance(Source.from_items(items).data, info, pairs, 0)
    assert dist.distance > config["match"]["medium_rec_thresh"].as_number()


def test_another_releases_id_in_the_tags_no_longer_costs_the_pinned_release(tmp_path):
    # A 1-CD album whose files carry the MusicBrainz ids of another country's release of
    # the same tracks: interactively beets scored it 82.1%, penalised on the id alone.
    titles = [
        "Venus",
        "Cherry Blossom Girl",
        "Run",
        "Universal Traveler",
        "Mike Mills",
        "Surfing on a Rocket",
        "Another Day",
        "Alpha Beta Gaga",
        "Biological",
        "Alone in Kyoto",
    ]
    tracks = [
        TrackInfo(title=t, length=240, index=n, medium=1, medium_index=n, track_id=f"pinned-{n}")
        for n, t in enumerate(titles, start=1)
    ]
    items = [
        Item(
            artist="Air",
            albumartist="Air",
            album="Talkie Walkie",
            title=t,
            length=240,
            track=n,
            disc=1,
            disctotal=1,
            mb_albumid="other-release",
            mb_trackid=f"other-{n}",
        )
        for n, t in enumerate(titles, start=1)
    ]
    info = AlbumInfo(
        tracks=tracks,
        album="Talkie Walkie",
        album_id="pinned",
        artist="Air",
        artist_id="a1",
        mediums=1,
        media="CD",
        year=2004,
    )

    def score(overlay: bool) -> float:
        config.clear()
        config.read(user=False, defaults=True)
        if overlay:
            path = tmp_path / "overlay.yaml"
            path.write_text(IMPORT_OVERLAY)
            config.set_file(path)
        cached_classproperty.cache.clear()
        pairs, _, _ = assign_items(items, info.tracks)
        return distance(Source.from_items(items).data, info, pairs, 0).distance

    assert score(overlay=False) > 0.04
    assert score(overlay=True) < 0.01


def test_two_discs_with_the_same_titles_keep_their_files(tmp_path):
    # A stereo and a mono mix of one album on two discs: the same titles, lengths seconds
    # apart. With nothing but the disc number and track id to tell the discs apart, the
    # overlay must still let beets put each file on its own disc.
    titles = [f"Song {n}" for n in range(1, 14)]
    tracks, items, index = [], [], 0
    for disc in (1, 2):
        for number, title in enumerate(titles, start=1):
            index += 1
            tracks.append(
                TrackInfo(
                    title=title,
                    length=200 + number,
                    index=index,
                    medium=disc,
                    medium_index=number,
                    track_id=f"rec-{disc}-{number}",
                )
            )
            items.append(
                Item(
                    artist="Artist",
                    albumartist="Artist",
                    album="Album",
                    title=title,
                    length=200 + number + (3 if disc == 2 else 0),
                    track=number,
                    disc=disc,
                    disctotal=2,
                    mb_trackid=f"rec-{disc}-{number}",
                )
            )
    config.clear()
    config.read(user=False, defaults=True)
    path = tmp_path / "overlay.yaml"
    path.write_text(IMPORT_OVERLAY)
    config.set_file(path)
    cached_classproperty.cache.clear()
    pairs, extra_items, extra_tracks = assign_items(items, tracks)
    assert not extra_items and not extra_tracks
    assert all(item.disc == track.medium for item, track in pairs)
