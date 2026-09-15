from __future__ import annotations

from hermes.config import QualityPolicy
from hermes.integrations.prowlarr import ReleaseResult
from hermes.services.ranking import (
    ENCODING_WEIGHT,
    FREELEECH_TIEBREAK,
    LOG_CUE_BONUS,
    MEDIA_WEIGHT,
    SEEDERS_TIEBREAK,
    SIZE_TIEBREAK_PER_BYTE,
    evaluate,
)

TARGET = dict(artist="Radiohead", title="OK Computer", year=1997)


def _r(
    title: str,
    *,
    size: int = 400_000_000,
    seeders: int = 5,
    flags: list[str] | None = None,
    guid: str | None = None,
) -> ReleaseResult:
    return ReleaseResult(
        guid=guid or title,
        indexer_id=1,
        indexer="Tracker B",
        title=title,
        size=size,
        seeders=seeders,
        leechers=0,
        indexer_flags=flags or [],
    )


def _rank(policy: QualityPolicy | None = None, *releases: ReleaseResult):
    return evaluate(list(releases), policy=policy or QualityPolicy(), **TARGET)


def test_filters_reject_with_reasons() -> None:
    ranked = _rank(
        None,
        _r("Radiohead - OK Computer (1997) [Album] [MP3 320 / CD]"),
        _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / Vinyl]"),
        _r(
            "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", seeders=0, guid="noseed"
        ),
        _r("Radiohead - Kid A (2000) [Album] [FLAC Lossless / WEB]"),
        _r(
            "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]",
            size=3_000_000_000,
            guid="huge",
        ),
        _r(
            "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]",
            size=1_000_000,
            guid="tiny",
        ),
        _r("Radiohead - OK Computer Interview (1997) [Interview] [FLAC Lossless / WEB]"),
        _r("complete nonsense"),
    )
    reasons = {c.release.guid: c.rejected_reason for c in ranked}
    assert reasons["Radiohead - OK Computer (1997) [Album] [MP3 320 / CD]"] == "MP3 is not FLAC"
    assert "Vinyl" in reasons["Radiohead - OK Computer (1997) [Album] [FLAC Lossless / Vinyl]"]
    assert "0 seeders" in reasons["noseed"]
    assert "below" in reasons["Radiohead - Kid A (2000) [Album] [FLAC Lossless / WEB]"]
    assert "exceeds" in reasons["huge"]
    assert "too small" in reasons["tiny"]
    assert (
        "Interview"
        in reasons["Radiohead - OK Computer Interview (1997) [Interview] [FLAC Lossless / WEB]"]
    )
    assert reasons["complete nonsense"].startswith("unparsed")
    assert all(c.rank is None for c in ranked)


def test_ranking_order_reflects_policy() -> None:
    web16 = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]")
    web24 = _r("Radiohead - OK Computer (1997) [Album] [FLAC 24bit Lossless / WEB]")
    cd_log = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD / Log (100%) / Cue]")
    cd_nolog = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD]")
    free_cd = _r(
        "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD]",
        flags=["freeleech"],
        guid="free",
    )
    ranked = _rank(QualityPolicy(keep_candidates=10), cd_nolog, web24, cd_log, web16, free_cd)
    order = [c.release.guid for c in ranked if c.accepted]
    assert order[0] == web24.guid, "the best release wins; no flag can buy the top spot"
    assert order.index(web24.guid) < order.index(web16.guid), "24-bit preferred by default"
    assert order.index(web16.guid) < order.index(cd_log.guid), "WEB preferred over CD"
    assert order.index(cd_log.guid) < order.index(cd_nolog.guid), "log+cue beats bare CD"
    # free_cd is cd_nolog with the flag: it may break that tie and nothing more.
    assert order.index("free") < order.index(cd_nolog.guid), "freeleech breaks a tie"
    assert order.index(cd_log.guid) < order.index("free"), "freeleech does not cross a quality gap"
    assert [c.rank for c in ranked if c.accepted] == [1, 2, 3, 4, 5]


