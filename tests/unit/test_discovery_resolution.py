"""Recording -> release group resolution for discovered tracks, and the JSPF parser, on
captured ListenBrainz and MusicBrainz responses."""

from __future__ import annotations

from hermes.config import ResolutionPolicy
from hermes.integrations.listenbrainz import parse_playlist
from hermes.integrations.musicbrainz import parse_recording
from hermes.services.resolution import choose_for_recording
from tests.fixtures import load

HER_ENTRANCE = "recording_503ea09d"  # Parra for Cuva: one Album (Juno)
THEME = "recording_0fffc332"  # Rival Consoles: a Single and a Soundtrack album
LYDIA = "recording_dad4820e"  # Dauwd: a Remix EP and a various-artists compilation


def test_playlist_parser_reads_jspf() -> None:
    pl = parse_playlist(load("listenbrainz/playlist_weekly_exploration"))
    assert pl.mbid == "55f2a01f-3974-46ca-8461-a8a84886eba4"
    assert pl.source == "weekly-exploration" and pl.created_for == "lbuser" and pl.public
    assert [t.position for t in pl.tracks] == [0, 1, 2]
    first = pl.tracks[0]
    assert first.recording_mbid == "dad4820e-4887-4d1e-83e2-6fe1166b3f49"
    assert first.artist == "Dauwd" and first.title == "Lydia"
    assert first.release_mbid == "5828cd29-f48b-40cf-8948-df723d8a87fa"


def test_createdfor_listing_carries_the_patch_name() -> None:
    listing = [parse_playlist(p) for p in load("listenbrainz/createdfor")["playlists"]]
    assert [p.source for p in listing] == ["daily-jams", "weekly-exploration", "weekly-jams"]
    assert all(p.tracks == [] for p in listing)


def test_single_album_recording_resolves() -> None:
    res = choose_for_recording(
        parse_recording(load(f"musicbrainz/{HER_ENTRANCE}")), ResolutionPolicy()
    )
    assert res.outcome == "resolved" and res.target is not None
    assert res.target.title == "Juno" and res.target.primary_type == "Album"
    assert res.target.artist_name == "Parra for Cuva"
    assert [r.id for r in res.target.releases]  # the releases carrying the recording


def test_single_and_soundtrack_are_nothing_acquirable_by_default() -> None:
    rec = parse_recording(load(f"musicbrainz/{THEME}"))
    res = choose_for_recording(rec, ResolutionPolicy())
    assert res.outcome == "nothing_acquirable"
    reasons = {c.release_group.title: c.rejected_reason for c in res.candidates}
    assert "allow_single" in (reasons["Theme (From MindsEye)"] or "")
    assert "Soundtrack" in (reasons["MindsEye"] or "")
    # Allowing soundtracks picks the Album over the Single (type order), allowing singles
    # alone picks the Single.
    res = choose_for_recording(rec, ResolutionPolicy(allow_secondary_types=["Soundtrack"]))
    assert res.outcome == "resolved" and res.target and res.target.title == "MindsEye"
    res = choose_for_recording(rec, ResolutionPolicy(allow_single=True))
    assert res.outcome == "resolved" and res.target and res.target.primary_type == "Single"


def test_compilation_by_another_artist_credit_is_rejected() -> None:
    rec = parse_recording(load(f"musicbrainz/{LYDIA}"))
    res = choose_for_recording(rec, ResolutionPolicy(allow_secondary_types=["Compilation"]))
    assert res.outcome == "nothing_acquirable"
    reasons = {c.release_group.title: c.rejected_reason for c in res.candidates}
    assert "Remix" in (reasons["Kindlinn"] or "")
    assert "not the recording's artist" in (reasons["Techno Anthology"] or "")
    res = choose_for_recording(rec, ResolutionPolicy(allow_secondary_types=["Remix"]))
    assert res.outcome == "resolved" and res.target and res.target.title == "Kindlinn"


def test_featured_artist_track_resolves_to_the_main_artists_album() -> None:
    """Seen live: 'Gorillaz feat. Little Dragon - Empire Ants' was rejected because the album
    is credited to Gorillaz alone. Every album-credit artist appears in the recording credit,
    so the album is the artist's own; the G Collection compilation is still refused."""
    rec = parse_recording(load("musicbrainz/recording_a0481faf"))
    assert rec.artist_name.startswith("Gorillaz feat.")
    res = choose_for_recording(rec, ResolutionPolicy())
    assert res.outcome == "resolved" and res.target is not None
    assert res.target.title == "Plastic Beach" and res.target.artist_name == "Gorillaz"
    reasons = {c.release_group.title: c.rejected_reason for c in res.candidates}
    assert "Compilation" in (reasons.get("G Collection") or "")
