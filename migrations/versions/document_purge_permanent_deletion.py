"""Permanent document deletion: one sanctioned path through the append-only guards.

Post-M12 product work, not a milestone. The decision this implements is ADR-026.

Until now a document could only be *withdrawn* (archived): every row, object and vector stayed.
`docs/architecture/retention.md` recorded why a real deletion had not been built — the guards that
make operational history append-only and completed datasets immutable would reject it, and a
verified answer's citations were bound to the source by a foreign key, so deleting the source
meant either orphaning the answer or destroying the user's history. ADR-026 decides that conflict:
the answer stays, its citations lose their source content and say the source was deleted.

What this migration changes, and only this:

* ``medrag_purging(document)`` reports whether the current transaction is purging that exact
  document. The purge sets ``medrag.purge_document`` with ``set_config(..., true)``, so the value
  is transaction-local and disappears at commit or rollback.
* The three dataset guards (chunks, embeddings, lexical postings) and the five document-owned
  append-only history tables let a ``DELETE`` through **only** for rows of the document being
  purged. Every other write they refused before, they still refuse — including every ``UPDATE``,
  and every ``DELETE`` outside a purge.
* ``audit_events`` and ``configuration_revisions`` are deliberately untouched. Their triggers keep
  their original functions; no purge can remove them. The deletion is itself recorded there.
* ``document_deletions`` is the workflow record and the tombstone. It holds identifiers, timestamps,
  the deleting principal and counts of what was removed — never a title, text or storage key — so
  it can outlive the document without retaining any of its content.
* ``turn_citations`` loses its foreign key to ``documents`` (a deleted document's citation keeps the
  identifier as history) and gains ``source_deleted_at``.

Downgrade restores the original guard functions and triggers and drops the tombstone table. It
re-adds the citation foreign key, which a citation to a deleted document cannot satisfy; removing
such citations means removing conversation history, so it is refused unless
``MEDRAG_ALLOW_CONVERSATION_LOSS=1`` — the same opt-in the M9 migration uses.

Revision ID: document_purge
Revises: out_of_scope_outcome
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

ORPHANED_CITATIONS = (
    "{verb} FROM turn_citations c WHERE NOT EXISTS "
    "(SELECT 1 FROM documents d WHERE d.id = c.document_id AND d.tenant_id = c.tenant_id)"
)

revision = "document_purge"
down_revision = "out_of_scope_outcome"
branch_labels = None
depends_on = None

PURGING = """
CREATE OR REPLACE FUNCTION medrag_purging(document uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  -- True only inside the one transaction that is purging exactly this document.
  SELECT document IS NOT NULL
     AND coalesce(current_setting('medrag.purge_document', true), '') = document::text
$$;
"""

M3_GUARD = """
CREATE OR REPLACE FUNCTION m3_dataset_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE run_id uuid; run_status text;
    BEGIN
      IF TG_TABLE_NAME='question_options' THEN
        SELECT q.chunk_run_id INTO run_id FROM question_artifacts q
          WHERE q.id=(CASE WHEN TG_OP='DELETE' THEN OLD.question_id ELSE NEW.question_id END);
      ELSE
        run_id = CASE WHEN TG_OP='DELETE' THEN OLD.chunk_run_id ELSE NEW.chunk_run_id END;
      END IF;
      -- Permanent deletion (ADR-026): the only way a completed chunk dataset may lose rows.
      IF TG_OP='DELETE' AND EXISTS (
        SELECT 1 FROM chunk_runs c WHERE c.id=run_id AND medrag_purging(c.document_id)
      ) THEN RETURN OLD; END IF;
      SELECT status INTO run_status FROM chunk_runs WHERE id=run_id;
      IF run_status IS DISTINCT FROM 'RUNNING'
      THEN RAISE EXCEPTION 'Completed chunk datasets cannot be changed'; END IF;
      IF TG_TABLE_NAME='chunk_source_elements' AND TG_OP <> 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM document_elements e WHERE e.id=NEW.element_id
          AND e.parse_run_id=NEW.parse_run_id
          AND NEW.end_offset <= length(coalesce(e.normalized_text,'')))
        THEN RAISE EXCEPTION 'Source offsets do not resolve'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""

