"""Does a parsed release title describe the album target? (docs/plan.md 5.5, match score)"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hermes.services.text import similarity
from hermes.services.title_parser import EDITION_WORDS, ParsedTitle

_EDITION_PAREN = re.compile(
    r"\s*[\(\[][^\)\]]*(?:"
    + "|".join(re.escape(w) for w in EDITION_WORDS)
    + r"|edition|version)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


@dataclass
class MatchResult:
    score: float
    artist_similarity: float
    title_similarity: float
    year_ok: bool | None  # None when the title carries no year
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 3),
            "artist_similarity": round(self.artist_similarity, 3),
            "title_similarity": round(self.title_similarity, 3),
            "year_ok": self.year_ok,
            "notes": self.notes,
        }


def strip_edition(album: str) -> str:
    """'OK Computer (Deluxe Edition)' -> 'OK Computer'."""
    return _EDITION_PAREN.sub("", album).strip()


_SUBTITLE = re.compile(r"\s*[:\u2013\u2014-]\s+.*$")
_SOUNDTRACK_TAIL = re.compile(
    r"\s*[\(\[]?\b(?:original (?:motion picture|movie|video game|television|game)? ?"
    r"(?:soundtrack|score)|soundtrack|ost|music from the (?:motion picture|film|series))"
    r"\b[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE,
)


_PAREN = re.compile(r"\s*[\(\[]([^\)\]]+)[\)\]]")


_PERFORMER_TAIL = re.compile(r"\s+(?:performed by|feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def artist_variants(artist: str) -> list[str]:
    """The artist credit, and the credit without a "performed by" or "feat." tail, which
    trackers add to the head of a title and MusicBrainz does not."""
    head = _PERFORMER_TAIL.sub("", artist).strip()
    return [artist] if head == artist or not head else [artist, head]


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}  # fmt: skip
_NUMBER_WORD = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
_DIGITS = re.compile(r"\d+")


def _split_tail(title: str) -> tuple[str, str | None, str | None]:
    """Head, tail and the tail's kind: a soundtrack label, a parenthesised alternative
    title, or a plain subtitle after ':' or ' - '."""
    m = _SOUNDTRACK_TAIL.search(title)
    if m and m.start() > 0:
        return title[: m.start()].strip(" :-\u2013\u2014"), m.group(0).strip(" ()[]"), "soundtrack"
    m = _PAREN.search(title)
    if m and m.start() > 0:
        head = (title[: m.start()] + title[m.end() :]).strip(" :-\u2013\u2014")
        return head, m.group(1).strip(), "alt"
    m = _SUBTITLE.search(title)
    if m and m.start() > 0:
        return title[: m.start()].strip(), m.group(0).strip(" :-\u2013\u2014"), "subtitle"
    return title, None, None


def title_head(title: str) -> str | None:
    """The title before its subtitle, soundtrack label or parenthesised alternative, or
    None when it has none: the words a tracker's listing keeps when it drops the tail
    ("Anthology" for "Anthology: 25 Years"). A query made of the head finds what
    `title_similarity` then judges by the same split."""
    head, tail, _ = _split_tail(title)
    return head if tail else None


def as_digits(text: str) -> str:
    """Number words and Roman numerals up to ten as digits ("Volume II" -> "Volume 2",
    "Day One" -> "Day 1"), so a number reads the same however a title spells it. The
    pronoun "I" becomes "1" too, on both sides alike, which changes nothing between them."""
    return _NUMBER_WORD.sub(lambda m: str(_WORD_NUMBERS[m.group(0).lower()]), text)


def _numbers(text: str) -> list[int]:
    return sorted(int(n) for n in _DIGITS.findall(as_digits(text)))


def _similar(a: str, b: str) -> float:
    """Title similarity with the numbers compared exactly: "Album" is not "Album II",
    "Day One" is not "Day Two" and "Pt. 1" is not "Pt. 2", however alike the words are,
    while "Volume II" is "Volume 2"."""
    if _numbers(a) != _numbers(b):
        return 0.0
    return similarity(as_digits(a), as_digits(b))


def title_similarity(target: str, album: str) -> tuple[float, str | None]:
    """How alike two album titles are once editions, subtitles and alternative titles are
    accounted for, asymmetrically: MusicBrainz's title may carry a subtitle or soundtrack
    label the tracker drops ("Interstellar: Original Motion Picture Soundtrack") or keep
    only its subtitle, and a tracker may add a native-script alternative in parentheses;
    but a subtitle only the tracker has ("Rival Dealer: Remixes", "Homogenic - Live")
    names a different release, and when both have one the tails must agree too ("Day One"
    is not "Day Two"). Numbers are compared exactly wherever two titles are compared:
    "Album II" is not "Album", "Pt. 2" is not "Pt. 1"."""
    stripped = strip_edition(album)
    base = _similar(target, stripped)
    note = "matched after stripping an edition suffix" if stripped != album else None
    t_head, t_tail, t_kind = _split_tail(target)
    a_head, a_tail, a_kind = _split_tail(stripped)
    best = base
    if t_tail and not a_tail:
        cand = _similar(t_head, stripped)
        cand_note = "matched the target's title before its subtitle or tail"
        if t_kind in ("alt", "subtitle"):
            # A listing may keep only the subtitle ("Music for Airports" for "Ambient 1:
            # Music for Airports"), as it may keep only the alternative title.
            by_tail = _similar(t_tail, stripped)
            if by_tail > cand:
                cand, cand_note = (
                    by_tail,
                    "matched the target's subtitle or alternative title alone",
                )
        if cand > best:
            best, note = cand, cand_note
    elif a_tail and not t_tail:
        if a_kind == "soundtrack":
            cand = _similar(target, a_head)
        elif a_kind == "alt":
            cand = max(_similar(target, a_head), _similar(target, a_tail))
        else:
            # A subtitle only the tracker has ("Remixes", "Live") is a different release,
            # however much of the title it shares: cap it below the threshold, which the
            # score cannot exceed since the title bounds it.
            return min(base, 0.8), "the tracker's title carries a subtitle the target lacks"
        if cand > best:
            best, note = cand, "matched the title before a subtitle, soundtrack tail or parenthesis"
    elif t_tail and a_tail:
        # Both carry a tail: the tails must agree too. This overrides the whole-string
        # similarity, which is high for "Day One" against "Day Two".
        heads = _similar(t_head, a_head)
        tails = _similar(t_tail, a_tail)
        if t_kind == "alt" or a_kind == "alt":
            crossed = max(_similar(t_head, a_tail), _similar(t_tail, a_head))
            tails = max(tails, crossed)
        return min(heads, tails), "matched head and subtitle separately"
    if best == 0.0 and _numbers(target) != _numbers(stripped):
        note = "the titles differ in a number"
    return best, note


def match(parsed: ParsedTitle, *, artist: str, title: str, year: int | None) -> MatchResult:
    notes: list[str] = []
    if not parsed.artist or not parsed.album:
        return MatchResult(0.0, 0.0, 0.0, None, ["title did not parse"])
    a_plain = similarity(artist, parsed.artist)
    a_sim = max(
        similarity(a, b) for a in artist_variants(artist) for b in artist_variants(parsed.artist)
    )
    if a_sim > a_plain:
        notes.append("matched the artist before a 'performed by' or 'feat.' tail")
    t_plain = similarity(title, parsed.album)
    t_sim, note = title_similarity(title, parsed.album)
    if note and (t_sim != t_plain or "edition" in note):
        notes.append(note)
    # The title decides and the artist can only lower the score: half and half would let
    # a matching artist carry a title similarity of 0.7 over the 0.85 threshold, which is
    # what "Rival Dealer: Remixes" and "Silent Alarm Remixed" came down to.
    score = min(t_sim, 0.5 * a_sim + 0.5 * t_sim)
    year_ok: bool | None = None
    if parsed.year and year:
        year_ok = abs(parsed.year - year) <= 1
        if not year_ok:
            if parsed.remaster_year:
                notes.append(f"year {parsed.year} differs but a remaster year is present")
                year_ok = True
            else:
                # On Gazelle the head year is the release group's year, so a different year
                # is a different album that happens to share the name. Not a candidate.
                score = 0.0
                notes.append(f"year {parsed.year} vs target {year}: different release")
    return MatchResult(max(score, 0.0), a_sim, t_sim, year_ok, notes)
