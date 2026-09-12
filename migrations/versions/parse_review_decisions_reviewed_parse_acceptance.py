"""Reviewed parse acceptance: an immutable curator decision admits a flagged parse.

Post-M12 remediation, not a milestone.

The automated verdict is preserved exactly: a reviewed run keeps `validation_result = NEEDS_REVIEW`
and every `parse_validation_findings` row. Acceptance is a separate append-only decision bound to
one exact parse run, and the run carries `REVIEWED_ACCEPTED` so automatic success stays
distinguishable from human judgement.

Five enforcement points move together. Any one of them left behind produces a half-open state —
most sharply `m3_run_guard`, whose `is_active` clause keys on `validation_result` and would let
chunking run to completion and then refuse to activate the dataset.

Revision ID: parse_review_decisions
Revises: m10_configuration
"""

import sqlalchemy as sa
from alembic import op

revision = "parse_review_decisions"
down_revision = "m10_configuration"
branch_labels = None
depends_on = None

PARSE_RUN_STATUS = ("RUNNING", "SUCCEEDED", "REVIEWED_ACCEPTED", "FAILED", "CANCELLED")
PRIOR_PARSE_RUN_STATUS = ("RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")


def _parse_run_status(values: tuple[str, ...], width: int, previous: int) -> None:
    """Rewrite the parse-run status vocabulary.

    The status enums are non-native, so PostgreSQL stores them as VARCHAR sized to the longest
    permitted value plus a CHECK constraint, exactly as `m2_document_parsing` documents. The
    column width moves with the vocabulary: REVIEWED_ACCEPTED is longer than every prior value.
    """
    listed = ", ".join(f"'{value}'" for value in values)
    op.alter_column(
        "parse_runs",
        "status",
        type_=sa.String(length=width),
        existing_type=sa.String(length=previous),
        existing_nullable=False,
    )
    op.execute('ALTER TABLE parse_runs DROP CONSTRAINT IF EXISTS "ck_parse_runs_parse_run_status"')
    op.execute(
        'ALTER TABLE parse_runs ADD CONSTRAINT "ck_parse_runs_parse_run_status" '
        f"CHECK (status IN ({listed}))"
    )


