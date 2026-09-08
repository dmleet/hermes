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
from hermes.services.submit import next_candidate, submit


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


def reject(session: Session, acq: Acquisition, by: str, reason: str = "") -> Acquisition:
    transition(session, acq, S.REJECTED, f"rejected by {by}" + (f": {reason}" if reason else ""))
    session.commit()
    return acq


def cancel(session: Session, acq: Acquisition, by: str) -> Acquisition:
    transition(session, acq, S.CANCELLED, f"cancelled by {by}")
    session.commit()
    return acq
