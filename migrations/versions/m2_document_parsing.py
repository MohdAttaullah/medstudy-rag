"""M2 document parsing: parse runs, pages, elements and structured artifacts.

Adds READY_FOR_CHUNKING to the job/version status vocabulary and replaces the M1 transition
guard with the M2 graph. Reversible: downgrade drops the new tables, restores the M1 guard and
restores the M1 status vocabulary, refusing to run if any row already uses an M2-only status.

Revision ID: m2_document_parsing
Revises: m1_event_order
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m2_document_parsing"
down_revision = "m1_event_order"
branch_labels = None
depends_on = None

M1_STATUSES = (
    "UPLOADED",
    "VALIDATING",
    "QUEUED",
    "PARSING",
    "NORMALIZING",
    "CHUNKING",
    "ENRICHING",
    "EMBEDDING",
    "INDEXING",
    "VERIFYING_INDEX",
    "READY",
    "FAILED",
    "QUARANTINED",
    "NEEDS_REVIEW",
    "CANCELLED",
)
M2_STATUSES = (
    "UPLOADED",
    "VALIDATING",
    "QUEUED",
    "PARSING",
    "NORMALIZING",
    "ENRICHING",
    "READY_FOR_CHUNKING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "VERIFYING_INDEX",
    "READY",
    "FAILED",
    "QUARANTINED",
    "NEEDS_REVIEW",
    "CANCELLED",
)
STATUS_CONSTRAINTS = (
    ("document_versions", "ingestion_status", "version_status"),
    ("ingestion_jobs", "status", "job_status"),
)

PARSE_RUN_STATUS = ("RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
PARSE_RESULT = ("PASS", "PASS_WITH_WARNINGS", "NEEDS_REVIEW", "FAIL")
OCR_MODE = ("OFF", "AUTO", "FORCE")
SEVERITY = ("INFO", "WARNING", "ERROR", "CRITICAL")
COORDINATE_ORIGIN = ("TOPLEFT",)
ELEMENT_TYPE = (
    "TITLE",
    "HEADING",
    "PARAGRAPH",
    "LIST",
    "LIST_ITEM",
    "TABLE",
    "FORMULA",
    "FIGURE",
    "CAPTION",
    "FOOTNOTE",
    "PAGE_HEADER",
    "PAGE_FOOTER",
    "SECTION",
    "CODE",
    "OTHER",
)


def enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def bbox_columns() -> list[sa.Column]:
    return [
        sa.Column("bbox_x1", sa.Float(), nullable=True),
        sa.Column("bbox_y1", sa.Float(), nullable=True),
        sa.Column("bbox_x2", sa.Float(), nullable=True),
        sa.Column("bbox_y2", sa.Float(), nullable=True),
        sa.Column("bbox_origin", enum(COORDINATE_ORIGIN, "coordinate_origin"), nullable=True),
    ]


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _replace_status_constraint(values: tuple[str, ...], width: int) -> None:
    """Rewrite the status vocabulary.

    The status enums are non-native, so PostgreSQL stores them as VARCHAR sized to the longest
    permitted value plus a CHECK constraint. READY_FOR_CHUNKING is longer than every M1 value,
    so the column width moves with the vocabulary.
    """
    listed = ", ".join(f"'{value}'" for value in values)
    for table, column, name in STATUS_CONSTRAINTS:
        op.alter_column(
            table,
            column,
            type_=sa.String(length=width),
            existing_type=sa.String(length=15 if width != 15 else 18),
            existing_nullable=False,
        )
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "ck_{table}_{name}"')
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT "ck_{table}_{name}" '
            f"CHECK ({column} IN ({listed}))"
        )


def upgrade() -> None:
    _replace_status_constraint(M2_STATUSES, 18)
    _install_m2_job_guard()

    op.create_table(
        "parse_runs",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_job_id", sa.Uuid(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("parser_name", sa.String(length=60), nullable=False),
        sa.Column("parser_provider", sa.String(length=60), nullable=False),
        sa.Column("parser_version", sa.String(length=60), nullable=False),
        sa.Column("configuration_version", sa.String(length=80), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_object_version_id", sa.String(length=200), nullable=False),
        sa.Column("status", enum(PARSE_RUN_STATUS, "parse_run_status"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("raw_artifact_key", sa.String(length=600), nullable=True),
        sa.Column("raw_artifact_version_id", sa.String(length=200), nullable=True),
        sa.Column("raw_artifact_bytes", sa.Integer(), nullable=True),
        sa.Column("ocr_mode", enum(OCR_MODE, "ocr_mode"), nullable=False),
        sa.Column("ocr_engine", sa.String(length=60), nullable=True),
        sa.Column("tables_enabled", sa.Boolean(), nullable=False),
        sa.Column("formulas_enabled", sa.Boolean(), nullable=False),
        sa.Column("figures_enabled", sa.Boolean(), nullable=False),
        sa.Column("previews_enabled", sa.Boolean(), nullable=False),
        sa.Column("validation_result", enum(PARSE_RESULT, "parse_result"), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("source_page_count", sa.Integer(), nullable=True),
        sa.Column("element_count", sa.Integer(), nullable=True),
        sa.Column("table_count", sa.Integer(), nullable=True),
        sa.Column("figure_count", sa.Integer(), nullable=True),
        sa.Column("formula_count", sa.Integer(), nullable=True),
        sa.Column("ocr_page_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("worker_identity", sa.String(length=120), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "NOT is_active OR status = 'SUCCEEDED'",
            name=op.f("ck_parse_runs_active_run_succeeded"),
        ),
        sa.CheckConstraint("attempt > 0", name=op.f("ck_parse_runs_positive_attempt")),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_parse_runs_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_job_id"],
            ["ingestion_jobs.id"],
            name=op.f("fk_parse_runs_ingestion_job_id_ingestion_jobs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parse_runs")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_parse_runs_id")),
    )
    op.create_index(op.f("ix_parse_runs_tenant_id"), "parse_runs", ["tenant_id"])
    op.create_index(op.f("ix_parse_runs_status"), "parse_runs", ["status"])
    op.create_index(op.f("ix_parse_runs_lease_expires_at"), "parse_runs", ["lease_expires_at"])
    op.create_index("ix_parse_runs_document_version_id", "parse_runs", ["document_version_id"])
    # Exactly one active parse dataset per version; superseded runs stay for comparison.
    op.create_index(
        "uq_parse_runs_active_version",
        "parse_runs",
        ["document_version_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "document_pages",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("element_count", sa.Integer(), nullable=False),
        sa.Column("source_text_chars", sa.Integer(), nullable=False),
        sa.Column("ocr_used", sa.Boolean(), nullable=False),
        sa.Column("ocr_evidence", sa.String(length=60), nullable=True),
        sa.Column("preview_key", sa.String(length=600), nullable=True),
        sa.Column("preview_version_id", sa.String(length=200), nullable=True),
        sa.Column("preview_media_type", sa.String(length=60), nullable=True),
        *bbox_columns(),
        *timestamps(),
        sa.CheckConstraint(
            "page_number >= 1", name=op.f("ck_document_pages_page_number_is_one_based")
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0", name=op.f("ck_document_pages_positive_page_size")
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_document_pages_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint("id", "parse_run_id", name=op.f("uq_document_pages_id")),
        sa.UniqueConstraint(
            "parse_run_id", "page_number", name=op.f("uq_document_pages_parse_run_id")
        ),
    )
    op.create_index(op.f("ix_document_pages_tenant_id"), "document_pages", ["tenant_id"])
    op.create_index(
        op.f("ix_document_pages_document_version_id"), "document_pages", ["document_version_id"]
    )
    op.create_index(op.f("ix_document_pages_parse_run_id"), "document_pages", ["parse_run_id"])

    op.create_table(
        "document_elements",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("parent_element_id", sa.Uuid(), nullable=True),
        sa.Column("element_type", enum(ELEMENT_TYPE, "element_type"), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("reading_order", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("text_normalized", sa.Boolean(), nullable=False),
        sa.Column("parser_confidence", sa.Float(), nullable=True),
        sa.Column("source_parser_ref", sa.String(length=120), nullable=True),
        sa.Column("source_label", sa.String(length=60), nullable=True),
        sa.Column("content_layer", sa.String(length=30), nullable=True),
        sa.Column("structure_inferred", sa.Boolean(), nullable=False),
        sa.Column("element_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *bbox_columns(),
        *timestamps(),
        sa.CheckConstraint(
            "reading_order >= 0 AND ordinal >= 0",
            name=op.f("ck_document_elements_non_negative_order"),
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_document_elements_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_element_id"],
            ["document_elements.id"],
            name=op.f("fk_document_elements_parent_element_id_document_elements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_document_elements_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_elements")),
        sa.UniqueConstraint("id", "parse_run_id", name=op.f("uq_document_elements_id")),
        sa.UniqueConstraint(
            "parse_run_id", "reading_order", name=op.f("uq_document_elements_parse_run_id")
        ),
    )
    op.create_index(op.f("ix_document_elements_tenant_id"), "document_elements", ["tenant_id"])
    op.create_index(
        op.f("ix_document_elements_document_version_id"),
        "document_elements",
        ["document_version_id"],
    )
    op.create_index(
        op.f("ix_document_elements_parse_run_id"), "document_elements", ["parse_run_id"]
    )
    op.create_index(
        "ix_document_elements_run_page", "document_elements", ["parse_run_id", "page_id"]
    )
    op.create_index(
        "ix_document_elements_run_type", "document_elements", ["parse_run_id", "element_type"]
    )

    op.create_table(
        "table_artifacts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_element_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("caption_element_id", sa.Uuid(), nullable=True),
        sa.Column("caption_text", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("header_row_count", sa.Integer(), nullable=False),
        sa.Column("cells", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("table_group_id", sa.Uuid(), nullable=True),
        sa.Column("continuation_of_id", sa.Uuid(), nullable=True),
        sa.Column("possible_continuation", sa.Boolean(), nullable=False),
        sa.Column("continuation_evidence", sa.String(length=60), nullable=True),
        sa.Column("malformed", sa.Boolean(), nullable=False),
        sa.Column("parser_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *bbox_columns(),
        *timestamps(),
        sa.CheckConstraint(
            "row_count >= 0 AND column_count >= 0",
            name=op.f("ck_table_artifacts_non_negative_table_size"),
        ),
        sa.ForeignKeyConstraint(
            ["caption_element_id"],
            ["document_elements.id"],
            name=op.f("fk_table_artifacts_caption_element_id_document_elements"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["continuation_of_id"],
            ["table_artifacts.id"],
            name=op.f("fk_table_artifacts_continuation_of_id_table_artifacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_element_id"],
            ["document_elements.id"],
            name=op.f("fk_table_artifacts_document_element_id_document_elements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_table_artifacts_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_table_artifacts_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_artifacts")),
        sa.UniqueConstraint(
            "document_element_id", name=op.f("uq_table_artifacts_document_element_id")
        ),
    )
    op.create_index(op.f("ix_table_artifacts_tenant_id"), "table_artifacts", ["tenant_id"])
    op.create_index(
        op.f("ix_table_artifacts_document_version_id"), "table_artifacts", ["document_version_id"]
    )
    op.create_index(op.f("ix_table_artifacts_parse_run_id"), "table_artifacts", ["parse_run_id"])

    op.create_table(
        "figure_artifacts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_element_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("caption_element_id", sa.Uuid(), nullable=True),
        sa.Column("caption_text", sa.Text(), nullable=True),
        sa.Column("figure_kind", sa.String(length=40), nullable=True),
        sa.Column("image_key", sa.String(length=600), nullable=True),
        sa.Column("image_version_id", sa.String(length=200), nullable=True),
        sa.Column("image_media_type", sa.String(length=60), nullable=True),
        sa.Column("image_width", sa.Integer(), nullable=True),
        sa.Column("image_height", sa.Integer(), nullable=True),
        sa.Column("image_bytes", sa.Integer(), nullable=True),
        sa.Column("parser_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *bbox_columns(),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["caption_element_id"],
            ["document_elements.id"],
            name=op.f("fk_figure_artifacts_caption_element_id_document_elements"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_element_id"],
            ["document_elements.id"],
            name=op.f("fk_figure_artifacts_document_element_id_document_elements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_figure_artifacts_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_figure_artifacts_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_figure_artifacts")),
        sa.UniqueConstraint(
            "document_element_id", name=op.f("uq_figure_artifacts_document_element_id")
        ),
    )
    op.create_index(op.f("ix_figure_artifacts_tenant_id"), "figure_artifacts", ["tenant_id"])
    op.create_index(
        op.f("ix_figure_artifacts_document_version_id"),
        "figure_artifacts",
        ["document_version_id"],
    )
    op.create_index(op.f("ix_figure_artifacts_parse_run_id"), "figure_artifacts", ["parse_run_id"])

    op.create_table(
        "formula_artifacts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_element_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("source_expression", sa.Text(), nullable=True),
        sa.Column("normalized_expression", sa.Text(), nullable=True),
        sa.Column("notation", sa.String(length=40), nullable=True),
        sa.Column("preceding_element_id", sa.Uuid(), nullable=True),
        sa.Column("following_element_id", sa.Uuid(), nullable=True),
        sa.Column("parser_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *bbox_columns(),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["document_element_id"],
            ["document_elements.id"],
            name=op.f("fk_formula_artifacts_document_element_id_document_elements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["following_element_id"],
            ["document_elements.id"],
            name=op.f("fk_formula_artifacts_following_element_id_document_elements"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_formula_artifacts_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_formula_artifacts_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preceding_element_id"],
            ["document_elements.id"],
            name=op.f("fk_formula_artifacts_preceding_element_id_document_elements"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_formula_artifacts")),
        sa.UniqueConstraint(
            "document_element_id", name=op.f("uq_formula_artifacts_document_element_id")
        ),
    )
    op.create_index(op.f("ix_formula_artifacts_tenant_id"), "formula_artifacts", ["tenant_id"])
    op.create_index(
        op.f("ix_formula_artifacts_document_version_id"),
        "formula_artifacts",
        ["document_version_id"],
    )
    op.create_index(
        op.f("ix_formula_artifacts_parse_run_id"), "formula_artifacts", ["parse_run_id"]
    )

    op.create_table(
        "parse_validation_findings",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("document_element_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("severity", enum(SEVERITY, "finding_severity"), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["document_element_id"],
            ["document_elements.id"],
            name=op.f("fk_parse_validation_findings_document_element_id_document_elements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["document_pages.id"],
            name=op.f("fk_parse_validation_findings_page_id_document_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parse_run_id"],
            ["parse_runs.id"],
            name=op.f("fk_parse_validation_findings_parse_run_id_parse_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parse_validation_findings")),
    )
    op.create_index(
        op.f("ix_parse_validation_findings_tenant_id"), "parse_validation_findings", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_parse_validation_findings_parse_run_id"),
        "parse_validation_findings",
        ["parse_run_id"],
    )
    op.create_index(
        op.f("ix_parse_validation_findings_code"), "parse_validation_findings", ["code"]
    )
    op.create_index(
        "ix_parse_validation_findings_run_severity",
        "parse_validation_findings",
        ["parse_run_id", "severity"],
    )

    # Findings are operational history: append-only, like stage events and audit rows.
    op.execute(
        "CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON parse_validation_findings "
        "FOR EACH ROW EXECUTE FUNCTION m1_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS append_only ON parse_validation_findings")
    for table in (
        "parse_validation_findings",
        "formula_artifacts",
        "figure_artifacts",
        "table_artifacts",
        "document_elements",
        "document_pages",
        "parse_runs",
    ):
        op.drop_table(table)
    # Refuse a silent data loss: M2-only statuses cannot be represented by the M1 vocabulary.
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM ingestion_jobs WHERE status = 'READY_FOR_CHUNKING')
         OR EXISTS (SELECT 1 FROM document_versions WHERE ingestion_status = 'READY_FOR_CHUNKING')
      THEN RAISE EXCEPTION
        'Cancel or fail READY_FOR_CHUNKING jobs before downgrading past M2';
      END IF;
    END $$;
    """)
    _install_m1_job_guard()
    _replace_status_constraint(M1_STATUSES, 15)


