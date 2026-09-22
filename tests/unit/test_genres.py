"""The genre display rule (services/genres.py): most votes first, a general genre dropped
for a more specific one that has at least half its votes."""

from __future__ import annotations

import pytest

from hermes.integrations.musicbrainz import Genre, ReleaseGroup
from hermes.services.genres import display, stored


def _top(*pairs: tuple[str, int]) -> dict:
    return {"top": [{"name": n, "count": c} for n, c in pairs]}


@pytest.mark.parametrize(
    ("top", "limit", "expected"),
    [
        # Interpol: "rock" outvotes "indie rock" but is contained in it.
        (
            _top(("rock", 8), ("indie rock", 6), ("post-punk revival", 3)),
            2,
            ["indie rock", "post-punk revival"],
        ),
        # Nine Inch Nails: "industrial" is contained in both specific genres.
        (
            _top(("industrial rock", 23), ("industrial", 15), ("industrial metal", 9)),
            2,
            ["industrial rock", "industrial metal"],
        ),
        # Steven Wilson: a tie between the general and the specific goes to the specific.
        (
            _top(("progressive rock", 4), ("rock", 4), ("alternative rock", 2)),
            2,
            ["progressive rock", "alternative rock"],
        ),
        # Air: nothing contains anything; plain top two.
        (
            _top(("downtempo", 18), ("electronic", 10), ("ambient", 2)),
            2,
            ["downtempo", "electronic"],
        ),
        # A heavily voted general genre survives one stray specific vote.
        (_top(("rock", 20), ("math rock", 1)), 2, ["rock", "math rock"]),
        # "electronic" is not a word of "electronica".
        (_top(("electronic", 11), ("electronica", 7)), 2, ["electronic", "electronica"]),
        # The detail page shows three.
        (
            _top(("rock", 8), ("indie rock", 6), ("post-punk revival", 3), ("dream pop", 2)),
            3,
            ["indie rock", "post-punk revival", "dream pop"],
        ),
        (None, 2, []),
        ({"top": []}, 2, []),
        ({"top": [{"name": "", "count": 3}]}, 2, []),
    ],
)
def test_display(top: dict | None, limit: int, expected: list[str]) -> None:
    assert display(top, limit) == expected


def test_stored_keeps_five_with_counts() -> None:
    rg = ReleaseGroup(
        id="rg",
        title="x",
        genres=[Genre(name=f"g{i}", count=10 - i) for i in range(7)],
    )
    kept = stored(rg)
    assert [g["name"] for g in kept["top"]] == ["g0", "g1", "g2", "g3", "g4"]
    assert kept["top"][0] == {"name": "g0", "count": 10}
    assert stored(ReleaseGroup(id="rg", title="x")) == {"top": []}
