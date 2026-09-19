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
        ("David Bowie “Heroes”", "David Bowie Heroes"),
        ("Sigur Rós – ( )", "Sigur Rós"),  # accents stay: the tracker keeps them
        ("The Strokes Is This It…", "Strokes Is This It"),
        ("  Belle and Sebastian Tigermilk ", "Belle Sebastian Tigermilk"),
        ("Belle & Sebastian", "Belle Sebastian"),
        ("Metallica …And Justice for All", "Metallica Justice for All"),
        ("Jethro Tull A", "Jethro Tull"),
        ("A", "A"),  # only noise words: keep them rather than send nothing
        ("The The", "The The"),
        ("AC/DC Back in Black", "AC DC Back in Black"),
        ("808s & Heartbreak", "808s Heartbreak"),
    ],
)
def test_search_form_keeps_only_words_a_search_index_can_require(raw: str, expected: str) -> None:
    assert search_form(raw) == expected
