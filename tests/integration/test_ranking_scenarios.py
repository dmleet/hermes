"""Ranking on captured tracker A searches (tests/fixtures/prowlarr/gazelle): for each album
the expected winner and the reasons the rest were refused. Add a case whenever a real
result set ranks wrongly; the fixture is the search response, download links redacted."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes.config import QualityPolicy
from hermes.integrations.prowlarr import ReleaseResult
from hermes.services.ranking import evaluate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "prowlarr" / "gazelle"


@dataclass
class Case:
    fixture: str
    artist: str
    title: str
    year: int
    types: set[str] = field(default_factory=lambda: {"Album"})
    top_contains: str = ""  # a substring the #1 title must carry
    top_media: str = "WEB"
    top_plain: bool = True  # the winner is the plain album, not an edition
    min_accepted: int = 1
    rejected_reasons: tuple[str, ...] = ()  # substrings that must appear among rejections
    duration_ms: int | None = None  # the plain album's playing time, for the rate estimate
    tracks: int = 0  # the plain album's track count, for editions' estimates


CASES = [
    # OKNOTOK has four times the seeders of the plain album and is 24-bit; the reissue
    # penalty still decides (until 2026-09-15 the seeders did, and the reissue won).
    # Kendrick: no plain WEB FLAC exists, so the plain CD rip beats the Deluxe WEB uploads.
    # Abbey Road: every lossless upload is some reissue, so the edition penalties pick the
    # least reworked one -- the 2015 remaster, ahead of a logged 1987 Italy reissue CD.
    Case(
        "radiohead_ok_computer",
        "Radiohead",
        "OK Computer",
        1997,
        duration_ms=3201000,
        tracks=12,
        top_contains="OK Computer",
        rejected_reasons=("Vinyl is excluded", "MP3 is not FLAC"),
    ),
    Case(
        "nine_inch_nails_the_downward_spiral",
        "Nine Inch Nails",
        "The Downward Spiral",
        1994,
        duration_ms=3902000,
        tracks=14,
    ),
    Case(
        "miles_davis_kind_of_blue",
        "Miles Davis",
        "Kind of Blue",
        1959,
        duration_ms=2744000,
        tracks=5,
        rejected_reasons=("Compilation does not match", "Anthology does not match"),
    ),
    Case("bj_rk_homogenic", "Björk", "Homogenic", 1997, duration_ms=2616000, tracks=10),
    Case(
        "sigur_r_s_g_tis_byrjun", "Sigur Rós", "Ágætis byrjun", 1999, duration_ms=4306000, tracks=10
    ),
    Case(
        "pink_floyd_the_wall",
        "Pink Floyd",
        "The Wall",
        1979,
        duration_ms=4869000,
        tracks=26,
        rejected_reasons=("Bootleg is not allowed",),
    ),
    Case("daft_punk_discovery", "Daft Punk", "Discovery", 2001, duration_ms=3657000, tracks=14),
    Case(
        "burial_rival_dealer",
        "Burial",
        "Rival Dealer",
        2013,
        duration_ms=1720000,
        tracks=4,
        types={"EP"},
    ),
    Case(
        "aphex_twin_selected_ambient_works_85_92",
        "Aphex Twin",
        "Selected Ambient Works 85-92",
        1992,
        duration_ms=4463000,
        tracks=13,
    ),
    Case(
        "boards_of_canada_music_has_the_right_to_children",
        "Boards of Canada",
        "Music Has the Right to Children",
        1998,
        duration_ms=3778000,
        tracks=18,
    ),
    Case(
        "godspeed_you_black_emperor_lift_your_skinny_fists_like_anten",
        "Godspeed You Black Emperor!",
        "Lift Yr. Skinny Fists Like Antennas to Heaven!",
        2000,
        duration_ms=5232000,
        tracks=4,
    ),
    Case(
        "kendrick_lamar_good_kid_m_a_a_d_city",
        "Kendrick Lamar",
        "good kid, m.A.A.d city",
        2012,
        duration_ms=4114000,
        tracks=12,
        top_media="CD",
    ),
    Case(
        "hans_zimmer_interstellar",
        "Hans Zimmer",
        "Interstellar: Original Motion Picture Soundtrack",
        2014,
        duration_ms=4322000,
        tracks=16,
        types={"Album", "Soundtrack"},
    ),
    Case(
        "fleetwood_mac_rumours",
        "Fleetwood Mac",
        "Rumours",
        1977,
        duration_ms=2383000,
        tracks=11,
        rejected_reasons=("Live album does not match",),
    ),
    Case(
        "the_beatles_abbey_road",
        "The Beatles",
        "Abbey Road",
        1969,
        duration_ms=2843000,
        tracks=17,
        top_plain=False,
    ),
    Case(
        "massive_attack_mezzanine",
        "Massive Attack",
        "Mezzanine",
        1998,
        duration_ms=3807000,
        tracks=11,
        rejected_reasons=("Remix is not allowed",),
    ),
    Case("tool_lateralus", "Tool", "Lateralus", 2001, duration_ms=4731000, tracks=13),
    Case("portishead_dummy", "Portishead", "Dummy", 1994, duration_ms=2926000, tracks=11),
    Case("air_moon_safari", "Air", "Moon Safari", 1998, duration_ms=2627000, tracks=10),
    Case("tycho_dive", "Tycho", "Dive", 2011, duration_ms=3069000, tracks=10),
]

_EDITION_WORDS = ("deluxe", "anniversary", "expanded", "box", "super", "bonus")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.fixture)
def test_captured_search_ranks_as_expected(case: Case) -> None:
    rows = json.loads((FIXTURES / f"{case.fixture}.json").read_text(encoding="utf-8"))
    releases = [ReleaseResult.from_api(r) for r in rows]
    ranked = evaluate(
        releases,
        artist=case.artist,
        title=case.title,
        year=case.year,
        policy=QualityPolicy(),
        target_types=case.types,
        duration_ms=case.duration_ms,
        track_count=case.tracks,
    )
    accepted = [c for c in ranked if c.accepted]
    if case.duration_ms:
        # 16-bit uploads are never refused for their rate (only shown); 24-bit ones that
        # estimate above 96 kHz are refused, for that or an earlier reason.
        for c in ranked:
            if c.parsed.encoding == "lossless":
                assert "kHz" not in (c.rejected_reason or ""), c.release.title
            if c.parsed.encoding == "lossless24" and c.estimate and c.estimate["rate_khz"] > 96:
                assert c.rejected_reason, c.release.title
    assert len(accepted) >= case.min_accepted, [c.rejected_reason for c in ranked[:5]]
    top = accepted[0]
    assert top.rank == 1
    assert top.parsed.media == case.top_media, top.release.title
    assert top.parsed.encoding in ("lossless", "lossless24"), top.release.title
    if top.parsed.encoding == "lossless24":
        assert top.estimate and top.estimate["rate_khz"] <= 96, top.release.title
    if case.top_contains:
        assert case.top_contains in top.release.title
    if case.top_plain:
        blob = (top.parsed.remaster_title or "").lower() + " ".join(top.parsed.edition_flags)
        assert not any(w in blob for w in _EDITION_WORDS), top.release.title
    reasons = Counter(c.rejected_reason or "" for c in ranked if not c.accepted)
    for needle in case.rejected_reasons:
        assert any(needle in r for r in reasons), (needle, reasons.most_common(5))
    # A captured private-tracker fixture carries no key, tracker token or site URL.
    for r in rows:
        assert "REDACTED" in (r.get("downloadUrl") or "REDACTED")
        for link in ("guid", "infoUrl"):
            assert (r.get(link) or "https://tracker.example/").startswith(
                "https://tracker.example/"
            )
