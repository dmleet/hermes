"""Approval gate between CANDIDATES_READY and SUBMITTED (docs/plan.md 5.6).

Timid mode (the default): every acquisition waits for a human. Otherwise manual requests
approve themselves and automated ones wait unless an auto-approve rule matches the top
candidate. ``dry_run`` stops everything short of submitting and records what would have
happened. Blast radius is Prowlarr's per-indexer grab limit, not a Hermes budget.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from hermes.config import AutoApproveRule
from hermes.domain.models import Acquisition, Candidate, Event, Origin, utcnow
from hermes.domain.state import AcquisitionState as S
from hermes.domain.state import transition
from hermes.services.context import Context
from hermes.services.submit import attempted_guids, next_candidate, submit


def rule_matches(rule: AutoApproveRule, candidate: Candidate) -> bool:
    if rule.freeleech is not None and candidate.freeleech != rule.freeleech:
        return False
    if rule.max_bytes is not None and (candidate.size_bytes or 0) > rule.max_bytes:
        return False
    if rule.indexers is None:
        return True
    return candidate.indexer_name.casefold() in (i.casefold() for i in rule.indexers)


async def decide(session: Session, ctx: Context, acq: Acquisition) -> Acquisition:
    """Called once candidates are ready. Ends in SUBMITTED, AWAITING_APPROVAL, or (dry run)
    stays in CANDIDATES_READY with an explanatory event."""
    if acq.state != S.CANDIDATES_READY:
        return acq
    top = next_candidate(acq)
    if top is None:
        transition(session, acq, S.FAILED, "no candidate left to submit")
        session.commit()
        return acq

    policy = ctx.policy
    if policy.approval.timid:
        transition(
            session,
            acq,
            S.AWAITING_APPROVAL,
            "waiting for approval (timid mode)",
            data={"candidate_id": top.id, "title": top.title},
        )
        session.commit()
        return acq
    if acq.origin == Origin.MANUAL:
        why = "manual request; approval not required"
    else:
        rule = next((r for r in policy.approval.auto_approve if rule_matches(r, top)), None)
        if rule is None:
            transition(
                session,
                acq,
                S.AWAITING_APPROVAL,
                "waiting for approval: no auto-approve rule matches the top candidate",
                data={"candidate_id": top.id, "title": top.title},
            )
            session.commit()
            return acq
        why = f"auto-approve rule matched: {rule.model_dump(exclude_none=True)}"

    if policy.dry_run:
        session.add(
            Event(
                acquisition=acq,
                message=f"dry_run: would submit #{top.rank} {top.title} ({why})",
                data={"candidate_id": top.id},
            )
        )
        session.commit()
        return acq

    acq.approved_by = "policy"
    acq.approved_at = utcnow()
    session.add(Event(acquisition=acq, message=f"approved: {why}", data={"candidate_id": top.id}))
    session.commit()
    return await submit(session, ctx, acq)


async def approve(session: Session, ctx: Context, acq: Acquisition, by: str) -> Acquisition:
    """A human approval. Still respects dry_run. Also how a stalled download is allowed
    to fall back to its next candidate when the policy does not permit that automatically."""
    if acq.state not in (S.AWAITING_APPROVAL, S.CANDIDATES_READY, S.STALLED):
        raise ValueError(f"cannot approve from state {acq.state}")
    acq.approved_by = by
    acq.approved_at = utcnow()
    session.add(Event(acquisition=acq, message=f"approved by {by}"))
    session.commit()
    if ctx.policy.dry_run:
        top = next_candidate(acq)
        session.add(
            Event(
                acquisition=acq,
                message=f"dry_run: would submit {top.title if top else 'nothing'}",
            )
        )
        session.commit()
        return acq
    return await submit(session, ctx, acq)


KEEP_CUT = "acceptable, but beyond keep_candidates"
PREFER_STATES = (S.AWAITING_APPROVAL, S.CANDIDATES_READY, S.STALLED)


def preferable(acq: Acquisition) -> dict[int, str]:
    """The candidates a human may put in front of the ranker's choice, with the button
    label: ranked rows ("Prefer") and rows the ranker cut only for `keep_candidates`
    ("Use anyway"), which passed every policy check and lost on rank. Rows rejected by a
    quality rule are not offered: `submit()` re-checks the sample rate on the torrent
    itself and would reverse the override, and a matching or release-type rejection
    describes a different album. Torrents already tried are out: `next_candidate` skips
    them, so Approve would quietly grab something else (docs/ui-plan.md 4.2)."""
    if acq.state not in PREFER_STATES:
        return {}
    tried = attempted_guids(acq)
    top = next_candidate(acq)
    out: dict[int, str] = {}
    for c in acq.candidates:
        if c.prowlarr_guid in tried or (top is not None and c.id == top.id):
            continue
        if not c.download_url:
            continue  # submit() skips a magnet-only row; Approve would take the next one
        if c.rank is not None:
            out[c.id] = "Prefer"
        elif (c.rejected_reason or "").startswith(KEEP_CUT):
            out[c.id] = "Use anyway"
    return out


def prefer(session: Session, acq: Acquisition, candidate: Candidate, by: str) -> Acquisition:
    """Put `candidate` in front: it becomes rank 1 and the other ranked rows keep their
    relative order, so `next_candidate` (Approve, and the observer's stall fallback) takes
    it next. Only reorders; the grab still goes through Approve and its preview. A later
    search re-ranks from scratch, which the page says."""
    if acq.state not in PREFER_STATES:
        raise ValueError(f"cannot prefer a candidate from state {acq.state}")
    if candidate.acquisition_id != acq.id:
        raise ValueError(f"candidate {candidate.id} belongs to another acquisition")
    if candidate.prowlarr_guid in attempted_guids(acq):
        raise ValueError(f"{candidate.title} was already tried (see the grab attempts)")
    if not candidate.download_url:
        raise ValueError(f"{candidate.title} has no torrent download URL (magnet-only)")
    top = next_candidate(acq)
    if top is not None and top.id == candidate.id:
        return acq  # already what Approve would fetch
    if candidate.rank is None:
        if not (candidate.rejected_reason or "").startswith(KEEP_CUT):
            raise ValueError(
                f"{candidate.title} was rejected ({candidate.rejected_reason}); "
                "only ranked candidates and keep_candidates cuts can be preferred"
            )
        candidate.rejected_reason = None
    ranked = sorted((c for c in acq.candidates if c.rank is not None), key=lambda c: c.rank or 0)
    order = [candidate] + [c for c in ranked if c.id != candidate.id]
    for i, c in enumerate(order, start=1):
        c.rank = i
    session.add(
        Event(
            acquisition=acq,
            message=f"preferred {candidate.title}"
            + (f" over {top.title}" if top else "")
            + f" (by {by})",
            data={"candidate_id": candidate.id, "previous_candidate_id": top.id if top else None},
        )
    )
    session.commit()
    return acq


def preferred_id(acq: Acquisition) -> int | None:
    """The candidate a human last put in front, if that choice still stands."""
    for event in reversed(acq.events):
        if event.message.startswith("preferred "):
            return int(event.data["candidate_id"])
        if event.data.get("to") == S.SEARCHING:
            return None  # a later search re-ranked from scratch
    return None


def reject(session: Session, acq: Acquisition, by: str, reason: str = "") -> Acquisition:
    transition(session, acq, S.REJECTED, f"rejected by {by}" + (f": {reason}" if reason else ""))
    session.commit()
    return acq


def cancel(session: Session, acq: Acquisition, by: str) -> Acquisition:
    transition(session, acq, S.CANCELLED, f"cancelled by {by}")
    session.commit()
    return acq
