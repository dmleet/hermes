from __future__ import annotations

import pytest

from hermes.services.text import normalize, similarity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Beatles", "beatles"),
        ("Belle & Sebastian", "belle and sebastian"),
        ("Belle and Sebastian", "belle and sebastian"),
        ("Sigur Rós", "sigur ros"),
        ("OK Computer: Special", "ok computer special"),
        ("  A   Moon Shaped Pool ", "moon shaped pool"),
        ("A", "a"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_similarity_is_forgiving_about_punctuation_and_case() -> None:
    assert similarity("Radiohead / Friends", "radiohead friends") > 0.95
    assert similarity("Dummy", "Dummy / Portishead") < 0.9
    assert similarity("", "x") == 0.0