def test_tiebreaks_cannot_outweigh_quality() -> None:
    """The invariant behind the tie-break weights: seeders, freeleech and size order
    releases that are equally good and must never decide between releases that are not.
    Everything they can contribute together stays under half of the smallest quality
    difference the ranking can express. Lower a quality weight or raise a tie-break and
    this fails -- which is the point."""
    policy = QualityPolicy()
    tiebreaks = (
        SEEDERS_TIEBREAK + FREELEECH_TIEBREAK + policy.max_bytes_lossless24 * SIZE_TIEBREAK_PER_BYTE
    )
    smallest_quality_step = min(
        min(policy.edition_penalties.values()),  # remaster, 0.1 by default
        MEDIA_WEIGHT / len(policy.media_preference),  # one step of the media order
        ENCODING_WEIGHT / len(policy.encoding_preference),
        LOG_CUE_BONUS,
    )
    assert tiebreaks < smallest_quality_step / 2, (tiebreaks, smallest_quality_step)


def test_seeders_never_decide_between_different_quality() -> None:
    """Captured from tracker A: the OKNOTOK reissue of OK Computer has four times the
    seeders of the plain album, and used to win because of it. The remaster is the smallest
    quality difference the policy expresses, and even that outweighs every seeder."""
    plain = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", seeders=1)
    remastered = _r(
        "Radiohead - OK Computer (1997) [Album] [Remaster 2017] [FLAC Lossless / WEB]",
        seeders=500,
    )
    ranked = _rank(None, remastered, plain)
    assert [c.release.guid for c in ranked if c.accepted][0] == plain.guid


def test_plain_album_outranks_deluxe_and_anniversary_editions() -> None:
    """Captured from tracker A: WEB FLAC uploads of the plain album, the Deluxe Edition and
    the Super Deluxe box tied and the box won on seeders. The plain album wins; a plain CD
    rip beats a deluxe WEB release; a remaster costs almost nothing so a remastered WEB
    release with comparable seeders still beats a plain CD rip."""
    plain = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", seeders=40)
    deluxe = _r(
        "Radiohead - OK Computer (1997) [Album] [Deluxe Edition 2009] [FLAC Lossless / WEB]",
        seeders=50,
    )
    box = _r(
        "Radiohead - OK Computer (1997) [Album] [20th Anniversary Super Deluxe Box 2017] "
        "[FLAC Lossless / WEB]",
        seeders=80,
    )
    cd = _r(
        "Radiohead - OK Computer (1997) [Album] [FLAC Lossless / CD / Log (100%) / Cue]",
        seeders=40,
    )
    remaster = _r(
        "Radiohead - OK Computer (1997) [Album] [Remastered 2016] [FLAC Lossless / WEB]",
        seeders=40,
    )
    ranked = _rank(QualityPolicy(keep_candidates=5), deluxe, box, cd, plain, remaster)
    order = [c.release.title for c in ranked if c.rank]
    assert order[0] == plain.title
    assert order.index(remaster.title) < order.index(cd.title) < order.index(deluxe.title)
    assert order[-1] == box.title
    # Penalties are policy: switch them off and the box ties with the plain album again.
    ranked = _rank(QualityPolicy(edition_penalties={}), plain, deluxe, box)
    scores = {c.release.title: c.score for c in ranked}
    assert scores[box.title] == scores[plain.title] == scores[deluxe.title]


