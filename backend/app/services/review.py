"""Curator review of a parse the deterministic quality layer flagged.

The automated verdict is never rewritten. A reviewed parse keeps `validation_result = NEEDS_REVIEW`
and every `ParseValidationFinding` exactly as the quality layer wrote it; the curator's judgement
is recorded separately, as an immutable `ParseReviewDecision` bound to one exact parse run.

That separation is the point. `SUCCEEDED` would say the parse passed, which is false, and a
rewritten `validation_result` would destroy the only record that a human was needed. The run
carries `REVIEWED_ACCEPTED` instead, so every reader — the database guards, the chunking
predicate, the inspector UI and any future audit — can still tell automatic success apart from
accepted-after-review.
"""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.ingestion.state import transition
from app.models.documents import DocumentVersion, IngestionJob
from app.models.enums import ParseResult, ParseRunStatus, Status
from app.models.parsing import ParseReviewDecision, ParseRun, ParseValidationFinding
from app.observability.ingestion import audit
from app.repositories.documents import get_document
from app.repositories.parsing import get_parse_run, scoped_version
from app.security.auth import Principal

#: The job-level error code that distinguishes a parse-stage review from a chunk- or embed-stage
#: one. All three land the job in NEEDS_REVIEW; only this one is a parse a curator can accept.
PARSE_REVIEW_CODE = "PARSE_NEEDS_REVIEW"


def findings_digest(session: Session, run_id: UUID) -> tuple[int, str]:
    """Count and digest of exactly the findings that exist now, for the decision record.

    The append-only trigger blocks UPDATE and DELETE but not INSERT. Without this, a finding
    appended after a decision would silently appear to have been covered by it.
    """
    rows = list(
        session.execute(
            select(
                ParseValidationFinding.scope,
                ParseValidationFinding.severity,
                ParseValidationFinding.code,
                ParseValidationFinding.page_number,
                ParseValidationFinding.id,
            )
            .where(ParseValidationFinding.parse_run_id == run_id)
            .order_by(
                ParseValidationFinding.scope,
                ParseValidationFinding.severity,
                ParseValidationFinding.code,
                ParseValidationFinding.page_number,
                ParseValidationFinding.id,
            )
        )
    )
    digest = hashlib.sha256()
    for scope, severity, code, page_number, finding_id in rows:
        level = getattr(severity, "value", severity)
        digest.update(f"{scope}|{level}|{code}|{page_number}|{finding_id}\n".encode())
    return len(rows), digest.hexdigest()


