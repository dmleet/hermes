"""What every pipeline stage needs: the policy and the clients. Nothing here points at a
place to keep files: Hermes stores no torrent files (docs/plan.md 5.7, decision log 2026-09-07)."""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes.config import Policy
from hermes.integrations.beets import BeetsClient
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

    def deluge_for(self, instance: str) -> DelugeClient:
        try:
            return self.deluge[instance]
        except KeyError as exc:
            raise KeyError(f"Deluge instance {instance!r} is not configured") from exc
