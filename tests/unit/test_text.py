from __future__ import annotations

import pytest

from hermes.services.text import normalize, search_form, similarity


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mercury Rev Deserter’s Songs", "Mercury Rev Deserter s Songs"),
        ("Don't Look Back", "Don t Look Back"),
        ("“Heroes”", '"Heroes"'),
        ("Sigur Rós – ( )", "Sigur Rós - ( )"),
        ("Is This It…", "Is This It..."),
        ("  Belle & Sebastian ", "Belle & Sebastian"),
    ],
)
def test_search_form_folds_punctuation_and_splits_apostrophes(raw: str, expected: str) -> None:
    assert search_form(raw) == expected
