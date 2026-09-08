from __future__ import annotations

from hermes.config import ResolutionPolicy
from hermes.integrations.musicbrainz import parse_release_group
from hermes.services.resolution import choose, policy_rejection, score_candidates
from tests.fixtures import load


def _results(name: str):
    return [parse_release_group(r) for r in load(f"musicbrainz/{name}")["release-groups"]]


def test_dummy_resolves_and_compilation_is_rejected() -> None:
    policy = ResolutionPolicy()
    candidates = score_candidates(_results("search_dummy"), "Portishead", "Dummy", policy)
    res = choose(candidates, policy)
    assert res.outcome == "resolved"
    assert res.target and res.target.id == "48140466-cff6-3222-bd55-63c27e43190d"
    assert candidates[1].policy_ok is False and "Compilation" in (
        candidates[1].rejected_reason or ""
    )


def test_random_access_memories_ignores_remix_and_interview_editions() -> None:
    policy = ResolutionPolicy()
    res = choose(
        score_candidates(_results("search_ram"), "Daft Punk", "Random Access Memories", policy),
        policy,
    )
    assert res.outcome == "resolved"
    assert res.target and res.target.id == "aa997ea0-2936-40bd-884d-3af8a0e064dc"
    rejected = {c.release_group.title: c.rejected_reason for c in res.candidates if not c.policy_ok}
    assert "Random Access Memories: The Collaborators" in rejected
    assert "Random Access Memories (Vanderway Edit)" in rejected


def test_ep_follows_policy() -> None:
    results = _results("search_rival_dealer")
    assert (
        choose(
            score_candidates(results, "Burial", "Rival Dealer", ResolutionPolicy(allow_ep=True)),
            ResolutionPolicy(),
        ).outcome
        == "resolved"
    )
    strict = ResolutionPolicy(allow_ep=False)
    res = choose(score_candidates(results, "Burial", "Rival Dealer", strict), strict)
    assert res.outcome == "needs_review" and "rejected by policy" in res.note


def test_nothing_found() -> None:
    policy = ResolutionPolicy()
    assert (
        choose(score_candidates(_results("search_nothing"), "x", "y", policy), policy).outcome
        == "not_found"
    )


def test_low_similarity_needs_review() -> None:
    policy = ResolutionPolicy()
    candidates = score_candidates(_results("search_dummy"), "Portishead", "Third", policy)
    res = choose(candidates, policy)
    assert res.outcome == "needs_review" and "not a confident match" in res.note


def test_two_equal_albums_are_ambiguous() -> None:
    policy = ResolutionPolicy()
    raw = load("musicbrainz/search_dummy")["release-groups"][0]
    twin = {**raw, "id": "11111111-1111-1111-1111-111111111111", "disambiguation": "2019 reissue"}
    candidates = score_candidates(
        [parse_release_group(raw), parse_release_group(twin)], "Portishead", "Dummy", policy
    )
    res = choose(candidates, policy)
    assert res.outcome == "needs_review" and "match equally well" in res.note


def test_secondary_types_can_be_allowed() -> None:
    rg = parse_release_group(load("musicbrainz/search_dummy")["release-groups"][1])
    assert policy_rejection(rg, ResolutionPolicy()) is not None
    assert policy_rejection(rg, ResolutionPolicy(allow_secondary_types=["Compilation"])) is None
