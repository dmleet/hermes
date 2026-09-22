from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from hermes.domain.models import Acquisition, AlbumTarget, Event


class ManualRequest(BaseModel):
    artist: str | None = None
    title: str | None = None
    mbid: str | None = None

    @model_validator(mode="after")
    def _needs_name_or_mbid(self) -> ManualRequest:
        if not self.mbid and not (self.artist and self.title):
            raise ValueError("provide artist and title, or mbid")
        return self


class DecisionBody(BaseModel):
    by: str = "api"
    reason: str = ""


class ResolveBody(BaseModel):
    release_group_mbid: str


class PreferBody(BaseModel):
    candidate_id: int
    by: str = "api"


def requested_label(acq: Acquisition) -> str | None:
    """What was asked for, from the originating signal (useful when nothing resolved)."""
    signal = acq.signal
    if signal is None:
        return None
    if signal.requested_artist or signal.requested_title:
        return f"{signal.requested_artist or '?'} - {signal.requested_title or '?'}"
    if signal.requested_mbid:
        return f"MBID {signal.requested_mbid}"
    if signal.recording_mbid:
        return f"recording {signal.recording_mbid}"
    return None


class GrabAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    candidate_id: int
    deluge_instance: str
    infohash: str
    torrent_name: str | None
    download_location: str
    completed_location: str
    completed_path: str | None
    added_at: datetime
    completed_at: datetime | None
    last_progress_at: datetime | None
    last_total_done: int | None
    outcome: str
    import_job_id: str | None = None
    download_shape: dict[str, Any] = {}
    import_started_at: datetime | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    at: datetime
    level: str
    message: str
    data: dict[str, Any]


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rank: int | None
    title: str
    indexer_name: str
    size_bytes: int | None
    seeders: int | None
    leechers: int | None
    freeleech: bool
    info_url: str | None
    match_score: float | None
    rejected_reason: str | None
    parsed_quality: dict[str, Any]


class AlbumTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    release_group_mbid: str
    artist_name: str
    title: str
    primary_type: str | None
    first_release_year: int | None
    preferred_release_mbid: str | None
    library_status: str
    genres: list[str] = []  # MusicBrainz release-group genres, most votes first

    @field_validator("genres", mode="before")
    @classmethod
    def _genre_names(cls, value: Any) -> list[str]:
        if isinstance(value, dict):  # the model's {"top": [{"name", "count"}, ...]}
            return [str(g["name"]) for g in value.get("top") or [] if g.get("name")]
        return list(value or [])


class AcquisitionOut(BaseModel):
    id: int
    state: str
    origin: str
    created_at: datetime
    updated_at: datetime
    error: str | None
    approved_by: str | None
    requested: str | None
    target: AlbumTargetOut | None
    candidates: list[CandidateOut]
    attempts: list[GrabAttemptOut]
    events: list[EventOut]

    @classmethod
    def from_model(cls, acq: Acquisition) -> AcquisitionOut:
        target: AlbumTarget | None = acq.album_target
        events: list[Event] = acq.events
        candidates = sorted(acq.candidates, key=lambda c: (c.rank is None, c.rank or 0, c.id))
        return cls(
            id=acq.id,
            state=acq.state,
            origin=acq.origin,
            created_at=acq.created_at,
            updated_at=acq.updated_at,
            error=acq.error,
            approved_by=acq.approved_by,
            requested=requested_label(acq),
            target=AlbumTargetOut.model_validate(target) if target else None,
            candidates=[CandidateOut.model_validate(c) for c in candidates],
            attempts=[GrabAttemptOut.model_validate(a) for a in acq.grab_attempts],
            events=[EventOut.model_validate(e) for e in events],
        )


class PlaylistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mbid: str
    title: str
    source: str
    created_for: str | None
    mode: str
    status: str
    lb_created_at: str | None
    seen_at: datetime
    ingested_at: datetime | None
    track_count: int
    summary: dict[str, Any]
    note: str | None


class AcquisitionSummary(BaseModel):
    id: int
    state: str
    origin: str
    updated_at: datetime
    requested: str | None
    artist: str | None
    title: str | None
    library_status: str | None
    best_candidate: str | None

    @classmethod
    def from_model(cls, acq: Acquisition) -> AcquisitionSummary:
        target = acq.album_target
        best = next((c for c in acq.candidates if c.rank == 1), None)
        return cls(
            id=acq.id,
            state=acq.state,
            origin=acq.origin,
            updated_at=acq.updated_at,
            requested=requested_label(acq),
            artist=target.artist_name if target else None,
            title=target.title if target else None,
            library_status=target.library_status if target else None,
            best_candidate=best.title if best else None,
        )
