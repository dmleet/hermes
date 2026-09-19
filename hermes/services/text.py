"""Text normalisation and similarity for matching names across systems."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_ARTICLES = ("the ", "a ", "an ")
_AND = re.compile(r"\s*(&|\+|\band\b)\s*")
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
# Typographic punctuation MusicBrainz's style guide mandates, folded to ASCII so the
# apostrophe rule below sees every spelling of it.
_TYPOGRAPHIC = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark (apostrophe)
        "\u201a": "'",  # single low-9 quotation mark
        "\u2032": "'",  # prime
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u2033": '"',  # double prime
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2026": "...",  # horizontal ellipsis
        "\u00a0": " ",  # no-break space
    }
)


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
    """The text as a tracker search query.

    Typographic punctuation is folded to ASCII and apostrophes become spaces, so
    "Deserter’s Songs" is queried as "Deserter s Songs". A Sphinx index (Gazelle) splits a
    title at a typographic apostrophe and blends a straight one, so the pieces are indexed
    either way and a query made of the pieces matches both spellings; a query carrying an
    apostrophe of either kind matched nothing (verified against a Gazelle tracker,
    2026-09-19). Case, accents and other punctuation are kept.
    """
    return _SPACES.sub(" ", value.translate(_TYPOGRAPHIC).replace("'", " ")).strip()


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
