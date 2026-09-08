"""Filter and rank search results for one album target (docs/plan.md 5.5).

Hard filters reject with a reason that is kept on the candidate so the UI can show why.
Survivors get a rank from a weighted score: freeleech, media and encoding preference,
log/cue for CD rips, seeders, and the match score. Weights are deliberately simple; the
policy decides the preference orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes.config import QualityPolicy
from hermes.integrations.prowlarr import ReleaseResult
from hermes.services.matching import MatchResult, match
from hermes.services.title_parser import ParsedTitle, parse_title

MIN_BYTES = 5_000_000  # below this it is not an album

# Tracker release types -> the MusicBrainz primary/secondary type they correspond to.
_TYPE_TO_MB = {
    "Album": "Album",
    "EP": "EP",
    "Single": "Single",
    "Live album": "Live",
    "Compilation": "Compilation",
    "Anthology": "Compilation",
    "Soundtrack": "Soundtrack",
    "Remix": "Remix",
    "DJ Mix": "DJ-mix",
    "Interview": "Interview",
    "Demo": "Demo",
    "Mixtape": "Mixtape/Street",
    "Bootleg": "Bootleg",
    "Concert Recording": "Live",
}


# FLAC bitrate ceilings (kbps) per sample-rate band, stereo. 16/44.1 sits under 1000,
# 24/44.1 and 24/48 under about 1700, 24/88.2 and 24/96 under about 3300, 24/176.4 and
# 24/192 under about 6000. The boundaries sit in the gaps between bands.
_BANDS: tuple[tuple[float, int], ...] = ((1900.0, 48), (3700.0, 96), (7500.0, 192))


def rate_band(kbps: float) -> int:
    """The highest plausible sample rate (kHz) for a lossless stream of this bitrate."""
    for ceiling, khz in _BANDS:
        if kbps <= ceiling:
            return khz
    return 384


def _khz_label(khz: int) -> str:
    return "over 192 kHz (multichannel or DSD)" if khz > 192 else f"{khz} kHz"


def _rate_allowed(policy: QualityPolicy, khz: int) -> bool:
    limit = policy.max_sample_rate_khz
    return limit is None or khz <= rate_band_of_limit(limit)


def rate_band_of_limit(limit_khz: int) -> int:
    """44 and 48 are one band, as are 88/96 and 176/192."""
    return rate_band(next(c for c, k in _BANDS if k >= min(limit_khz, 192)) - 1)


def estimate_rate(
    release: ReleaseResult, parsed: ParsedTitle, duration_ms: int | None, track_count: int = 0
) -> dict[str, Any] | None:
    """What sample rate a lossless release probably has. A rate in the title wins;
    otherwise size over playing time gives the bitrate and the band it falls in. The
    playing time is the plain album's; an edition or reissue has a different one, so for
    those the tracker's file count times the album's mean track length stands in (extras
    among the files only stretch the time and lower the estimate, never raise it)."""
    if parsed.encoding not in ("lossless", "lossless24"):
        return None
    plain = not parsed.remaster_title and not (set(parsed.edition_flags) - _MIX_FLAGS)
    seconds = None
    if duration_ms and plain:
        seconds = duration_ms / 1000
    elif duration_ms and track_count and release.files:
        seconds = duration_ms / 1000 / track_count * release.files
    kbps = None
    if release.size and seconds:
        kbps = release.size * 8 / seconds / 1000
    if parsed.sample_rate_khz:
        return {
            "rate_khz": parsed.sample_rate_khz,
            "kbps": round(kbps) if kbps else None,
            "source": "title",
        }
    if kbps is None:
        return None
    source = "size" if plain else "size/files"
    return {"rate_khz": rate_band(kbps), "kbps": round(kbps), "source": source}


def torrent_rate_verdict(
    audio_sizes: list[int], track_ms: list[int], policy: QualityPolicy
) -> str | None:
    """After the torrent is fetched: the same check per file. With one size per track the
    median per-track bitrate is used, which shrugs off a bundled extra; otherwise the
    total. Returns a reason to skip, or None."""
    if policy.max_sample_rate_khz is None or not audio_sizes:
        return None
    lengths = [ms for ms in track_ms if ms > 0]
    if not lengths or len(lengths) != len(track_ms):
        return None  # lengths unknown or incomplete: no verdict
    if len(lengths) == len(audio_sizes):
        rates = sorted(
            size * 8 / (ms / 1000) / 1000 for size, ms in zip(audio_sizes, lengths, strict=True)
        )
        how = "median track"
    else:
        # A different track count (an extra file, a bonus track): rate each file against
        # the release's mean track length and take the median, so one big extra is ignored.
        mean_ms = sum(lengths) / len(lengths)
        rates = sorted(size * 8 / (mean_ms / 1000) / 1000 for size in audio_sizes)
        how = "median file at the mean track length"
    kbps = rates[len(rates) // 2]
    khz = rate_band(kbps)
    if _rate_allowed(policy, khz):
        return None
    return (
        f"about {_khz_label(khz)} ({kbps:.0f} kbps by {how}) exceeds "
        f"quality.max_sample_rate_khz {policy.max_sample_rate_khz}"
    )


@dataclass
class RankedCandidate:
    release: ReleaseResult
    parsed: ParsedTitle
    match: MatchResult
    score: float = 0.0
    rank: int | None = None
    rejected_reason: str | None = None
    estimate: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected_reason is None


def _preference_bonus(value: str | None, preference: list[str], weight: float) -> float:
    if not preference:
        return 0.0
    if value is None:
        return weight * 0.5  # unknown is neither preferred nor penalised
    lowered = [p.lower() for p in preference]
    if value.lower() not in lowered:
        return 0.0
    position = lowered.index(value.lower())
    return weight * (len(preference) - position) / len(preference)


def type_mismatch(release_type: str | None, target_types: set[str]) -> str | None:
    """A title-track Single or a live album must not stand in for the studio Album.
    ``target_types`` is the MusicBrainz primary type plus secondary types of the target."""
    if not release_type or not target_types:
        return None
    expected = _TYPE_TO_MB.get(release_type, release_type)
    if expected in target_types:
        return None
    return (
        f"release type {release_type} does not match the target ({', '.join(sorted(target_types))})"
    )


def rejection(
    c: RankedCandidate, policy: QualityPolicy, target_types: set[str] | None = None
) -> str | None:
    p, r = c.parsed, c.release
    if not p.ok:
        return "unparsed title: " + "; ".join(p.problems or ["unknown"])
    if policy.require_format and (p.format or "").lower() != policy.require_format.lower():
        return f"{p.format} is not {policy.require_format}"
    if p.media and p.media.lower() in (m.lower() for m in policy.excluded_media):
        return f"media {p.media} is excluded"
    if (r.seeders or 0) < policy.min_seeders:
        return f"{r.seeders or 0} seeders (< {policy.min_seeders})"
    allowed_types = {t.casefold() for t in policy.allowed_release_types}
    if p.release_type and p.release_type.casefold() not in allowed_types:
        return f"release type {p.release_type} is not allowed"
    mismatch = type_mismatch(p.release_type, target_types or set())
    if mismatch:
        return mismatch
    if c.match.score < policy.match_threshold:
        return f"match {c.match.score:.2f} below {policy.match_threshold}"
    # Only a 24-bit release can exceed the limit; a 16-bit estimate is shown, not enforced
    # (a box set or a bundled video can inflate it).
    if c.estimate and policy.max_sample_rate_khz is not None and p.encoding == "lossless24":
        khz = int(c.estimate["rate_khz"])
        if c.estimate["source"] != "title" and c.estimate.get("kbps"):
            # Size estimates carry artwork and logs; give them ten percent before refusing.
            # The per-file check at submit is the exact one.
            khz = rate_band(float(c.estimate["kbps"]) * 0.9)
        if not _rate_allowed(policy, khz):
            said = "title says" if c.estimate["source"] == "title" else "about"
            kbps = c.estimate.get("kbps")
            detail = f" ({kbps} kbps)" if kbps else ""
            limit = policy.max_sample_rate_khz
            return f"{said} {_khz_label(khz)}{detail}, above quality.max_sample_rate_khz {limit}"
    if r.size is not None:
        cap = (
            policy.max_bytes_lossless24 if p.encoding == "lossless24" else policy.max_bytes_lossless
        )
        if r.size > cap:
            return f"size {r.size / 1e9:.2f} GB exceeds {cap / 1e9:.2f} GB"
        if r.size < MIN_BYTES:
            return f"size {r.size / 1e6:.1f} MB is too small for an album"
    return None


def edition_penalty(p: ParsedTitle, policy: QualityPolicy) -> float:
    """The largest policy penalty among the title's edition flags. A flag the policy does
    not name ("japan", "limited edition") and any other reissue label (a remaster title
    with no recognised word: "OKNOTOK 1997 2017", "EU Repress") count as "reissue"."""
    reissue = policy.edition_penalties.get("reissue", 0.0)
    penalties = [policy.edition_penalties.get(flag, reissue) for flag in p.edition_flags]
    if p.remaster_title and not p.edition_flags:
        penalties.append(reissue)
    return max(penalties, default=0.0)


# Flags that name a mix, not a different tracklist: the plain album's playing time still
# applies to them when estimating a sample rate.
_MIX_FLAGS = frozenset({"mono", "stereo", "explicit"})


def rank_score(c: RankedCandidate, policy: QualityPolicy) -> float:
    p, r = c.parsed, c.release
    score = 2.0 * c.match.score
    score -= edition_penalty(p, policy)
    if r.freeleech:
        score += 3.0
    score += _preference_bonus(p.media, policy.media_preference, 2.0)
    score += _preference_bonus(p.encoding, policy.encoding_preference, 1.5)
    score += _preference_bonus(r.indexer, policy.indexer_preference, 1.0)
    if p.media == "CD" and p.log_score == 100 and p.has_cue:
        score += 0.25  # separates CD rips from each other; never outweighs media preference
    # Seeders matter once min_seeders is met, but less than log/cue and the preferences.
    score += min(r.seeders or 0, 10) / 10.0 * 0.2
    score -= (r.size or 0) / 1e12  # tie-break: smaller first
    return score


def evaluate(
    releases: list[ReleaseResult],
    *,
    artist: str,
    title: str,
    year: int | None,
    policy: QualityPolicy,
    target_types: set[str] | None = None,
    duration_ms: int | None = None,
    track_count: int = 0,
) -> list[RankedCandidate]:
    """Parse, match, filter and rank. Returns every release; the top ``keep_candidates``
    accepted ones carry a rank (those are the fallbacks submit may try), the rest of the
    accepted ones are marked as beyond the keep limit."""
    out: list[RankedCandidate] = []
    for r in releases:
        parsed = parse_title(r.title)
        c = RankedCandidate(r, parsed, match(parsed, artist=artist, title=title, year=year))
        c.estimate = estimate_rate(r, parsed, duration_ms, track_count)
        c.rejected_reason = rejection(c, policy, target_types)
        c.score = rank_score(c, policy) if c.accepted else 0.0
        out.append(c)
    accepted = sorted((c for c in out if c.accepted), key=lambda c: -c.score)
    for i, c in enumerate(accepted, start=1):
        if i <= policy.keep_candidates:
            c.rank = i
        else:
            c.rejected_reason = f"acceptable, but beyond keep_candidates ({policy.keep_candidates})"
    rejected = [c for c in out if not c.accepted]
    return accepted + rejected
