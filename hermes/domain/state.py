"""Acquisition state machine. The single place that mutates ``Acquisition.state``.

Every transition writes an ``Event`` so the acquisition page can show why things happened.
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition, Event, utcnow


class AcquisitionState(enum.StrEnum):
    DISCOVERED = "DISCOVERED"
    MANUAL = "MANUAL"
    RESOLVED = "RESOLVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ALREADY_OWNED = "ALREADY_OWNED"
    SEARCHING = "SEARCHING"
    NO_MATCH = "NO_MATCH"
    CANDIDATES_READY = "CANDIDATES_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    DOWNLOADING = "DOWNLOADING"
    STALLED = "STALLED"
    READY_FOR_BEETS = "READY_FOR_BEETS"
    IMPORTING = "IMPORTING"
    IMPORT_NEEDS_REVIEW = "IMPORT_NEEDS_REVIEW"
    IMPORTED = "IMPORTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = AcquisitionState

TERMINAL: frozenset[AcquisitionState] = frozenset(
    {S.ALREADY_OWNED, S.REJECTED, S.IMPORTED, S.CANCELLED}
)

# Explicit forward edges. FAILED and CANCELLED are handled generically below.
_EDGES: dict[AcquisitionState, frozenset[AcquisitionState]] = {
    S.DISCOVERED: frozenset({S.RESOLVED, S.NEEDS_REVIEW}),
    S.MANUAL: frozenset({S.RESOLVED, S.NEEDS_REVIEW}),
    S.NEEDS_REVIEW: frozenset({S.RESOLVED, S.REJECTED}),
    S.RESOLVED: frozenset({S.ALREADY_OWNED, S.SEARCHING}),
    S.SEARCHING: frozenset({S.NO_MATCH, S.CANDIDATES_READY}),
    S.NO_MATCH: frozenset({S.SEARCHING, S.REJECTED}),  # REJECTED: gave up after max_retries
    S.CANDIDATES_READY: frozenset({S.AWAITING_APPROVAL, S.SUBMITTED, S.SEARCHING}),
    S.AWAITING_APPROVAL: frozenset({S.SUBMITTED, S.REJECTED}),
    S.SUBMITTED: frozenset({S.DOWNLOADING, S.STALLED}),
    S.DOWNLOADING: frozenset({S.READY_FOR_BEETS, S.STALLED}),
    S.STALLED: frozenset({S.SUBMITTED}),
    # A download can need review before the import starts (beets cannot see the path).
    S.READY_FOR_BEETS: frozenset({S.IMPORTING, S.IMPORT_NEEDS_REVIEW}),
    S.IMPORTING: frozenset({S.IMPORTED, S.IMPORT_NEEDS_REVIEW}),
    S.IMPORT_NEEDS_REVIEW: frozenset({S.IMPORTING, S.IMPORTED}),
    S.FAILED: frozenset({S.RESOLVED, S.SEARCHING, S.SUBMITTED, S.READY_FOR_BEETS}),
    S.ALREADY_OWNED: frozenset(),
    S.REJECTED: frozenset(),
    S.IMPORTED: frozenset(),
    S.CANCELLED: frozenset(),
}

_CANCELLABLE: frozenset[AcquisitionState] = frozenset(
    {
        S.DISCOVERED,
        S.MANUAL,
        S.NEEDS_REVIEW,
        S.RESOLVED,
        S.SEARCHING,
        S.NO_MATCH,
        S.CANDIDATES_READY,
        S.AWAITING_APPROVAL,
        S.STALLED,  # the torrent is dead; nothing is downloading
        # beets would not take the download (refused, skipped, a stuck agent): a human can
        # let the album go; the torrent keeps seeding, and a new request starts afresh.
        S.IMPORT_NEEDS_REVIEW,
        S.FAILED,
    }
)


class InvalidTransition(Exception):
    def __init__(self, current: str, target: AcquisitionState) -> None:
        super().__init__(f"cannot move acquisition from {current} to {target}")
        self.current = current
        self.target = target


def can_transition(current: AcquisitionState, target: AcquisitionState) -> bool:
    if target == S.FAILED:
        return current not in TERMINAL
    if target == S.CANCELLED:
        return current in _CANCELLABLE
    return target in _EDGES[current]


def transition(
    session: Session,
    acquisition: Acquisition,
    target: AcquisitionState,
    message: str,
    *,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> Event:
    """Move ``acquisition`` to ``target`` and record an Event. Raises InvalidTransition."""
    current = AcquisitionState(acquisition.state)
    if not can_transition(current, target):
        raise InvalidTransition(acquisition.state, target)
    acquisition.state = target
    acquisition.updated_at = utcnow()
    if target == S.FAILED:
        acquisition.error = message
        level = "error"
    event = Event(
        acquisition=acquisition,
        level=level,
        message=message,
        data={"from": current, "to": target, **(data or {})},
    )
    session.add(event)
    return event
