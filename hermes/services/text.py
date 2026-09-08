"""Text normalisation and similarity for matching names across systems."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_ARTICLES = ("the ", "a ", "an ")
_AND = re.compile(r"\s*(&|\+|\band\b)\s*")
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize(value: str) -> str:
    """Casefold, strip accents and punctuation, unify '&'/'and', drop a leading article."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _AND.sub(" and ", text)
    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    for article in _ARTICLES:
        if text.startswith(article) and len(text) > len(article):
            text = text[len(article) :]
            break
    return text


def similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity of two names after normalisation."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        # Punctuation-only names ("( )", "†") normalise to nothing; compare them raw.
        ra, rb = a.casefold().strip(), b.casefold().strip()
        if not ra or not rb:
            return 0.0
        return fuzz.ratio(ra, rb) / 100.0
    return fuzz.ratio(na, nb) / 100.0