def upgrade() -> None:
    # 1. The status vocabulary and its column width.
    _parse_run_status(PARSE_RUN_STATUS, 17, 9)

    # 2. A flagged parse may become the active dataset, but only as REVIEWED_ACCEPTED, which
    #    guard 6 below ties to a recorded decision.
    op.execute(
        'ALTER TABLE parse_runs DROP CONSTRAINT IF EXISTS "ck_parse_runs_active_run_succeeded"'
    )
    op.execute(
        'ALTER TABLE parse_runs ADD CONSTRAINT "ck_parse_runs_active_run_usable" '
        "CHECK (NOT is_active OR status IN ('SUCCEEDED', 'REVIEWED_ACCEPTED'))"
    )

    op.create_table(
        "parse_review_decisions",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=6), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("validation_result_at_decision", sa.String(length=18), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("findings_digest", sa.String(length=64), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('ACCEPT')", name=op.f("ck_parse_review_decisions_decision_vocabulary")
        ),
        sa.CheckConstraint(
            "validation_result_at_decision = 'NEEDS_REVIEW'",
            name=op.f("ck_parse_review_decisions_reviewed_result_recorded"),
        ),
        sa.CheckConstraint(
            "finding_count >= 0", name=op.f("ck_parse_review_decisions_finding_count_not_negative")
        ),
        # Composite, so a decision cannot be recorded across a tenant boundary.
        sa.ForeignKeyConstraint(
            ["parse_run_id", "tenant_id"],
            ["parse_runs.id", "parse_runs.tenant_id"],
            name="fk_parse_review_decisions_parse_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name=op.f("fk_parse_review_decisions_reviewer_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parse_review_decisions")),
    )
    op.create_index(
        "ix_parse_review_decisions_tenant_id", "parse_review_decisions", ["tenant_id"]
    )
    op.create_index(
        "ix_parse_review_decisions_parse_run_id", "parse_review_decisions", ["parse_run_id"]
    )
    # One acceptance per run, ever: the idempotency fence, and what makes a second decision
    # impossible even if two curators submit concurrently.
    op.create_index(
        "uq_parse_review_decisions_accept",
        "parse_review_decisions",
        ["parse_run_id"],
        unique=True,
        postgresql_where=sa.text("decision = 'ACCEPT'"),
    )
    # A decision is operational history, on the same footing as findings and audit events.
    op.execute(
        "CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON parse_review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION m1_append_only()"
    )

    _install_review_guard()
    _install_accepting_job_guard()
    _install_chunk_run_guard(accept_reviewed=True)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM parse_runs WHERE status = 'REVIEWED_ACCEPTED')
      THEN RAISE EXCEPTION
        'Reviewed-accepted parses exist; they cannot be represented without this revision';
      END IF;
    END $$;
    """)
    _install_chunk_run_guard(accept_reviewed=False)
    _install_prior_job_guard()
    op.execute("DROP TRIGGER IF EXISTS parse_review_guard ON parse_runs")
    op.execute("DROP FUNCTION IF EXISTS parse_review_guard()")
    op.execute("DROP TRIGGER IF EXISTS append_only ON parse_review_decisions")
    op.drop_table("parse_review_decisions")
    op.execute('ALTER TABLE parse_runs DROP CONSTRAINT IF EXISTS "ck_parse_runs_active_run_usable"')
    op.execute(
        'ALTER TABLE parse_runs ADD CONSTRAINT "ck_parse_runs_active_run_succeeded" '
        "CHECK (NOT is_active OR status = 'SUCCEEDED')"
    )
    _parse_run_status(PRIOR_PARSE_RUN_STATUS, 9, 17)


def _install_review_guard() -> None:
    """REVIEWED_ACCEPTED is reachable only from a flagged parse with a decision of its own."""
    op.execute("""
    CREATE OR REPLACE FUNCTION parse_review_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.status = 'REVIEWED_ACCEPTED' AND OLD.status IS DISTINCT FROM 'REVIEWED_ACCEPTED' THEN
        -- Only a parse the quality layer flagged for review. A FAIL result shares the FAILED
        -- status and must never be laundered into acceptance through this path.
        IF OLD.status <> 'FAILED' OR OLD.validation_result <> 'NEEDS_REVIEW'
        THEN RAISE EXCEPTION 'Only a parse flagged for review can be accepted'; END IF;
        IF NOT EXISTS (SELECT 1 FROM parse_review_decisions d
                       WHERE d.parse_run_id = NEW.id AND d.decision = 'ACCEPT')
        THEN RAISE EXCEPTION 'A reviewed acceptance requires a recorded decision'; END IF;
      END IF;
      -- The automated verdict is immutable once written, for every run.
      IF OLD.validation_result IS NOT NULL
         AND NEW.validation_result IS DISTINCT FROM OLD.validation_result
      THEN RAISE EXCEPTION 'Automated validation results are immutable'; END IF;
      IF OLD.status = 'REVIEWED_ACCEPTED' AND NEW.status NOT IN
         ('REVIEWED_ACCEPTED', 'CANCELLED')
      THEN RAISE EXCEPTION 'A reviewed acceptance can only be cancelled'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER parse_review_guard BEFORE UPDATE ON parse_runs
      FOR EACH ROW EXECUTE FUNCTION parse_review_guard();
    """)


def _install_chunk_run_guard(accept_reviewed: bool) -> None:
    """Reinstall `m3_run_guard`, optionally admitting reviewed-accepted parses.

    Two separate clauses key on the parse: the INSERT branch on `status`, and the activation
    branch on `validation_result`. Because a reviewed parse keeps NEEDS_REVIEW forever, the
    second clause is the one that would otherwise let chunking finish and then refuse to
    activate the dataset it produced.
    """
    source_ready = (
        "p.is_active AND p.status IN ('SUCCEEDED','REVIEWED_ACCEPTED')"
        if accept_reviewed
        else "p.is_active AND p.status='SUCCEEDED'"
    )
    active_ready = (
        "(p.validation_result IN ('PASS','PASS_WITH_WARNINGS') OR p.status='REVIEWED_ACCEPTED')"
        if accept_reviewed
        else "p.validation_result IN ('PASS','PASS_WITH_WARNINGS')"
    )
    op.execute(f"""
    CREATE OR REPLACE FUNCTION m3_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.is_active
        THEN RAISE EXCEPTION 'Chunk runs must begin pending and inactive'; END IF;
        IF NOT EXISTS (SELECT 1 FROM document_versions v JOIN ingestion_jobs j ON j.document_version_id=v.id
          JOIN parse_runs p ON p.document_version_id=v.id WHERE v.id=NEW.document_version_id
          AND v.document_id=NEW.document_id AND v.tenant_id=NEW.tenant_id
          AND p.id=NEW.parse_run_id AND p.tenant_id=NEW.tenant_id
          AND j.id=NEW.ingestion_job_id AND j.tenant_id=NEW.tenant_id
          AND {source_ready})
        THEN RAISE EXCEPTION 'Chunk run source scope or readiness invalid'; END IF;
      ELSE
        IF ROW(NEW.tenant_id,NEW.document_id,NEW.document_version_id,NEW.parse_run_id,NEW.ingestion_job_id,
               NEW.generation,NEW.chunker_name,NEW.chunker_version,NEW.configuration_version,
               NEW.policy_fingerprint,NEW.config_snapshot,NEW.tokenizer_name,NEW.tokenizer_version,NEW.force)
           IS DISTINCT FROM ROW(OLD.tenant_id,OLD.document_id,OLD.document_version_id,OLD.parse_run_id,
               OLD.ingestion_job_id,OLD.generation,OLD.chunker_name,OLD.chunker_version,OLD.configuration_version,
               OLD.policy_fingerprint,OLD.config_snapshot,OLD.tokenizer_name,OLD.tokenizer_version,OLD.force)
        THEN RAISE EXCEPTION 'Chunk run identity is immutable'; END IF;
        IF OLD.status NOT IN ('PENDING','RUNNING') AND
          (to_jsonb(NEW)-'is_active'-'updated_at') IS DISTINCT FROM
          (to_jsonb(OLD)-'is_active'-'updated_at')
        THEN RAISE EXCEPTION 'Completed chunk runs are immutable'; END IF;
      END IF;
      IF NEW.is_active THEN
        IF NOT EXISTS (SELECT 1 FROM parse_runs p WHERE p.id=NEW.parse_run_id AND p.is_active
          AND {active_ready})
          OR NOT EXISTS (SELECT 1 FROM chunks c WHERE c.chunk_run_id=NEW.id)
          OR EXISTS (SELECT 1 FROM chunks c WHERE c.chunk_run_id=NEW.id AND
            NOT EXISTS (SELECT 1 FROM chunk_source_elements s WHERE s.chunk_id=c.id))
        THEN RAISE EXCEPTION 'An active dataset requires valid current parse and complete sources'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)