def _install_m2_job_guard() -> None:
    """Executable M2 transition graph, enforced in the database as well as the application."""
    op.execute("""
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
        IF NOT (
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
          (OLD.status IN ('FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING')
           AND NEW.status = 'CANCELLED') OR
          -- Retry of a failure, and explicit operator-requested reparse of a completed or
          -- flagged version. Both consume one unit of the same bounded retry budget.
          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING')
           AND NEW.status = 'VALIDATING'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries))
        THEN RAISE EXCEPTION 'Illegal ingestion transition'; END IF;
      END IF;
      IF NEW.retry_count <> OLD.retry_count AND NOT
        (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING')
         AND NEW.status = 'VALIDATING' AND NEW.retry_count = OLD.retry_count + 1)
      THEN RAISE EXCEPTION 'Retry count can change only on a retry or reparse'; END IF;
      RETURN NEW;
    END $$;
    """)


def _install_m1_job_guard() -> None:
    op.execute("""
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
        IF NOT (
          (OLD.status = 'UPLOADED' AND NEW.status IN ('VALIDATING', 'CANCELLED')) OR
          (OLD.status = 'VALIDATING' AND NEW.status IN
            ('QUEUED', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR
          (OLD.status = 'QUEUED' AND NEW.status IN ('FAILED', 'QUARANTINED', 'CANCELLED')) OR
          (OLD.status IN ('FAILED', 'QUARANTINED', 'NEEDS_REVIEW') AND NEW.status = 'CANCELLED') OR
          (OLD.status = 'FAILED' AND NEW.status = 'VALIDATING'
           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries))
        THEN RAISE EXCEPTION 'Illegal M1 ingestion transition'; END IF;
      END IF;
      IF NEW.retry_count <> OLD.retry_count AND NOT
        (OLD.status = 'FAILED' AND NEW.status = 'VALIDATING'
         AND NEW.retry_count = OLD.retry_count + 1)
      THEN RAISE EXCEPTION 'Retry count can change only on a retry'; END IF;
      RETURN NEW;
    END $$;
    """)
