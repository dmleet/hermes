"""Resolve a request into a MusicBrainz release group (docs/plan.md 5.2).

Manual requests arrive as artist + title or as an MBID. The outcome is one of:
resolved (a release group the policy accepts, unambiguously), needs_review (candidates
exist but none is a confident, policy-clean winner), or not_found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hermes.config import ResolutionPolicy
from hermes.integrations.musicbrainz import (
    MusicBrainzClient,
    NotFound,
    Recording,
    ReleaseGroup,
)
from hermes.services.text import normalize, similarity

_TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}
_MIN_MB_SCORE = 90
_AMBIGUITY_MARGIN = 0.05


@dataclass
class Candidate:
    release_group: ReleaseGroup
    artist_similarity: float
    title_similarity: float
    score: float
    policy_ok: bool
    rejected_reason: str | None = None

    def confident(self, policy: ResolutionPolicy) -> bool:
        return (
            self.artist_similarity >= policy.min_similarity
            and self.title_similarity >= policy.min_similarity
            and (self.release_group.score or 0) >= _MIN_MB_SCORE
        )

    def as_dict(self) -> dict[str, Any]:
        rg = self.release_group
        return {
            "release_group_mbid": rg.id,
            "artist": rg.artist_name,
            "title": rg.title,
            "primary_type": rg.primary_type,
            "secondary_types": rg.secondary_types,
            "first_release_date": rg.first_release_date,
            "mb_score": rg.score,
            "artist_similarity": round(self.artist_similarity, 3),
            "title_similarity": round(self.title_similarity, 3),
            "score": round(self.score, 3),
            "policy_ok": self.policy_ok,
            "rejected_reason": self.rejected_reason,
        }


@dataclass
class Resolution:
    outcome: Literal["resolved", "needs_review", "not_found", "nothing_acquirable"]
    target: ReleaseGroup | None = None
    preferred_release_mbid: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    note: str = ""


def policy_rejection(rg: ReleaseGroup, policy: ResolutionPolicy) -> str | None:
    """Why the policy refuses this release group, or None if it is acceptable."""
    ptype = rg.primary_type
    if ptype == "EP" and not policy.allow_ep:
        return "EP (resolution.allow_ep is off)"
    if ptype == "Single" and not policy.allow_single:
        return "Single (resolution.allow_single is off)"
    if ptype not in ("Album", "EP", "Single"):
        return f"primary type {ptype or 'unknown'} is not acquirable"
    allowed = {t.casefold() for t in policy.allow_secondary_types}
    disallowed = [t for t in rg.secondary_types if t.casefold() not in allowed]
    if disallowed:
        return f"secondary type {', '.join(disallowed)} (not in resolution.allow_secondary_types)"
    return None


def score_candidates(
    results: list[ReleaseGroup], artist: str, title: str, policy: ResolutionPolicy
) -> list[Candidate]:
    out = []
    for rg in results:
        a_sim = similarity(artist, rg.artist_name)
        t_sim = similarity(title, rg.title)
        mb = (rg.score or 0) / 100.0
        rejection = policy_rejection(rg, policy)
        out.append(
            Candidate(
                release_group=rg,
                artist_similarity=a_sim,
                title_similarity=t_sim,
                score=0.5 * mb + 0.25 * a_sim + 0.25 * t_sim,
                policy_ok=rejection is None,
                rejected_reason=rejection,
            )
        )
    # Confident matches first, then Album before EP before Single: a confident EP must not
    # be hidden behind a weak Album that merely shares words with the request.
    out.sort(
        key=lambda c: (
            not c.policy_ok,
            not c.confident(policy),
            _TYPE_RANK.get(c.release_group.primary_type or "", 9),
            -c.score,
        )
    )
    return out


def choose(candidates: list[Candidate], policy: ResolutionPolicy) -> Resolution:
    if not candidates:
        return Resolution("not_found", note="MusicBrainz returned no release groups")
    acceptable = [c for c in candidates if c.policy_ok]
    if not acceptable:
        return Resolution(
            "needs_review", candidates=candidates, note="every candidate is rejected by policy"
        )
    best = acceptable[0]
    if not best.confident(policy):
        return Resolution(
            "needs_review",
            candidates=candidates,
            note=(
                f"best candidate is not a confident match "
                f"(artist {best.artist_similarity:.2f}, title {best.title_similarity:.2f}, "
                f"mb {best.release_group.score})"
            ),
        )
    rivals = [
        c
        for c in acceptable[1:]
        if c.release_group.primary_type == best.release_group.primary_type
        and c.artist_similarity >= policy.min_similarity
        and c.title_similarity >= policy.min_similarity
        and best.score - c.score < _AMBIGUITY_MARGIN
    ]
    if rivals:
        return Resolution(
            "needs_review",
            candidates=candidates,
            note=f"{len(rivals) + 1} {best.release_group.primary_type}s match equally well",
        )
    return Resolution("resolved", target=best.release_group, candidates=candidates)


async def resolve_by_name(
    mb: MusicBrainzClient, policy: ResolutionPolicy, artist: str, title: str
) -> Resolution:
    results = await mb.search_release_groups(artist, title, limit=policy.search_limit)
    if not results:
        # The exact phrase missed (e.g. "Your" vs "Yr."). Let MusicBrainz match on terms and
        # rely on similarity scoring to accept or send to review.
        results = await mb.search_release_groups(
            artist, title, limit=policy.search_limit, loose=True
        )
    return choose(score_candidates(results, artist, title, policy), policy)


def _credited_to_recording_artist(
    recording: Recording, rg: ReleaseGroup, policy: ResolutionPolicy
) -> bool:
    """Is the release group the recording's artist's own? Every artist in the group's credit
    must appear in the recording's credit (by MBID, else by name). A track credited
    "Gorillaz feat. Little Dragon" on an album credited "Gorillaz" passes; a various-artists
    compilation or a DJ mix does not. Whole-string similarity is the fallback when the
    credits carry no structure."""
    if not rg.artist_credits or not recording.artist_credits:
        return similarity(recording.artist_name, rg.artist_name) >= policy.min_similarity
    rec_ids = {c.mbid for c in recording.artist_credits if c.mbid}
    rec_names = {normalize(c.name) for c in recording.artist_credits}
    for credit in rg.artist_credits:
        if credit.mbid and credit.mbid in rec_ids:
            continue
        if normalize(credit.name) in rec_names:
            continue
        return similarity(recording.artist_name, rg.artist_name) >= policy.min_similarity
    return True


def choose_for_recording(
    recording: Recording, policy: ResolutionPolicy, *, when: str = ""
) -> Resolution:
    """Which release group a discovered recording stands for (docs/plan.md 5.2). The groups are
    facts linked to the recording, not search hits, so there is no confidence question:
    among the policy-acceptable groups take Album before EP before Single, earliest first
    release first. A group whose artist credit is not the recording's (a various-artists
    compilation, a DJ mix) is rejected. Nothing acceptable is not a review case for an
    automated signal: the signal records why and no acquisition is created."""
    groups = recording.release_groups()
    if not groups:
        return Resolution("not_found", note="the recording is on no release in MusicBrainz")
    candidates: list[Candidate] = []
    for rg in groups:
        a_sim = similarity(recording.artist_name, rg.artist_name)
        rejection = policy_rejection(rg, policy)
        if rejection is None and not _credited_to_recording_artist(recording, rg, policy):
            rejection = f"credited to {rg.artist_name!r}, not the recording's artist"
        candidates.append(
            Candidate(
                release_group=rg,
                artist_similarity=a_sim,
                title_similarity=1.0,
                score=a_sim,
                policy_ok=rejection is None,
                rejected_reason=rejection,
            )
        )
    candidates.sort(
        key=lambda c: (
            not c.policy_ok,
            _TYPE_RANK.get(c.release_group.primary_type or "", 9),
            c.release_group.first_release_date or "9999",
        )
    )
    best = candidates[0]
    if not best.policy_ok:
        reasons = "; ".join(f"{c.release_group.title}: {c.rejected_reason}" for c in candidates)
        return Resolution(
            "nothing_acquirable",
            candidates=candidates,
            note=f"no acquirable release group for {recording.title!r} ({reasons})",
        )
    return Resolution("resolved", target=best.release_group, candidates=candidates)


async def resolve_recording(
    mb: MusicBrainzClient, policy: ResolutionPolicy, recording_mbid: str
) -> Resolution:
    try:
        recording = await mb.recording(recording_mbid)
    except NotFound:
        return Resolution("not_found", note=f"recording {recording_mbid} is not in MusicBrainz")
    return choose_for_recording(recording, policy)


async def resolve_by_mbid(mb: MusicBrainzClient, policy: ResolutionPolicy, mbid: str) -> Resolution:
    """An MBID may name a release group or a specific release; accept either."""
    try:
        rg = await mb.release_group(mbid)
        preferred = None
    except NotFound:
        try:
            release = await mb.release(mbid)
        except NotFound:
            return Resolution("not_found", note=f"{mbid} is neither a release group nor a release")
        rg = await mb.release_group(release.release_group.id)
        preferred = release.id
    rejection = policy_rejection(rg, policy)
    candidate = Candidate(rg, 1.0, 1.0, 1.0, rejection is None, rejection)
    if rejection:
        return Resolution(
            "needs_review",
            candidates=[candidate],
            note=f"explicit MBID rejected by policy: {rejection}",
        )
    return Resolution(
        "resolved", target=rg, preferred_release_mbid=preferred, candidates=[candidate]
    )
