"""Parse tracker release titles into a normalised quality model (docs/plan.md 5.5).

Two families are handled. Gazelle trackers title releases as

    Artist - Album (1997) [Album] [Remaster Title 2017]
        [FLAC 24bit Lossless / WEB / Log (100%) / Cue]

and PandaCD (the dev tracker) as

    Artist - Album [2008] [FLAC Lossless]    Artist - Album [2011] [MP3 V0 (VBR)]

The parser is a pure function. Bracket groups are peeled off the end and classified by
content (year, release type, quality, media, remaster); the remaining head is
``Artist - Album (Year)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LOSSLESS_FORMATS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WavPack", "DSD", "DSF"}
KNOWN_FORMATS = [  # longest first so "Ogg Vorbis" wins over "Ogg"
    "Ogg Vorbis",
    "WavPack",
    "FLAC",
    "ALAC",
    "AIFF",
    "Opus",
    "AAC",
    "MP3",
    "WAV",
    "APE",
    "DSD",
    "DSF",
    "DTS",
    "AC3",
    "Ogg",
]
RELEASE_TYPES = {
    "album": "Album",
    "soundtrack": "Soundtrack",
    "ep": "EP",
    "anthology": "Anthology",
    "compilation": "Compilation",
    "single": "Single",
    "live album": "Live album",
    "remix": "Remix",
    "bootleg": "Bootleg",
    "interview": "Interview",
    "mixtape": "Mixtape",
    "demo": "Demo",
    "concert recording": "Concert Recording",
    "dj mix": "DJ Mix",
    "unknown": "Unknown",
}
MEDIA = {
    "web": "WEB",
    "cd": "CD",
    "vinyl": "Vinyl",
    "sacd": "SACD",
    "dvd": "DVD",
    "blu-ray": "Blu-Ray",
    "bluray": "Blu-Ray",
    "bd": "Blu-Ray",
    "cassette": "Cassette",
    "dat": "DAT",
    "soundboard": "Soundboard",
}
EDITION_WORDS = (
    "deluxe",
    "remaster",
    "anniversary",
    "expanded",
    "bonus",
    "special edition",
    "limited edition",
    "collector",
    "japan",
    "explicit",
    "mono",
    "stereo",
)

_BRACKET_TAIL = re.compile(r"\s*\[((?:[^\[\]]|\([^()]*\))*)\]\s*$")
_YEAR = re.compile(r"^\d{4}$")
_HEAD = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+?)(?:\s*\((?P<year>\d{4})\))?\s*$")
_LOG = re.compile(r"^log(?:\s*\(\s*(?P<score>-?\d+)\s*%\s*\))?$", re.IGNORECASE)
_TRAILING_YEAR = re.compile(r"^(?P<title>.*?)\s*(?P<year>\d{4})$")
_FORMAT = re.compile(
    r"^(?P<fmt>" + "|".join(re.escape(f) for f in KNOWN_FORMATS) + r")\b\s*(?P<enc>.*)$",
    re.IGNORECASE,
)
_CANON_FORMAT = {f.lower(): f for f in KNOWN_FORMATS}


@dataclass
class ParsedTitle:
    raw: str
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    release_type: str | None = None
    remaster_title: str | None = None
    remaster_year: int | None = None
    format: str | None = None  # FLAC, MP3, ...
    encoding: str | None = None  # lossless | lossless24 | lossy
    encoding_detail: str | None = None  # "24bit Lossless", "320", "V0 (VBR)"
    sample_rate_khz: int | None = None  # only when the title says so ("24/96", "192kHz")
    media: str | None = None  # WEB, CD, Vinyl, ...
    has_log: bool = False
    log_score: int | None = None
    has_cue: bool = False
    edition_flags: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.artist and self.album and self.format)

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}


def _peel_brackets(title: str) -> tuple[str, list[str]]:
    groups: list[str] = []
    rest = title.strip()
    while True:
        m = _BRACKET_TAIL.search(rest)
        if not m:
            break
        groups.insert(0, m.group(1).strip())
        rest = rest[: m.start()].rstrip()
    return rest, groups


def _parse_quality(group: str, parsed: ParsedTitle) -> None:
    segments = [s.strip() for s in group.split("/")]
    m = _FORMAT.match(segments[0])
    if not m:
        parsed.problems.append(f"quality group without a known format: {group}")
        return
    fmt = _CANON_FORMAT[m.group("fmt").lower()]
    detail = m.group("enc").strip() or None
    parsed.format = fmt
    parsed.encoding_detail = detail
    if fmt in LOSSLESS_FORMATS:
        parsed.encoding = "lossless24" if detail and "24" in detail else "lossless"
    else:
        parsed.encoding = "lossy"
    for seg in segments[1:]:
        _parse_extra_segment(seg, parsed)


def _parse_extra_segment(seg: str, parsed: ParsedTitle) -> bool:
    key = seg.lower()
    if key in MEDIA:
        parsed.media = MEDIA[key]
        return True
    log = _LOG.match(seg)
    if log:
        parsed.has_log = True
        if log.group("score") is not None:
            parsed.log_score = int(log.group("score"))
        return True
    if key == "cue":
        parsed.has_cue = True
        return True
    if key == "scene":
        parsed.edition_flags.append("scene")
        return True
    parsed.problems.append(f"unrecognised segment: {seg}")
    return False


def _classify_group(group: str, parsed: ParsedTitle) -> None:
    key = group.lower()
    if _YEAR.match(group):
        if parsed.year is None:
            parsed.year = int(group)
        else:
            parsed.problems.append(f"second year group: {group}")
        return
    if key in RELEASE_TYPES:
        parsed.release_type = RELEASE_TYPES[key]
        return
    if _FORMAT.match(group.split("/")[0].strip()):
        _parse_quality(group, parsed)
        return
    if key in MEDIA:
        parsed.media = MEDIA[key]
        return
    if key == "scene":
        parsed.edition_flags.append("scene")
        return
    # Anything else is a remaster / edition label, e.g. "OKNOTOK 1997 2017 2017".
    m = _TRAILING_YEAR.match(group)
    if m and m.group("title"):
        parsed.remaster_title = m.group("title").strip() or None
        parsed.remaster_year = int(m.group("year"))
    elif m:
        parsed.remaster_year = int(m.group("year"))
    else:
        parsed.remaster_title = group


def _edition_flags(*texts: str | None) -> list[str]:
    found = []
    blob = " ".join(t for t in texts if t).lower()
    for word in EDITION_WORDS:
        if word in blob:
            found.append(word)
    return found


def parse_title(title: str) -> ParsedTitle:
    parsed = ParsedTitle(raw=title)
    head, groups = _peel_brackets(title)
    m = _HEAD.match(head)
    if m:
        parsed.artist = m.group("artist").strip()
        parsed.album = m.group("album").strip()
        if m.group("year"):
            parsed.year = int(m.group("year"))
    else:
        parsed.problems.append("head is not 'Artist - Album'")
    for group in groups:
        _classify_group(group, parsed)
    if parsed.format is None:
        parsed.problems.append("no format found")
    parsed.edition_flags = sorted(
        set(parsed.edition_flags) | set(_edition_flags(parsed.album, parsed.remaster_title))
    )
    parsed.sample_rate_khz = stated_sample_rate(parsed.album, parsed.remaster_title)
    return parsed


_RATE = re.compile(
    r"(?<![\d.])(?:(?:16|24)\s*(?:bit|-bit)?\s*[/\-]\s*)?"
    r"(?P<khz>44\.1|48|88\.2|96|176\.4|192)\s*k(?:hz)?\b",
    re.IGNORECASE,
)
_RATE_SLASH = re.compile(
    r"(?<![\d.])(?:16|24)\s*(?:bit|-bit)?\s*/\s*(?P<khz>44\.1|48|88\.2|96|176\.4|192)\b"
)


def stated_sample_rate(*texts: str | None) -> int | None:
    """A sample rate written into the title: "24/96", "24bit-96kHz", "Hi-Res 192kHz"."""
    for text in texts:
        if not text:
            continue
        m = _RATE.search(text) or _RATE_SLASH.search(text)
        if m:
            return int(float(m.group("khz")))
    return None
