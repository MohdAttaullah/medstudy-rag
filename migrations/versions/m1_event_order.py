"""Stable per-job history ordering, including timestamp ties."""

from alembic import op
import sqlalchemy as sa

revision = "m1_event_order"
down_revision = "0dd8e0dcb035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingestion_stage_events", sa.Column("sequence", sa.Integer(), nullable=True))
    op.execute("ALTER TABLE ingestion_stage_events DISABLE TRIGGER append_only")
    op.execute("""
        WITH ordered AS (
          SELECT id, row_number() OVER (
            PARTITION BY ingestion_job_id ORDER BY created_at,
            CASE WHEN from_status IS NULL THEN 0 ELSE 1 END, id) AS position
          FROM ingestion_stage_events
        )
        UPDATE ingestion_stage_events SET sequence = ordered.position
        FROM ordered WHERE ingestion_stage_events.id = ordered.id
    """)
    op.execute("ALTER TABLE ingestion_stage_events ENABLE TRIGGER append_only")
    op.alter_column("ingestion_stage_events", "sequence", nullable=False)
    op.create_unique_constraint(
        "uq_ingestion_stage_events_ingestion_job_id",
        "ingestion_stage_events",
        ["ingestion_job_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_ingestion_stage_events_ingestion_job_id", "ingestion_stage_events")
    op.drop_column("ingestion_stage_events", "sequence")