def test_sample_rate_is_inferred_from_size_and_playing_time() -> None:
    """Tracker titles say '24bit Lossless' and nothing about the rate. Size over the
    release's playing time puts a candidate in a band; the policy ceiling applies."""
    from hermes.services.ranking import rate_band, torrent_rate_verdict

    hour = 3_600_000  # ms
    web24 = "Radiohead - OK Computer (1997) [Album] [FLAC 24bit Lossless / WEB]"
    ninety_six = _r(web24, size=1_200_000_000, guid="96")  # ~2667 kbps
    one_ninety_two = _r(web24, size=2_400_000_000, guid="192")  # ~5333 kbps
    cd16 = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", size=400_000_000)
    stated = _r(
        "Radiohead - OK Computer (1997) [Album] [Hi-Res 24/192 Remaster 2016] "
        "[FLAC 24bit Lossless / WEB]",
        size=1_000_000_000,
        guid="stated",
    )
    ranked = evaluate(
        [ninety_six, one_ninety_two, cd16, stated],
        policy=QualityPolicy(max_bytes_lossless24=10**10),
        duration_ms=hour,
        **TARGET,
    )
    by = {c.release.guid: c for c in ranked}
    assert by["96"].accepted and by["96"].estimate == {
        "rate_khz": 96,
        "kbps": 2667,
        "source": "size",
    }
    assert "about 192 kHz" in (by["192"].rejected_reason or "")
    assert by[cd16.guid].accepted and by[cd16.guid].estimate["rate_khz"] == 48
    assert "title says 192 kHz" in (by["stated"].rejected_reason or "")
    # No playing time: nothing to infer, nothing rejected. No limit: nothing rejected.
    ranked = evaluate([one_ninety_two], policy=QualityPolicy(max_bytes_lossless24=10**10), **TARGET)
    assert ranked[0].accepted and ranked[0].estimate is None
    ranked = evaluate(
        [one_ninety_two],
        policy=QualityPolicy(max_bytes_lossless24=10**10, max_sample_rate_khz=None),
        duration_ms=hour,
        **TARGET,
    )
    assert ranked[0].accepted
    assert rate_band(900) == 48 and rate_band(1500) == 48 and rate_band(2800) == 96
    assert rate_band(5000) == 192 and rate_band(9000) > 192

    # After the fetch: per-file sizes against track lengths, median so an extra is ignored.
    ms = [240_000] * 10
    sizes_96 = [80_000_000] * 10  # 2667 kbps each
    sizes_192 = [160_000_000] * 10
    policy = QualityPolicy()
    assert torrent_rate_verdict(sizes_96, ms, policy) is None
    assert "about 192 kHz" in (torrent_rate_verdict(sizes_192, ms, policy) or "")
    assert torrent_rate_verdict(sizes_96 + [900_000_000], ms, policy) is None  # extra ignored
    assert torrent_rate_verdict(sizes_192, [], policy) is None  # no lengths: no verdict
    assert torrent_rate_verdict(sizes_192, ms, QualityPolicy(max_sample_rate_khz=192)) is None


def test_stated_sample_rates_are_read_from_titles() -> None:
    from hermes.services.title_parser import parse_title, stated_sample_rate

    assert stated_sample_rate("Hi-Res 24/96 Remaster") == 96
    assert stated_sample_rate("24bit-96kHz") == 96
    assert stated_sample_rate("192kHz") == 192 and stated_sample_rate("24/44.1") == 44
    assert stated_sample_rate("2019 Remaster") is None and stated_sample_rate("1996") is None
    t = "Tool - Lateralus (2001) [Album] [24bit/96kHz Remaster 2019] [FLAC 24bit Lossless / WEB]"
    assert parse_title(t).sample_rate_khz == 96


def test_edition_flags_without_a_penalty_key_count_as_reissues() -> None:
    """Review M9: 'Japan Edition' and 'Limited Edition' scored zero and suppressed the
    reissue fallback, so they outranked the plain album on seeders."""
    plain = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", seeders=3)
    japan = _r(
        "Radiohead - OK Computer (1997) [Album] [Japan Edition 2001] [FLAC Lossless / WEB]",
        seeders=60,
    )
    limited = _r(
        "Radiohead - OK Computer (1997) [Album] [Limited Edition 1997] [FLAC Lossless / WEB]",
        seeders=60,
    )
    ranked = _rank(None, japan, limited, plain)
    assert ranked[0].release.title == plain.title
    # A mix flag does not make an edition for the rate estimate (the plain playing time
    # applies), while a real edition is estimated from its file count.
    from hermes.services.ranking import estimate_rate
    from hermes.services.title_parser import parse_title

    stereo = _r(
        "Radiohead - OK Computer (1997) [Album] [Stereo Mix 1997] [FLAC 24bit Lossless / WEB]",
        size=1_200_000_000,
    ).model_copy(update={"files": 12})
    est = estimate_rate(stereo, parse_title(stereo.title), 3_600_000, 12)
    assert est and est["source"] == "size/files"  # a remaster title is present: edition
    mono_flag = _r(
        "Radiohead - OK Computer (Mono) (1997) [Album] [FLAC 24bit Lossless / WEB]",
        size=1_200_000_000,
    )
    est = estimate_rate(mono_flag, parse_title(mono_flag.title), 3_600_000, 12)
    assert est and est["source"] == "size" and est["rate_khz"] == 96


