"""Configuration.

Two layers, per docs/plan.md section 6:

* ``Policy`` is the YAML document (what to acquire, how to rank, approval, paths).
  Unknown keys are rejected so typos surface at startup.
* ``Settings`` is the environment (URLs, secrets, where the DB and policy file live).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


PlaylistMode = Literal["acquire", "ignore"]


def _default_playlists() -> dict[str, PlaylistMode]:
    return {"weekly-exploration": "acquire", "weekly-jams": "ignore", "daily-jams": "ignore"}


class ListenBrainzPolicy(_Strict):
    user: str = ""
    playlists: dict[str, PlaylistMode] = Field(default_factory=_default_playlists)
    # Arbitrary playlist MBIDs to ingest like generated ones (curated lists; in dev a
    # hand-built playlist of Creative Commons recordings that exist on PandaCD).
    extra_playlists: dict[str, PlaylistMode] = Field(default_factory=dict)
    # Generated playlists appear on Monday but the hour varies, so look every day.
    poll_hours: int = Field(default=24, ge=1)


class SearchPolicy(_Strict):
    """When a search finds nothing acceptable, how often and how long to try again."""

    max_retries: int = Field(default=4, ge=0)  # searches per acquisition before giving up
    retry_days: int = Field(default=7, ge=1)  # wait between automatic re-searches
    # Prowlarr indexer names to search; empty means every enabled indexer. Lets a Prowlarr
    # shared with other apps keep indexers Hermes should not spend queries on.
    indexers: list[str] = Field(default_factory=list)


class ResolutionPolicy(_Strict):
    allow_ep: bool = True
    allow_single: bool = False
    allow_secondary_types: list[str] = Field(default_factory=list)
    min_similarity: float = Field(default=0.9, ge=0.0, le=1.0)  # artist and title vs. request
    search_limit: int = Field(default=10, ge=1, le=100)


class QualityPolicy(_Strict):
    require_format: str = "FLAC"
    excluded_media: list[str] = Field(default_factory=lambda: ["Vinyl"])
    media_preference: list[str] = Field(
        default_factory=lambda: ["WEB", "CD", "SACD", "DVD", "Blu-Ray"]
    )
    # 24-bit first: with max_sample_rate_khz keeping 192 kHz out, a 24-bit release within the
    # limit is the better copy of the same album.
    encoding_preference: list[str] = Field(default_factory=lambda: ["lossless24", "lossless"])
    indexer_preference: list[str] = Field(default_factory=list)  # Prowlarr indexer names
    max_bytes_lossless: int = 1_500_000_000
    max_bytes_lossless24: int = 4_000_000_000
    min_seeders: int = 1
    allowed_release_types: list[str] = Field(
        default_factory=lambda: [
            "Album",
            "EP",
            "Single",
            "Anthology",
            "Compilation",
            "Soundtrack",
            "Live album",
        ]
    )
    match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    keep_candidates: int = Field(default=3, ge=1)
    # Highest sample rate to accept for lossless releases, in kHz (44, 48, 88, 96, 176, 192)
    # or null for no limit. Tracker titles rarely state the rate, so it is inferred from
    # size and playing time: FLAC at 24/96 runs about 2200-3300 kbps, 24/192 about
    # 4000-6000, so the bands are a factor of two apart and easy to tell. A rate written
    # into the title ("24/96", "192kHz") is believed over the estimate.
    max_sample_rate_khz: int | None = 96
    # Points taken off a release whose title marks it as an edition other than the plain
    # album (keys are the parser's edition flags plus "reissue" for any other reissue
    # label). The largest applicable penalty counts. A plain album keeps its score, so on
    # a tracker where deluxe, anniversary and box-set uploads otherwise tie with it, the
    # plain one wins; 1.0 is below the media-preference gap, so a plain CD rip still
    # loses to a plain WEB release but beats a deluxe WEB release.
    edition_penalties: dict[str, float] = Field(
        default_factory=lambda: {
            "deluxe": 1.0,
            "expanded": 1.0,
            "anniversary": 1.0,
            "bonus": 1.0,
            "special edition": 1.0,
            "remaster": 0.1,
            # An unnamed reissue ("OKNOTOK 1997-2017", "Definitive Edition") may change the
            # tracklist: above the encoding gap (0.75) so a 24-bit reissue does not displace
            # the plain 16-bit album, below the media gap (2.0).
            "reissue": 0.8,
        }
    )


class AutoApproveRule(_Strict):
    """All set fields must hold for the rule to match."""

    freeleech: bool | None = None
    max_bytes: int | None = None
    indexers: list[str] | None = None


class ApprovalPolicy(_Strict):
    """Timid (like beets' timid mode): every acquisition waits for a human, whatever its
    origin. With timid off, manual requests approve themselves and automated ones follow
    the auto-approve rules. Blast radius belongs to Prowlarr's per-indexer grab limit."""

    timid: bool = True
    auto_approve: list[AutoApproveRule] = Field(default_factory=list)


class DelugeInstance(_Strict):
    """One Deluge daemon. Several may be configured, e.g. one per tracker."""

    url: str
    indexers: list[str] = Field(default_factory=list)  # Prowlarr indexer names routed here


class DelugePolicy(_Strict):
    instances: dict[str, DelugeInstance] = Field(default_factory=dict)
    default_instance: str | None = None
    pending_root: str = "/downloads/pending/hermes"  # download_location (see layout)
    completed_root: str = "/downloads/complete/hermes"  # move_completed_path (see layout)
    # Where a torrent lands under the two roots. `per_acquisition`: its own directory,
    # `<root>/<acquisition id>`, which makes a torrent attributable without a torrent file
    # and lets an interrupted submit be recovered by directory. `flat`: straight into the
    # root, beside every other download, e.g. a pool shared with other clients so the same
    # files can be cross-seeded; recovery then relies on Deluge's "already in session".
    layout: Literal["per_acquisition", "flat"] = "per_acquisition"
    # Deluge's Label plugin lowercases names and allows only [a-z0-9_-.].
    label: str = Field(default="hermes", pattern=r"^[a-z0-9_\-.]+$")
    poll_seconds: int = Field(default=45, ge=5)
    stall_hours: int = Field(default=48, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> DelugePolicy:
        if self.default_instance is not None and self.default_instance not in self.instances:
            raise ValueError(
                f"deluge.default_instance {self.default_instance!r} is not in deluge.instances"
            )
        if self.pending_root.rstrip("/") == self.completed_root.rstrip("/"):
            raise ValueError("deluge.pending_root and deluge.completed_root must differ")
        return self

    def locations(self, acquisition_id: int) -> tuple[str, str]:
        """(download_location, move_completed_path) for one acquisition's torrent."""
        pending, completed = self.pending_root.rstrip("/"), self.completed_root.rstrip("/")
        if self.layout == "flat":
            return pending, completed
        return f"{pending}/{acquisition_id}", f"{completed}/{acquisition_id}"

    def instance_for(self, indexer_name: str) -> str | None:
        for name, inst in self.instances.items():
            if indexer_name.casefold() in (i.casefold() for i in inst.indexers):
                return name
        return self.default_instance


class BeetsPolicy(_Strict):
    agent_url: str = "http://beets:8338"  # the beets-hermes agent (reads and imports)
    import_timeout_seconds: int = Field(default=1800, ge=60)
    # Where beets puts imported albums (its `directory`). An import whose album path is not
    # under it (an interrupted copy leaves the library pointing at the seeded files) is sent
    # to review instead of being called done.
    library_root: str = "/music"


class NavidromePolicy(_Strict):
    trigger_scan: bool = True


class ArtPolicy(_Strict):
    """Album art for the pages, from the Cover Art Archive (docs/ui-plan.md 4.1). Cosmetic:
    off means no archive.org calls at all and the pages show an initial instead."""

    enabled: bool = True


class PathsPolicy(_Strict):
    """Path prefix mapping between the torrent client's view and the beets pod's view.

    Needed whenever the two mount the download share at different points. For example, if
    Deluge mounts the whole share at /downloads and moves finished torrents to
    /downloads/complete, while beets mounts only the complete subtree at /downloads, then
    Deluge's /downloads/complete/X is beets' /downloads/X. Identical mounts make this a no-op.
    """

    deluge_root: str = "/downloads/complete"
    beets_root: str = "/downloads"

    def to_beets(self, deluge_path: str) -> str:
        """Rewrite the prefix on path-segment boundaries only."""
        root = self.deluge_root.rstrip("/")
        target = self.beets_root.rstrip("/")
        if deluge_path == root:
            return target
        if deluge_path.startswith(root + "/"):
            return target + deluge_path[len(root) :]
        return deluge_path


class Policy(_Strict):
    dry_run: bool = True
    listenbrainz: ListenBrainzPolicy = Field(default_factory=ListenBrainzPolicy)
    resolution: ResolutionPolicy = Field(default_factory=ResolutionPolicy)
    search: SearchPolicy = Field(default_factory=SearchPolicy)
    quality: QualityPolicy = Field(default_factory=QualityPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    deluge: DelugePolicy = Field(default_factory=DelugePolicy)
    beets: BeetsPolicy = Field(default_factory=BeetsPolicy)
    navidrome: NavidromePolicy = Field(default_factory=NavidromePolicy)
    paths: PathsPolicy = Field(default_factory=PathsPolicy)
    art: ArtPolicy = Field(default_factory=ArtPolicy)


def load_policy(path: Path) -> Policy:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return Policy.model_validate(raw)


class Settings(BaseSettings):
    """Environment-provided settings. Field names map to upper-cased env vars."""

    # .env holds URLs, dev values and secrets; it is gitignored and never opened by tooling.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    hermes_config_path: Path = Path("config.yaml")
    hermes_data_dir: Path = Path("data")
    hermes_database_url: str | None = None
    hermes_host: str = "0.0.0.0"
    hermes_port: int = 8000
    # Overrides policy.dry_run when set, so a deployment can flip it without editing YAML.
    hermes_dry_run: bool | None = None

    listenbrainz_token: SecretStr | None = None
    prowlarr_url: str | None = None
    prowlarr_api_key: SecretStr | None = None
    # One Web UI password used for every configured Deluge instance.
    deluge_password: SecretStr | None = None
    navidrome_url: str | None = None
    navidrome_user: str | None = None
    navidrome_password: SecretStr | None = None
    musicbrainz_contact: str = ""

    @property
    def database_url(self) -> str:
        if self.hermes_database_url:
            return self.hermes_database_url
        return f"sqlite:///{(self.hermes_data_dir / 'hermes.db').as_posix()}"
