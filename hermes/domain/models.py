"""Persistent domain model. See docs/plan.md section 4.

Signal -> AlbumTarget -> Acquisition -> (Candidate, GrabAttempt); Event is the audit log.
State values live in ``hermes.domain.state``; only ``transition()`` there may change
``Acquisition.state``.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class SignalKind(enum.StrEnum):
    LISTENBRAINZ = "listenbrainz"
    MANUAL = "manual"


class ResolutionStatus(enum.StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    NEEDS_REVIEW = "needs_review"
    IGNORED = "ignored"
    FAILED = "failed"


class LibraryStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    MISSING = "missing"
    OWNED = "owned"
    OWNED_LOSSY = "owned_lossy"


class Origin(enum.StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class GrabOutcome(enum.StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    STALLED = "stalled"
    FAILED = "failed"
    REMOVED = "removed"


class PlaylistStatus(enum.StrEnum):
    INGESTED = "ingested"
    IGNORED = "ignored"
    PARTIAL = "partial"  # some tracks still unrecorded (interrupted); the next tick resumes
    FAILED = "failed"


class Playlist(Base):
    """A ListenBrainz playlist Hermes has seen: the idempotence record for discovery."""

    __tablename__ = "playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    mbid: Mapped[str] = mapped_column(String(36), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    # troi's patch name ("weekly-exploration") or "extra" for a configured playlist MBID.
    source: Mapped[str] = mapped_column(String(64))
    created_for: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16))  # acquire | ignore
    status: Mapped[str] = mapped_column(String(16), default=PlaylistStatus.PARTIAL)
    lb_created_at: Mapped[str | None] = mapped_column(String(64))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    track_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # outcome counts
    note: Mapped[str | None] = mapped_column(Text)


class Signal(Base):
    __tablename__ = "signal"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    recording_mbid: Mapped[str | None] = mapped_column(String(36), index=True)
    source_playlist_mbid: Mapped[str | None] = mapped_column(String(36), index=True)
    source_playlist_name: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int | None] = mapped_column(Integer)
    # Manual requests carry free text until resolved.
    requested_artist: Mapped[str | None] = mapped_column(String(255))
    requested_title: Mapped[str | None] = mapped_column(String(255))
    requested_mbid: Mapped[str | None] = mapped_column(String(36))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    album_target_id: Mapped[int | None] = mapped_column(ForeignKey("album_target.id"))
    resolution_status: Mapped[str] = mapped_column(String(16), default=ResolutionStatus.PENDING)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    album_target: Mapped[AlbumTarget | None] = relationship(back_populates="signals")

    __table_args__ = (
        UniqueConstraint("source_playlist_mbid", "position", name="uq_signal_playlist_position"),
    )


class AlbumTarget(Base):
    __tablename__ = "album_target"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_group_mbid: Mapped[str] = mapped_column(String(36), unique=True)
    artist_name: Mapped[str] = mapped_column(String(255))
    artist_mbid: Mapped[str | None] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(255))
    primary_type: Mapped[str | None] = mapped_column(String(32))
    secondary_types: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_release_year: Mapped[int | None] = mapped_column(Integer)
    release_mbids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"ids": [...]}
    preferred_release_mbid: Mapped[str | None] = mapped_column(String(36))
    library_status: Mapped[str] = mapped_column(String(16), default=LibraryStatus.UNKNOWN)
    library_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Album art for the pages (services/art.py): pending | fetched | missing | failed, when it
    # was last tried, and failures in a row (the retry backoff). Server defaults so the
    # migration backfills every existing target as pending.
    art_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    art_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    art_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Track lengths of one release in the group ({"release": mbid, "ms": [...]}), fetched
    # once: with a candidate's size they give its bitrate, and so its sample rate band.
    track_lengths: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # MusicBrainz's genres for the release group ({"top": [{"name", "count"}, ...]}, most
    # votes first, five at most), for the queue and detail pages (services/genres.py).
    # NULL means never fetched: the genres job fills those in, active rows first.
    genres: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))

    signals: Mapped[list[Signal]] = relationship(back_populates="album_target")
    acquisitions: Mapped[list[Acquisition]] = relationship(back_populates="album_target")


class Acquisition(Base):
    __tablename__ = "acquisition"
    # Section 4's rule "one non-terminal acquisition per target" as a database fact, so two
    # processes (the server and a CLI tick) cannot both create one. NULL targets (requests
    # that never resolved) are distinct to the index.
    __table_args__ = (
        Index(
            "uq_acquisition_active_target",
            "album_target_id",
            unique=True,
            sqlite_where=text(
                "state NOT IN ('ALREADY_OWNED', 'REJECTED', 'IMPORTED', 'CANCELLED')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null while resolution has not produced a target (NEEDS_REVIEW / FAILED at resolution).
    album_target_id: Mapped[int | None] = mapped_column(ForeignKey("album_target.id"), index=True)
    # The request or discovery signal that created this acquisition, so an unresolved one can
    # still be shown by what was asked for.
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signal.id"))
    state: Mapped[str] = mapped_column(String(32), index=True)
    origin: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_grab_id: Mapped[int | None] = mapped_column(Integer)
    search_retries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    album_target: Mapped[AlbumTarget | None] = relationship(back_populates="acquisitions")
    signal: Mapped[Signal | None] = relationship(foreign_keys=[signal_id])
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="acquisition", order_by="Candidate.rank"
    )
    grab_attempts: Mapped[list[GrabAttempt]] = relationship(back_populates="acquisition")
    events: Mapped[list[Event]] = relationship(back_populates="acquisition", order_by="Event.at")


class Candidate(Base):
    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(primary_key=True)
    acquisition_id: Mapped[int] = mapped_column(ForeignKey("acquisition.id"), index=True)
    prowlarr_guid: Mapped[str] = mapped_column(String(512))
    indexer_id: Mapped[int] = mapped_column(Integer)
    indexer_name: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    download_url: Mapped[str | None] = mapped_column(String(1024))
    # The indexer's human page for this release, for the UI to link the title to. Prowlarr
    # reports it per indexer and not every one has it, so it stays optional. It is not a
    # download link and carries no credential: the bytes are fetched from `download_url`,
    # which is Prowlarr's own proxy.
    info_url: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    seeders: Mapped[int | None] = mapped_column(Integer)
    leechers: Mapped[int | None] = mapped_column(Integer)
    freeleech: Mapped[bool] = mapped_column(default=False)
    parsed_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_score: Mapped[float | None] = mapped_column()
    rank: Mapped[int | None] = mapped_column(Integer)
    rejected_reason: Mapped[str | None] = mapped_column(String(255))

    acquisition: Mapped[Acquisition] = relationship(back_populates="candidates")

    @property
    def info_link(self) -> str | None:
        """``info_url`` when it is one to follow. An indexer that reports none, or reports
        something that is not an http(s) URL, leaves the title unlinked rather than
        rendering a dead or surprising link."""
        url = self.info_url or ""
        return url if url.startswith(("http://", "https://")) else None


class GrabAttempt(Base):
    __tablename__ = "grab_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    acquisition_id: Mapped[int] = mapped_column(ForeignKey("acquisition.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"))
    deluge_instance: Mapped[str] = mapped_column(String(32))  # key into policy.deluge.instances
    infohash: Mapped[str] = mapped_column(String(40), index=True)
    torrent_name: Mapped[str | None] = mapped_column(String(512))
    download_location: Mapped[str] = mapped_column(String(1024))
    completed_location: Mapped[str] = mapped_column(String(1024))  # move_completed_path given
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_path: Mapped[str | None] = mapped_column(String(1024))
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_total_done: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16), default=GrabOutcome.ACTIVE)
    import_job_id: Mapped[str | None] = mapped_column(String(64))  # beets-hermes agent job
    import_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the torrent's files look like (audio count, files per directory, CD rip), read
    # once at submit time. Hermes keeps no copy of the torrent file itself: its announce
    # URL carries the tracker passkey and Deluge already holds the file for seeding.
    download_shape: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    acquisition: Mapped[Acquisition] = relationship(back_populates="grab_attempts")


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    acquisition_id: Mapped[int | None] = mapped_column(ForeignKey("acquisition.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signal.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    acquisition: Mapped[Acquisition | None] = relationship(back_populates="events")