def test_partial_track_lengths_give_no_duration() -> None:
    """Review M10: a partial sum made a 24/96 release look like 24/192."""
    from hermes.services.search import complete_duration_ms

    assert complete_duration_ms([240_000] * 10) == 2_400_000
    assert complete_duration_ms([240_000] * 5 + [0] * 5) is None
    assert complete_duration_ms([]) is None


def test_search_time_rate_rejection_allows_ten_percent_for_artwork() -> None:
    hour = 3_600_000
    web24 = "Radiohead - OK Computer (1997) [Album] [FLAC 24bit Lossless / WEB]"
    dense = _r(web24, size=1_750_000_000)  # 3889 kbps: a dense 24/96 plus scans
    ranked = evaluate(
        [dense], policy=QualityPolicy(max_bytes_lossless24=10**10), duration_ms=hour, **TARGET
    )
    assert ranked[0].accepted, ranked[0].rejected_reason
    clear = _r(web24, size=2_000_000_000)  # 4444 kbps: over even with the margin
    ranked = evaluate(
        [clear], policy=QualityPolicy(max_bytes_lossless24=10**10), duration_ms=hour, **TARGET
    )
    assert "kHz" in (ranked[0].rejected_reason or "")


def test_keep_candidates_limits_the_fallback_list() -> None:
    releases = [
        _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]", guid=f"g{i}", seeders=i)
        for i in range(1, 6)
    ]
    ranked = _rank(QualityPolicy(keep_candidates=2), *releases)
    assert [c.rank for c in ranked[:2]] == [1, 2]
    assert all(c.rank is None and "beyond keep_candidates" in c.rejected_reason for c in ranked[2:])


def test_encoding_preference_can_favour_16_bit() -> None:
    policy = QualityPolicy(encoding_preference=["lossless", "lossless24"])
    web16 = _r("Radiohead - OK Computer (1997) [Album] [FLAC Lossless / WEB]")
    web24 = _r("Radiohead - OK Computer (1997) [Album] [FLAC 24bit Lossless / WEB]")
    ranked = _rank(policy, web16, web24)
    assert ranked[0].release.guid == web16.guid


def test_edition_rate_is_estimated_from_the_file_count() -> None:
    """OKNOTOK is a two-disc reissue: against the plain album's 53 minutes its 2 GB reads
    as 192 kHz; against 25 files at the album's mean track length it reads as 96 kHz."""
    reissue = ReleaseResult(
        guid="ok",
        indexer_id=1,
        indexer="Gazelle",
        size=2_093_000_000,
        seeders=16,
        files=25,
        title=(
            "Radiohead - OK Computer (1997) [Album] [OKNOTOK 1997-2017 2017] "
            "[FLAC 24bit Lossless / WEB]"
        ),
    )
    roomy = QualityPolicy(max_bytes_lossless24=10**10)
    hour53 = 53 * 60_000
    ranked = evaluate([reissue], policy=roomy, duration_ms=hour53, **TARGET)
    assert ranked[0].estimate is None  # no track count: nothing to say about an edition
    ranked = evaluate([reissue], policy=roomy, duration_ms=hour53, track_count=12, **TARGET)
    assert ranked[0].estimate["source"] == "size/files"
    assert ranked[0].estimate["rate_khz"] == 96 and ranked[0].accepted
    big = reissue.model_copy(update={"size": 4_500_000_000})
    ranked = evaluate([big], policy=roomy, duration_ms=hour53, track_count=12, **TARGET)
    assert "about 192 kHz" in (ranked[0].rejected_reason or "")


def test_unknown_media_is_neutral_not_rejected() -> None:
    pandacd = _r("Radiohead - OK Computer [1997] [FLAC Lossless]")
    (c,) = _rank(None, pandacd)
    assert c.accepted and c.rank == 1 and c.parsed.media is None