ACCEPT_CLAUSE = """
          -- Curator acceptance of a flagged parse. Deliberately outside the retry budget: the
          -- dataset being admitted is the one the curator examined, so nothing is reprocessed.
          -- Conditioned on the parse itself, so the edge cannot be taken without a decision.
          (OLD.status = 'NEEDS_REVIEW' AND NEW.status = 'READY_FOR_CHUNKING'
           AND NEW.retry_count = OLD.retry_count
           AND EXISTS (SELECT 1 FROM parse_runs p
                       WHERE p.document_version_id = NEW.document_version_id
                         AND p.is_active AND p.status = 'REVIEWED_ACCEPTED')) OR"""


def _job_guard(accept_clause: str) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION m1_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'UPLOADED' OR NEW.retry_count <> 0
        THEN RAISE EXCEPTION 'Jobs must begin UPLOADED'; END IF;
        RETURN NEW;
      END IF;
      IF ROW(NEW.document_version_id, NEW.tenant_id, NEW.requested_by_user_id,
             NEW.configuration_version, NEW.config_snapshot, NEW.max_retries)
         IS DISTINCT FROM ROW(OLD.document_version_id, OLD.tenant_id, OLD.requested_by_user_id,
             OLD.configuration_version, OLD.config_snapshot, OLD.max_retries)
      THEN RAISE EXCEPTION 'Job provenance and configuration are immutable'; END IF;
      IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT ({accept_clause}
          (OLD.status = 'UPLOADED' AND NEW.status IN ('VALIDATING', 'CANCELLED')) OR
          (OLD.status = 'VALIDATING' AND NEW.status IN
            ('QUEUED', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR
          (OLD.status = 'QUEUED' AND NEW.status IN
            ('PARSING', 'FAILED', 'QUARANTINED', 'CANCELLED')) OR
          (OLD.status = 'PARSING' AND NEW.status IN
            ('NORMALIZING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR
          (OLD.status = 'NORMALIZING' AND NEW.status IN
            ('ENRICHING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR
          (OLD.status = 'ENRICHING' AND NEW.status IN
            ('READY_FOR_CHUNKING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR
          (OLD.status = 'READY_FOR_CHUNKING' AND NEW.status IN
            ('CHUNKING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'CHUNKING' AND NEW.status IN
            ('VALIDATING_CHUNKS','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'VALIDATING_CHUNKS' AND NEW.status IN
            ('READY_FOR_EMBEDDING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'READY_FOR_EMBEDDING' AND NEW.status IN
            ('EMBEDDING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'EMBEDDING' AND NEW.status IN
            ('INDEXING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'INDEXING' AND NEW.status IN
            ('VERIFYING_INDEX','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'VERIFYING_INDEX' AND NEW.status IN
            ('READY_FOR_RETRIEVAL','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'READY_FOR_RETRIEVAL' AND NEW.status IN
            ('SPARSE_INDEXING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'SPARSE_INDEXING' AND NEW.status IN
            ('VERIFYING_SPARSE_INDEX','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status = 'VERIFYING_SPARSE_INDEX' AND NEW.status IN
            ('RETRIEVAL_READY','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR
          (OLD.status IN ('FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING',
                          'READY_FOR_EMBEDDING', 'READY_FOR_RETRIEVAL', 'RETRIEVAL_READY')
           AND NEW.status = 'CANCELLED') OR
          -- Retry, reparse, rechunk, re-embed and lexical rebuild all consume one unit of the
          -- same bounded budget, so no reprocessing path can loop without limit.
          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING', 'READY_FOR_EMBEDDING',
                          'READY_FOR_RETRIEVAL', 'RETRIEVAL_READY')
           AND NEW.status = 'VALIDATING'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries) OR
          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_EMBEDDING', 'READY_FOR_RETRIEVAL',
                          'RETRIEVAL_READY')
           AND NEW.status = 'READY_FOR_CHUNKING'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries) OR
          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_RETRIEVAL', 'RETRIEVAL_READY')
           AND NEW.status = 'READY_FOR_EMBEDDING'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries) OR
          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'RETRIEVAL_READY')
           AND NEW.status = 'READY_FOR_RETRIEVAL'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries))
        THEN RAISE EXCEPTION 'Illegal ingestion transition'; END IF;
      END IF;
      IF NEW.retry_count <> OLD.retry_count AND NOT
        (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING', 'READY_FOR_EMBEDDING',
                        'READY_FOR_RETRIEVAL', 'RETRIEVAL_READY')
         AND NEW.status IN ('VALIDATING','READY_FOR_CHUNKING','READY_FOR_EMBEDDING',
                            'READY_FOR_RETRIEVAL')
         AND NEW.retry_count = OLD.retry_count + 1)
      THEN RAISE EXCEPTION
        'Retry count can change only on a retry, reparse, rechunk, re-embed or lexical rebuild';
      END IF;
      RETURN NEW;
    END $$;
    """


def _install_accepting_job_guard() -> None:
    op.execute(_job_guard(ACCEPT_CLAUSE))


def _install_prior_job_guard() -> None:
    op.execute(_job_guard(""))

