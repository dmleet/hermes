"""Text normalisation and similarity for matching names across systems."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_ARTICLES = ("the ", "a ", "an ")
_AND = re.compile(r"\s*(&|\+|\band\b)\s*")
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
# Words a release matcher ignores on both sides (Lidarr's list); a search index requires
# every query word, so sending them can only lose matches and dropping them only widens.
_NOISE_WORDS = frozenset({"a", "an", "the", "and", "or", "of"})


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


def search_form(value: str) -> str:
    """The text as a tracker search query: the words its index can require, nothing else.

    Punctuation of every kind becomes a space and the noise words go; case and accents are
    kept. Measured against a Gazelle tracker (2026-09-19): its Sphinx index splits a title
    at a typographic apostrophe and blends a straight one, so the pieces are indexed under
    either spelling while a query carrying an apostrophe matched nothing ("Deserter s
    Songs" found all editions, "Deserter's"/"Deserter’s"/"Deserters" none); "Belle and
    Sebastian" found nothing where "Belle Sebastian" did, because the tracker spells it
    "&" and Prowlarr strips that; accents removed from the query found nothing where the
    accented spelling did. If nothing but noise words remain they are kept, so a title
    such as "A" still contributes.
    """
    text = _SPACES.sub(" ", _PUNCT.sub(" ", value)).strip()
    words = [w for w in text.split(" ") if w and w.casefold() not in _NOISE_WORDS]
    return " ".join(words) or text


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