class ReviewService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def decision_for(
        self, actor: Principal, document_id: UUID, version_id: UUID, run_id: UUID
    ) -> ParseReviewDecision | None:
        with self.sessions() as session:
            scoped_version(session, actor.tenant_id, document_id, version_id)
            run = get_parse_run(session, actor.tenant_id, run_id)
            if run.document_version_id != version_id:
                raise DomainError("PARSE_RUN_NOT_FOUND", "Parse run not found.", 404)
            return self._existing(session, run_id)

    @staticmethod
    def _existing(session: Session, run_id: UUID) -> ParseReviewDecision | None:
        return session.scalar(
            select(ParseReviewDecision).where(
                ParseReviewDecision.parse_run_id == run_id,
                ParseReviewDecision.decision == "ACCEPT",
            )
        )

    def accept(
        self,
        actor: Principal,
        document_id: UUID,
        version_id: UUID,
        run_id: UUID,
        rationale: str,
        correlation_id: UUID,
    ) -> UUID:
        """Record an ACCEPT decision and admit the existing parse dataset into chunking.

        Consumes no retry and creates no parse run: the dataset being admitted is the one the
        curator examined. Returns the decision id, and is idempotent — a run already accepted
        returns its existing decision rather than raising, so a retried request or a second
        curator cannot produce a second decision or a second transition.
        """
        actor.require("ingestion:accept")
        with self.sessions.begin() as session:
            version = scoped_version(session, actor.tenant_id, document_id, version_id)
            document = get_document(session, actor.tenant_id, version.document_id, lock=True)
            run = get_parse_run(session, actor.tenant_id, run_id)
            if run.document_version_id != version_id:
                raise DomainError("PARSE_RUN_NOT_FOUND", "Parse run not found.", 404)

            existing = self._existing(session, run_id)
            if existing is not None:
                return existing.id  # Idempotent: the decision already stands.

            if document.archived_at:
                raise DomainError(
                    "DOCUMENT_ARCHIVED", "Archived document parses cannot be accepted.", 409
                )
            if (
                run.validation_result is not ParseResult.NEEDS_REVIEW
                or run.status is not ParseRunStatus.FAILED
            ):
                raise DomainError(
                    "PARSE_REVIEW_NOT_APPLICABLE",
                    "Only a parse flagged for review can be accepted.",
                    409,
                )
            newest = session.scalar(
                select(ParseRun)
                .where(ParseRun.document_version_id == version_id)
                .order_by(ParseRun.attempt.desc())
                .limit(1)
            )
            if newest is not None and newest.id != run.id:
                raise DomainError(
                    "PARSE_REVIEW_SUPERSEDED",
                    "A newer parse run exists for this version; review that one instead.",
                    409,
                )
            if run.ingestion_job_id is None:
                raise DomainError(
                    "PARSE_REVIEW_NOT_APPLICABLE", "This parse has no ingestion job.", 409
                )
            job = session.get(IngestionJob, run.ingestion_job_id, with_for_update=True)
            if job is None:
                raise DomainError("INGESTION_JOB_NOT_FOUND", "Ingestion job not found.", 404)
            if job.status is not Status.NEEDS_REVIEW or job.last_error_code != PARSE_REVIEW_CODE:
                # A chunk- or embed-stage review also sits in NEEDS_REVIEW and is not this.
                raise DomainError(
                    "INGESTION_INVALID_TRANSITION",
                    "This job is not awaiting parse review.",
                    409,
                )

            count, digest = findings_digest(session, run.id)
            decision = ParseReviewDecision(
                tenant_id=actor.tenant_id,
                parse_run_id=run.id,
                decision="ACCEPT",
                reviewer_user_id=actor.user_id,
                rationale=rationale,
                validation_result_at_decision=ParseResult.NEEDS_REVIEW.value,
                finding_count=count,
                findings_digest=digest,
                configuration_fingerprint=run.configuration_fingerprint,
                correlation_id=correlation_id,
            )
            session.add(decision)
            # The decision must be visible to the parse_review_guard trigger, which refuses a
            # REVIEWED_ACCEPTED run that has no ACCEPT decision of its own.
            session.flush()

            _supersede(session, version, run)
            run.status = ParseRunStatus.REVIEWED_ACCEPTED
            run.is_active = True
            version.page_count = run.page_count
            # Order matters, and the database enforces it. The job guard admits the acceptance
            # edge only while an active REVIEWED_ACCEPTED run exists for the version, so the run
            # has to be written before the job moves; the unit of work does not order updates
            # across tables by assignment order.
            session.flush()

            job.correlation_id = correlation_id
            transition(
                session,
                job,
                version,
                Status.READY_FOR_CHUNKING,
                actor.user_id,
                accept=True,
            )
            audit(
                session,
                actor.tenant_id,
                actor.user_id,
                "PARSE_REVIEW_ACCEPTED",
                run.id,
                correlation_id,
                {
                    "parse_run_id": str(run.id),
                    "decision": "ACCEPT",
                    "validation_result": ParseResult.NEEDS_REVIEW.value,
                    "finding_count": count,
                    "findings_digest": digest,
                    # The rationale is curator prose that may quote the document. It lives in the
                    # decision row and never reaches audit metadata or the structured log.
                    "rationale_chars": len(rationale),
                },
            )
            return decision.id


def _supersede(session: Session, version: DocumentVersion, run: ParseRun) -> None:
    """Retire any previously active run, exactly as the automatic success path does."""
    previous = session.scalars(
        select(ParseRun)
        .where(
            ParseRun.document_version_id == version.id,
            ParseRun.id != run.id,
            ParseRun.is_active.is_(True),
        )
        .with_for_update()
    )
    for stale in previous:
        stale.is_active = False
    session.flush()