M4_GUARD = """
CREATE OR REPLACE FUNCTION m4_record_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE owner_status text; owner_id uuid;
    BEGIN
      owner_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.embedding_run_id ELSE NEW.embedding_run_id END;
      IF owner_id IS NULL THEN RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END; END IF;
      -- Permanent deletion (ADR-026): the only way a completed embedding dataset may lose rows.
      IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM embedding_runs r WHERE r.id = owner_id AND medrag_purging(r.document_id)
      ) THEN RETURN OLD; END IF;
      SELECT status INTO owner_status FROM embedding_runs WHERE id = owner_id;
      IF owner_status IS DISTINCT FROM 'RUNNING'
      THEN RAISE EXCEPTION 'Completed embedding datasets cannot be changed'; END IF;
      IF TG_TABLE_NAME = 'chunk_embeddings' AND TG_OP <> 'DELETE' THEN
        -- The vector must describe a chunk of the run's own dataset, in the run's own tenant.
        IF NOT EXISTS (SELECT 1 FROM chunks c JOIN embedding_runs r ON r.id = NEW.embedding_run_id
          WHERE c.id = NEW.chunk_id AND c.chunk_run_id = r.chunk_run_id
            AND c.tenant_id = r.tenant_id AND NEW.tenant_id = r.tenant_id)
        THEN RAISE EXCEPTION 'Embedding source chunk scope invalid'; END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""

M5_GUARD = """
CREATE OR REPLACE FUNCTION m5_sparse_record_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE owner_status text; owner_id uuid;
    BEGIN
      owner_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.sparse_index_id ELSE NEW.sparse_index_id END;
      IF owner_id IS NULL THEN RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END; END IF;
      -- Permanent deletion (ADR-026): the only way a completed lexical index may lose rows.
      IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM sparse_indexes s WHERE s.id = owner_id AND medrag_purging(s.document_id)
      ) THEN RETURN OLD; END IF;
      SELECT status INTO owner_status FROM sparse_indexes WHERE id = owner_id;
      IF owner_status IS DISTINCT FROM 'STAGING'
      THEN RAISE EXCEPTION 'Completed lexical indexes cannot be changed'; END IF;
      IF TG_TABLE_NAME = 'sparse_documents' AND TG_OP <> 'DELETE' THEN
        -- The row must describe a chunk of the index's own dataset, in the index's own tenant.
        IF NOT EXISTS (SELECT 1 FROM chunks c JOIN sparse_indexes s ON s.id = NEW.sparse_index_id
          WHERE c.id = NEW.chunk_id AND c.chunk_run_id = s.chunk_run_id
            AND c.tenant_id = s.tenant_id AND NEW.tenant_id = s.tenant_id)
        THEN RAISE EXCEPTION 'Lexical source chunk scope invalid'; END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""

# Document-owned operational history. Append-only exactly as before, except that a purge of the
# owning document may remove it. Each branch resolves the row to its document through the parent
# that still exists at that point of the purge; the purge deletes history before its parents.
HISTORY = """
CREATE OR REPLACE FUNCTION document_history_append_only() RETURNS trigger
LANGUAGE plpgsql AS $function$
    DECLARE owner uuid;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF TG_TABLE_NAME = 'ingestion_stage_events' THEN
          SELECT v.document_id INTO owner FROM ingestion_jobs j
            JOIN document_versions v ON v.id = j.document_version_id
            WHERE j.id = OLD.ingestion_job_id;
        ELSIF TG_TABLE_NAME IN ('parse_validation_findings', 'parse_review_decisions') THEN
          SELECT v.document_id INTO owner FROM parse_runs p
            JOIN document_versions v ON v.id = p.document_version_id
            WHERE p.id = OLD.parse_run_id;
        ELSIF TG_TABLE_NAME = 'index_validation_findings' THEN
          SELECT coalesce(
            (SELECT r.document_id FROM embedding_runs r WHERE r.id = OLD.embedding_run_id),
            (SELECT i.document_id FROM index_runs i WHERE i.id = OLD.index_run_id)
          ) INTO owner;
        ELSIF TG_TABLE_NAME = 'sparse_validation_findings' THEN
          SELECT s.document_id INTO owner FROM sparse_indexes s WHERE s.id = OLD.sparse_index_id;
        END IF;
        IF medrag_purging(owner) THEN RETURN OLD; END IF;
      END IF;
      RAISE EXCEPTION 'Operational history is append-only';
    END $function$;
"""

# (table, trigger name) for the five document-owned history tables. audit_events is not here.
HISTORY_TRIGGERS = (
    ("ingestion_stage_events", "append_only"),
    ("parse_validation_findings", "append_only"),
    ("parse_review_decisions", "append_only"),
    ("index_validation_findings", "index_findings_append_only"),
    ("sparse_validation_findings", "sparse_findings_append_only"),
)

