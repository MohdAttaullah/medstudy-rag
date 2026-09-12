"""Curator review of a flagged parse.

Separate from `app.api.parsing`, which states in its own contract that nothing there starts,
changes or approves a parse. That contract is worth keeping: inspection and judgement are
different authorities, and only this module holds the second one.

Routes are parse-run scoped rather than job scoped. A decision belongs to one exact parse run —
never to "whichever run happens to be current" — so the run is named in the URL and re-resolved
under the caller's tenant before anything is written.
"""

from uuid import UUID

from fastapi import APIRouter, Request

from app.api.documents import Actor, Service, correlation
from app.core.errors import DomainError
from app.schemas.review import ReviewDecisionRequest, ReviewDecisionView

router = APIRouter(prefix="/api/v1")

Base = "/documents/{document_id}/versions/{version_id}/parse-runs/{run_id}/review"


@router.get(Base, response_model=ReviewDecisionView | None)
def decision(
    document_id: UUID, version_id: UUID, run_id: UUID, actor: Actor, service: Service
) -> ReviewDecisionView | None:
    """The review decision for this parse run, or null if it was never reviewed."""
    actor.require("document:read")
    found = service.reviews.decision_for(actor, document_id, version_id, run_id)
    return ReviewDecisionView.model_validate(found) if found is not None else None


@router.post(Base, response_model=ReviewDecisionView)
def accept(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    body: ReviewDecisionRequest,
    request: Request,
    actor: Actor,
    service: Service,
) -> ReviewDecisionView:
    """Accept a parse the quality layer flagged, on the record.

    The automated verdict and every finding are left exactly as they are; the acceptance is a
    separate immutable fact. Idempotent: a run already accepted returns its existing decision.
    """
    service.reviews.accept(
        actor, document_id, version_id, run_id, body.rationale, correlation(request)
    )
    found = service.reviews.decision_for(actor, document_id, version_id, run_id)
    if found is None:  # pragma: no cover - the write above committed one
        raise DomainError("PARSE_REVIEW_NOT_APPLICABLE", "The decision was not recorded.", 409)
    return ReviewDecisionView.model_validate(found)
