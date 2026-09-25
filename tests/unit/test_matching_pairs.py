"""The matching corpus: a target as MusicBrainz names it against a listing as a tracker
titles it, with the verdict the matcher must reach (tests/fixtures/matching/pairs.yaml).

Real rows come from acquisitions a human approved or imported; invented rows stand for
one mechanism each. A row with `gap` is a verdict the matcher does not reach yet, kept as
a strict expected failure: a fix turns it green and must then drop the mark, and a
regression on any other row is loud. The corpus judges matching and the query sequence
alone; what a tracker's index answers is measured through the dev Prowlarr, not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes.config import Policy
from hermes.services.matching import match
from hermes.services.search import search_queries
from hermes.services.title_parser import parse_title

PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "matching" / "pairs.yaml"
THRESHOLD = Policy().quality.match_threshold
FIELDS = {"artist", "title", "year", "listing", "expect", "why", "queries", "gap", "query_gap"}


def _load() -> list[dict[str, Any]]:
    rows = yaml.safe_load(PAIRS.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows
    return rows


def _params(rows: list[dict[str, Any]], gap: str = "gap") -> list[pytest.ParameterSet]:
    out = []
    for row in rows:
        listing = str(row["listing"]).split(" [", 1)[0]
        marks = [pytest.mark.xfail(reason=str(row[gap]), strict=True)] if row.get(gap) else []
        ident = f"{row['artist']} - {row['title']} | {listing}"
        out.append(pytest.param(row, id=ident, marks=marks))
    return out


ROWS = _load()


def test_corpus_rows_are_well_formed() -> None:
    seen: set[tuple[Any, ...]] = set()
    for row in ROWS:
        assert set(row) <= FIELDS, row
        assert {"artist", "title", "year", "listing", "expect"} <= set(row), row
        assert row["expect"] in {"accept", "reject"}, row
        assert isinstance(row["year"], int), row
        key = (row["artist"], row["title"], row["listing"])
        assert key not in seen, f"duplicate row: {key}"
        seen.add(key)


@pytest.mark.parametrize("row", _params(ROWS))
def test_verdict(row: dict[str, Any]) -> None:
    m = match(
        parse_title(str(row["listing"])),
        artist=str(row["artist"]),
        title=str(row["title"]),
        year=int(row["year"]),
    )
    accepted = m.score >= THRESHOLD
    assert accepted == (row["expect"] == "accept"), (row.get("why"), m.as_dict())


@pytest.mark.parametrize("row", _params([r for r in ROWS if r.get("queries")], gap="query_gap"))
def test_queries(row: dict[str, Any]) -> None:
    """The Prowlarr queries the search would try for the target, in order."""
    artist = str(row["artist"])
    steps = search_queries(
        artist, str(row["title"]), various_artists=artist.casefold() == "various artists"
    )
    assert [q for _, q in steps] == row["queries"], steps