# The original function bodies, restored verbatim on downgrade.
M3_ORIGINAL = """
CREATE OR REPLACE FUNCTION m3_dataset_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE run_id uuid; run_status text;
    BEGIN
      IF TG_TABLE_NAME='question_options' THEN
        SELECT q.chunk_run_id INTO run_id FROM question_artifacts q
          WHERE q.id=(CASE WHEN TG_OP='DELETE' THEN OLD.question_id ELSE NEW.question_id END);
      ELSE
        run_id = CASE WHEN TG_OP='DELETE' THEN OLD.chunk_run_id ELSE NEW.chunk_run_id END;
      END IF;
      SELECT status INTO run_status FROM chunk_runs WHERE id=run_id;
      IF run_status IS DISTINCT FROM 'RUNNING'
      THEN RAISE EXCEPTION 'Completed chunk datasets cannot be changed'; END IF;
      IF TG_TABLE_NAME='chunk_source_elements' AND TG_OP <> 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM document_elements e WHERE e.id=NEW.element_id
          AND e.parse_run_id=NEW.parse_run_id
          AND NEW.end_offset <= length(coalesce(e.normalized_text,'')))
        THEN RAISE EXCEPTION 'Source offsets do not resolve'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""

M4_ORIGINAL = """
CREATE OR REPLACE FUNCTION m4_record_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE owner_status text; owner_id uuid;
    BEGIN
      owner_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.embedding_run_id ELSE NEW.embedding_run_id END;
      IF owner_id IS NULL THEN RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END; END IF;
      SELECT status INTO owner_status FROM embedding_runs WHERE id = owner_id;
      IF owner_status IS DISTINCT FROM 'RUNNING'
      THEN RAISE EXCEPTION 'Completed embedding datasets cannot be changed'; END IF;
      IF TG_TABLE_NAME = 'chunk_embeddings' AND TG_OP <> 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM chunks c JOIN embedding_runs r ON r.id = NEW.embedding_run_id
          WHERE c.id = NEW.chunk_id AND c.chunk_run_id = r.chunk_run_id
            AND c.tenant_id = r.tenant_id AND NEW.tenant_id = r.tenant_id)
        THEN RAISE EXCEPTION 'Embedding source chunk scope invalid'; END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""

M5_ORIGINAL = """
CREATE OR REPLACE FUNCTION m5_sparse_record_guard() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE owner_status text; owner_id uuid;
    BEGIN
      owner_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.sparse_index_id ELSE NEW.sparse_index_id END;
      IF owner_id IS NULL THEN RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END; END IF;
      SELECT status INTO owner_status FROM sparse_indexes WHERE id = owner_id;
      IF owner_status IS DISTINCT FROM 'STAGING'
      THEN RAISE EXCEPTION 'Completed lexical indexes cannot be changed'; END IF;
      IF TG_TABLE_NAME = 'sparse_documents' AND TG_OP <> 'DELETE' THEN
        IF NOT EXISTS (SELECT 1 FROM chunks c JOIN sparse_indexes s ON s.id = NEW.sparse_index_id
          WHERE c.id = NEW.chunk_id AND c.chunk_run_id = s.chunk_run_id
            AND c.tenant_id = s.tenant_id AND NEW.tenant_id = s.tenant_id)
        THEN RAISE EXCEPTION 'Lexical source chunk scope invalid'; END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $function$;
"""


def upgrade() -> None:
    op.execute(PURGING)
    op.execute(M3_GUARD)
    op.execute(M4_GUARD)
    op.execute(M5_GUARD)
    op.execute(HISTORY)
    for table, trigger in HISTORY_TRIGGERS:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION document_history_append_only()"
        )

    op.create_table(
        "document_deletions",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # Deliberately not a foreign key: the tombstone outlives the document it records.
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("removed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'COMPLETED', 'FAILED')", name="deletion_status"
        ),
        sa.CheckConstraint("attempts >= 1", name="deletion_attempts"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "document_id", name="uq_document_deletion"),
    )
    op.create_index(
        op.f("ix_document_deletions_tenant_id"), "document_deletions", ["tenant_id"], unique=False
    )

    op.drop_constraint(
        "fk_turn_citations_document_id_documents", "turn_citations", type_="foreignkey"
    )
    op.add_column(
        "turn_citations", sa.Column("source_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    # A citation to a deleted document cannot satisfy the restored key. Satisfying it means removing
    # those citations from users' conversation history, so — exactly as the M9 migration protects
    # question history — that requires the explicit opt-in, and is refused otherwise.
    orphans = op.get_bind().execute(sa.text(ORPHANED_CITATIONS.format(verb="SELECT count(*)")))
    if orphans.scalar():
        if os.environ.get("MEDRAG_ALLOW_CONVERSATION_LOSS") != "1":
            raise RuntimeError(
                "Citations refer to permanently deleted documents. Downgrading would remove them "
                "from conversation history; set MEDRAG_ALLOW_CONVERSATION_LOSS=1 to accept that."
            )
        op.execute(ORPHANED_CITATIONS.format(verb="DELETE"))
    op.drop_column("turn_citations", "source_deleted_at")
    op.create_foreign_key(
        "fk_turn_citations_document_id_documents",
        "turn_citations",
        "documents",
        ["document_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.drop_index(op.f("ix_document_deletions_tenant_id"), table_name="document_deletions")
    op.drop_table("document_deletions")
    for table, trigger in HISTORY_TRIGGERS:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION m1_append_only()"
        )
    op.execute("DROP FUNCTION document_history_append_only()")
    op.execute(M5_ORIGINAL)
    op.execute(M4_ORIGINAL)
    op.execute(M3_ORIGINAL)
    op.execute("DROP FUNCTION medrag_purging(uuid)")
