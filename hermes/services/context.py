"""What every pipeline stage needs: the policy and the clients. The one place to keep
files is ``art_dir``, for album art thumbnails; Hermes stores no torrent files
(docs/plan.md 5.7, decision log 2026-09-07)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hermes.config import Policy
from hermes.integrations.beets import BeetsClient
from hermes.integrations.coverart import CoverArtClient
from hermes.integrations.deluge import DelugeClient
from hermes.integrations.listenbrainz import ListenBrainzClient
from hermes.integrations.musicbrainz import MusicBrainzClient
from hermes.integrations.navidrome import NavidromeClient
from hermes.integrations.prowlarr import ProwlarrClient


@dataclass
class Context:
    policy: Policy
    musicbrainz: MusicBrainzClient
    beets: BeetsClient
    prowlarr: ProwlarrClient | None = None
    deluge: dict[str, DelugeClient] = field(default_factory=dict)
    navidrome: NavidromeClient | None = None
    listenbrainz: ListenBrainzClient | None = None
    coverart: CoverArtClient | None = None
    art_dir: Path | None = None  # <data dir>/art, where fetched covers live
    # Acquisitions imported since Navidrome was last asked to scan; the importer asks once
    # the import queue drains (a scan during an import reads half-written files). None
    # until the first tick derives it from the events, so a restart loses nothing.
    navidrome_scan_owed: list[int] | None = None

    def deluge_for(self, instance: str) -> DelugeClient:
        try:
            return self.deluge[instance]
        except KeyError as exc:
            raise KeyError(f"Deluge instance {instance!r} is not configured") from exc
