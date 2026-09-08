"""Glue between stages that run back to back after a request or a search."""

from __future__ import annotations

from sqlalchemy.orm import Session

from hermes.domain.models import Acquisition
from hermes.domain.state import AcquisitionState as S
from hermes.services.approval import decide
from hermes.services.context import Context
from hermes.services.search import run_search


async def search_and_decide(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    """SEARCHING -> CANDIDATES_READY -> (approval gate) -> SUBMITTED | AWAITING_APPROVAL."""
    if ctx.prowlarr is None:
        raise ValueError("Prowlarr is not configured")
    acq = await run_search(session, ctx.prowlarr, ctx.policy, acq, musicbrainz=ctx.musicbrainz)
    if acq.state == S.CANDIDATES_READY:
        acq = await decide(session, ctx, acq)
    return acq
