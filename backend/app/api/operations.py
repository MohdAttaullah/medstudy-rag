"""Read-only operational state for administrators.

Everything here is already visible somewhere — job rows, the settings history, the readiness probe
— but an operator diagnosing an incident should not have to assemble it from five endpoints. This
is one authorized summary of the things an on-call engineer actually asks: is the queue growing,
are jobs failing, is the model ready, what configuration is live, and what schema is deployed.

Strictly read-only, and guarded by `operations:read` rather than `settings:write`, because
watching the system is not the same authority as changing it. No infrastructure control is exposed
here at all: there is no restart, no flush, no re-dispatch. Those remain the existing audited
ingestion actions with their own scopes.
"""

from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.documents import Actor, Service
from app.models.documents import IngestionJob
from app.schemas.operations import OperationsStatus

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


@router.get("/status", response_model=OperationsStatus)
def status(actor: Actor, service: Service) -> dict[str, Any]:
    """Operational state for this tenant plus service-wide readiness facts.

    Job counts are tenant-scoped like every other query in this system; model and schema identity
    are service-wide and carry no tenant data.
    """
    actor.require("operations:read")
    settings = service.settings

    with service.sessions() as session:
        rows = session.execute(
            select(IngestionJob.status, func.count())
            .where(IngestionJob.tenant_id == actor.tenant_id)
            .group_by(IngestionJob.status)
        ).all()
        counts: dict[str, int] = {str(state): int(total) for state, total in rows}
        # `retry_count` is the durable field M1 records; a growing sum is the signal that jobs
        # are failing and being re-dispatched rather than succeeding first time.
        retries = (
            session.scalar(
                select(func.coalesce(func.sum(IngestionJob.retry_count), 0)).where(
                    IngestionJob.tenant_id == actor.tenant_id
                )
            )
            or 0
        )
        _, snapshot = service.configuration.resolve(actor)

    return {
        "environment": settings.environment,
        "service_name": settings.service_name,
        "auth_mode": settings.auth.mode,
        "rate_limiting_enabled": settings.limits.rate_limiting_enabled,
        "job_counts": counts,
        "total_retries": int(retries),
        "configuration_revision": snapshot["revision"],
        "configuration_fingerprint": snapshot["effective_fingerprint"],
        "pending_rebuild_settings": sorted(snapshot.get("runtime_values", {})),
        # Model identity, never a credential. This is what an incident review needs in order to
        # attribute a bad answer to the exact model and policy that produced it.
        "models": {
            "embedding": settings.embedding.model_id,
            "embedding_revision": settings.embedding.model_revision,
            "query_encoder": settings.query_encoder.model_id,
            "reranker": settings.reranker.model_id,
            "generator": settings.generator.model_id if settings.generator else None,
            "verifier": settings.verifier.model_id if settings.verifier else None,
        },
        "policy_fingerprints": {
            "retrieval": settings.retrieval.fingerprint,
            "reranking": settings.reranking.fingerprint,
            "sufficiency": settings.sufficiency.fingerprint,
            "claim_verification": settings.claim_verification.fingerprint,
            "final_verification": settings.final_verification.fingerprint,
        },
        "credentials_present": {
            "openai": settings.credential_for("openai"),
            "anthropic": settings.credential_for("anthropic"),
        },
    }
