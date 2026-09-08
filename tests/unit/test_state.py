from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition, AlbumTarget, Event, Origin
from hermes.domain.state import TERMINAL, InvalidTransition, can_transition, transition
from hermes.domain.state import AcquisitionState as S


def _acquisition(session: Session, state: S) -> Acquisition:
    target = AlbumTarget(
        release_group_mbid="48140466-cff6-3222-bd55-63c27e43190d",
        artist_name="Portishead",
        title="Dummy",
    )
    acq = Acquisition(album_target=target, state=state, origin=Origin.MANUAL)
    session.add(acq)
    session.flush()
    return acq


def test_happy_path_records_events(session: Session) -> None:
    acq = _acquisition(session, S.MANUAL)
    path = [
        S.RESOLVED,
        S.SEARCHING,
        S.CANDIDATES_READY,
        S.AWAITING_APPROVAL,
        S.SUBMITTED,
        S.DOWNLOADING,
        S.READY_FOR_BEETS,
        S.IMPORTING,
        S.IMPORTED,
    ]
    for target in path:
        transition(session, acq, target, f"-> {target}")
    session.commit()

    assert acq.state == S.IMPORTED
    events = session.scalars(select(Event).order_by(Event.id)).all()
    assert [e.data["to"] for e in events] == path
    assert events[0].data["from"] == S.MANUAL


def test_invalid_transition_raises_and_leaves_state(session: Session) -> None:
    acq = _acquisition(session, S.RESOLVED)
    with pytest.raises(InvalidTransition):
        transition(session, acq, S.IMPORTED, "skip ahead")
    assert acq.state == S.RESOLVED
    assert session.scalars(select(Event)).all() == []


def test_failed_sets_error_and_is_retryable(session: Session) -> None:
    acq = _acquisition(session, S.DOWNLOADING)
    event = transition(session, acq, S.FAILED, "torrent removed")
    assert acq.error == "torrent removed"
    assert event.level == "error"
    transition(session, acq, S.SUBMITTED, "retry with next candidate")
    assert acq.state == S.SUBMITTED


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
def test_terminal_states_cannot_fail_or_move(terminal: S) -> None:
    assert not can_transition(terminal, S.FAILED)
    assert not can_transition(terminal, S.CANCELLED)
    assert all(not can_transition(terminal, s) for s in S)


def test_cancel_only_before_submission() -> None:
    assert can_transition(S.AWAITING_APPROVAL, S.CANCELLED)
    assert can_transition(S.NO_MATCH, S.CANCELLED)
    assert not can_transition(S.SUBMITTED, S.CANCELLED)
    assert not can_transition(S.DOWNLOADING, S.CANCELLED)
    assert not can_transition(S.IMPORTING, S.CANCELLED)


def test_stalled_falls_back_to_submitted() -> None:
    assert can_transition(S.DOWNLOADING, S.STALLED)
    assert can_transition(S.STALLED, S.SUBMITTED)
    assert not can_transition(S.STALLED, S.READY_FOR_BEETS)


def test_candidates_ready_can_be_researched() -> None:
    assert can_transition(S.CANDIDATES_READY, S.SEARCHING)
    assert can_transition(S.NO_MATCH, S.SEARCHING)
    assert not can_transition(S.AWAITING_APPROVAL, S.SEARCHING)


def test_import_edges() -> None:
    assert can_transition(S.READY_FOR_BEETS, S.IMPORTING)
    assert can_transition(S.READY_FOR_BEETS, S.IMPORT_NEEDS_REVIEW)
    assert can_transition(S.IMPORTING, S.IMPORTED)
    assert can_transition(S.IMPORT_NEEDS_REVIEW, S.IMPORTING)
    assert not can_transition(S.IMPORTED, S.IMPORTING)
