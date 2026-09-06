"""M3 hierarchical chunks and source provenance

Revision ID: m3_hierarchical_chunks
Revises: m2_document_parsing
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'm3_hierarchical_chunks'
down_revision = 'm2_document_parsing'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_unique_constraint(op.f('uq_figure_artifacts_id'), 'figure_artifacts', ['id', 'parse_run_id'])
    op.create_unique_constraint(op.f('uq_formula_artifacts_id'), 'formula_artifacts', ['id', 'parse_run_id'])
    op.create_unique_constraint('uq_parse_runs_id_version_tenant', 'parse_runs', ['id', 'document_version_id', 'tenant_id'])
    op.create_unique_constraint(op.f('uq_table_artifacts_id'), 'table_artifacts', ['id', 'parse_run_id'])
    _m3_statuses()
    op.create_table('chunk_runs', sa.Column('tenant_id', sa.Uuid(), nullable=False), sa.Column('document_id', sa.Uuid(), nullable=False), sa.Column('document_version_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('ingestion_job_id', sa.Uuid(), nullable=False), sa.Column('generation', sa.Integer(), nullable=False), sa.Column('chunker_name', sa.String(length=80), nullable=False), sa.Column('chunker_version', sa.String(length=80), nullable=False), sa.Column('configuration_version', sa.String(length=80), nullable=False), sa.Column('policy_fingerprint', sa.String(length=64), nullable=False), sa.Column('config_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column('tokenizer_name', sa.String(length=100), nullable=False), sa.Column('tokenizer_version', sa.String(length=100), nullable=False), sa.Column('input_fingerprint', sa.String(length=64), nullable=True), sa.Column('status', sa.String(length=24), nullable=False), sa.Column('validation_result', sa.String(length=24), nullable=True), sa.Column('is_active', sa.Boolean(), server_default='false', nullable=False), sa.Column('force', sa.Boolean(), server_default='false', nullable=False), sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column('started_at', sa.DateTime(timezone=True), nullable=True), sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True), sa.Column('lease_token', sa.Uuid(), nullable=True), sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True), sa.Column('duration_ms', sa.Integer(), nullable=True), sa.Column('worker_identity', sa.String(length=120), nullable=True), sa.Column('error_code', sa.String(length=80), nullable=True), sa.Column('error_message', sa.String(length=300), nullable=True), sa.Column('correlation_id', sa.Uuid(), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("NOT is_active OR (status = 'SUCCEEDED' AND validation_result IS NOT NULL AND validation_result IN ('PASS','PASS_WITH_WARNINGS'))", name=op.f('ck_chunk_runs_active_chunks_validated')), sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_REVIEW','CANCELLED')", name=op.f('ck_chunk_runs_chunk_run_status')), sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_chunk_runs_document_id_documents')), sa.ForeignKeyConstraint(['ingestion_job_id'], ['ingestion_jobs.id'], name=op.f('fk_chunk_runs_ingestion_job_id_ingestion_jobs')), sa.ForeignKeyConstraint(['parse_run_id', 'document_version_id', 'tenant_id'], ['parse_runs.id', 'parse_runs.document_version_id', 'parse_runs.tenant_id'], name=op.f('fk_chunk_runs_parse_run_id_parse_runs')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_runs')), sa.UniqueConstraint('id', 'parse_run_id', 'tenant_id', name='uq_chunk_runs_id_parse_run_id_tenant_id'), sa.UniqueConstraint('id', 'parse_run_id', name='uq_chunk_runs_id_parse_run_id'), sa.UniqueConstraint('ingestion_job_id', 'generation', name='uq_chunk_runs_ingestion_job_id_generation'))
    op.create_index(op.f('ix_chunk_runs_document_version_id'), 'chunk_runs', ['document_version_id'], unique=False)
    op.create_index(op.f('ix_chunk_runs_ingestion_job_id'), 'chunk_runs', ['ingestion_job_id'], unique=False)
    op.create_index(op.f('ix_chunk_runs_lease_expires_at'), 'chunk_runs', ['lease_expires_at'], unique=False)
    op.create_index(op.f('ix_chunk_runs_parse_run_id'), 'chunk_runs', ['parse_run_id'], unique=False)
    op.create_index(op.f('ix_chunk_runs_status'), 'chunk_runs', ['status'], unique=False)
    op.create_index(op.f('ix_chunk_runs_tenant_id'), 'chunk_runs', ['tenant_id'], unique=False)
    op.create_index('uq_chunk_run_active_version', 'chunk_runs', ['document_version_id'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_index('uq_chunk_run_pending_version', 'chunk_runs', ['document_version_id'], unique=True, postgresql_where=sa.text("status IN ('PENDING','RUNNING')"))
    op.create_table('question_artifacts', sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('tenant_id', sa.Uuid(), nullable=False), sa.Column('question_hash', sa.String(length=64), nullable=False), sa.Column('question_number', sa.String(length=30), nullable=True), sa.Column('question_text', sa.Text(), nullable=False), sa.Column('question_type', sa.String(length=30), nullable=False), sa.Column('explicit_answer', sa.Text(), nullable=True), sa.Column('explanation', sa.Text(), nullable=True), sa.Column('extraction_status', sa.String(length=40), nullable=False), sa.Column('structure_inferred', sa.Boolean(), nullable=False), sa.Column('answer_inferred', sa.Boolean(), server_default='false', nullable=False), sa.Column('authority', postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column('page_start', sa.Integer(), nullable=True), sa.Column('page_end', sa.Integer(), nullable=True), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('NOT answer_inferred', name=op.f('ck_question_artifacts_never_infer_answers')), sa.ForeignKeyConstraint(['chunk_run_id', 'parse_run_id', 'tenant_id'], ['chunk_runs.id', 'chunk_runs.parse_run_id', 'chunk_runs.tenant_id'], name=op.f('fk_question_artifacts_chunk_run_id_chunk_runs')), sa.PrimaryKeyConstraint('id', name=op.f('pk_question_artifacts')), sa.UniqueConstraint('chunk_run_id', 'question_hash', name='uq_question_artifacts_chunk_run_id_question_hash'), sa.UniqueConstraint('id', 'chunk_run_id', name='uq_question_artifacts_id_chunk_run_id'))
    op.create_index(op.f('ix_question_artifacts_chunk_run_id'), 'question_artifacts', ['chunk_run_id'], unique=False)
    op.create_table('chunks', sa.Column('tenant_id', sa.Uuid(), nullable=False), sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('parent_chunk_id', sa.Uuid(), nullable=True), sa.Column('question_id', sa.Uuid(), nullable=True), sa.Column('chunk_type', sa.String(length=40), nullable=False), sa.Column('sequence_number', sa.Integer(), nullable=False), sa.Column('raw_text', sa.Text(), nullable=False), sa.Column('normalized_text', sa.Text(), nullable=False), sa.Column('retrieval_text', sa.Text(), nullable=False), sa.Column('token_count', sa.Integer(), nullable=False), sa.Column('retrieval_token_count', sa.Integer(), nullable=False), sa.Column('page_start', sa.Integer(), nullable=True), sa.Column('page_end', sa.Integer(), nullable=True), sa.Column('chunk_hash', sa.String(length=64), nullable=False), sa.Column('chunk_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('page_start >= 1 AND page_end >= page_start', name=op.f('ck_chunks_chunk_page_range')), sa.CheckConstraint('parent_chunk_id IS NULL OR parent_chunk_id <> id', name=op.f('ck_chunks_no_self_parent')), sa.CheckConstraint('token_count >= 0 AND retrieval_token_count >= 0', name=op.f('ck_chunks_chunk_tokens_nonnegative')), sa.ForeignKeyConstraint(['chunk_run_id', 'parse_run_id', 'tenant_id'], ['chunk_runs.id', 'chunk_runs.parse_run_id', 'chunk_runs.tenant_id'], name=op.f('fk_chunks_chunk_run_id_chunk_runs')), sa.ForeignKeyConstraint(['parent_chunk_id', 'chunk_run_id'], ['chunks.id', 'chunks.chunk_run_id'], name=op.f('fk_chunks_parent_chunk_id_chunks')), sa.ForeignKeyConstraint(['question_id', 'chunk_run_id'], ['question_artifacts.id', 'question_artifacts.chunk_run_id'], name=op.f('fk_chunks_question_id_question_artifacts')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks')), sa.UniqueConstraint('chunk_run_id', 'chunk_hash', name='uq_chunks_chunk_run_id_chunk_hash'), sa.UniqueConstraint('chunk_run_id', 'sequence_number', name='uq_chunks_chunk_run_id_sequence_number'), sa.UniqueConstraint('id', 'chunk_run_id', 'parse_run_id', name='uq_chunks_id_chunk_run_id_parse_run_id'), sa.UniqueConstraint('id', 'chunk_run_id', name='uq_chunks_id_chunk_run_id'))
    op.create_index(op.f('ix_chunks_chunk_run_id'), 'chunks', ['chunk_run_id'], unique=False)
    op.create_index(op.f('ix_chunks_chunk_type'), 'chunks', ['chunk_type'], unique=False)
    op.create_index(op.f('ix_chunks_tenant_id'), 'chunks', ['tenant_id'], unique=False)
    op.create_table('question_options', sa.Column('question_id', sa.Uuid(), nullable=False), sa.Column('ordinal', sa.Integer(), nullable=False), sa.Column('label', sa.String(length=10), nullable=False), sa.Column('text', sa.Text(), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('ordinal >= 0', name=op.f('ck_question_options_option_order_nonnegative')), sa.ForeignKeyConstraint(['question_id'], ['question_artifacts.id'], name=op.f('fk_question_options_question_id_question_artifacts')), sa.PrimaryKeyConstraint('id', name=op.f('pk_question_options')), sa.UniqueConstraint('question_id', 'label', name='uq_question_options_question_id_label'), sa.UniqueConstraint('question_id', 'ordinal', name='uq_question_options_question_id_ordinal'))
    op.create_index(op.f('ix_question_options_question_id'), 'question_options', ['question_id'], unique=False)
    op.create_table('chunk_artifact_relations', sa.Column('chunk_id', sa.Uuid(), nullable=False), sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('table_id', sa.Uuid(), nullable=True), sa.Column('figure_id', sa.Uuid(), nullable=True), sa.Column('formula_id', sa.Uuid(), nullable=True), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('num_nonnulls(table_id,figure_id,formula_id) = 1', name=op.f('ck_chunk_artifact_relations_exactly_one_source_artifact')), sa.ForeignKeyConstraint(['chunk_id', 'chunk_run_id', 'parse_run_id'], ['chunks.id', 'chunks.chunk_run_id', 'chunks.parse_run_id'], name=op.f('fk_chunk_artifact_relations_chunk_id_chunks')), sa.ForeignKeyConstraint(['figure_id', 'parse_run_id'], ['figure_artifacts.id', 'figure_artifacts.parse_run_id'], name=op.f('fk_chunk_artifact_relations_figure_id_figure_artifacts')), sa.ForeignKeyConstraint(['formula_id', 'parse_run_id'], ['formula_artifacts.id', 'formula_artifacts.parse_run_id'], name=op.f('fk_chunk_artifact_relations_formula_id_formula_artifacts')), sa.ForeignKeyConstraint(['table_id', 'parse_run_id'], ['table_artifacts.id', 'table_artifacts.parse_run_id'], name=op.f('fk_chunk_artifact_relations_table_id_table_artifacts')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_artifact_relations')))
    op.create_index(op.f('ix_chunk_artifact_relations_chunk_id'), 'chunk_artifact_relations', ['chunk_id'], unique=False)
    op.create_table('chunk_relations', sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('source_id', sa.Uuid(), nullable=False), sa.Column('target_id', sa.Uuid(), nullable=False), sa.Column('relation', sa.String(length=40), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('source_id <> target_id', name=op.f('ck_chunk_relations_chunk_relation_not_self')), sa.ForeignKeyConstraint(['source_id', 'chunk_run_id'], ['chunks.id', 'chunks.chunk_run_id'], name=op.f('fk_chunk_relations_source_id_chunks')), sa.ForeignKeyConstraint(['target_id', 'chunk_run_id'], ['chunks.id', 'chunks.chunk_run_id'], name=op.f('fk_chunk_relations_target_id_chunks')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_relations')), sa.UniqueConstraint('source_id', 'target_id', 'relation', name='uq_chunk_relations_source_id_target_id_relation'))
    op.create_index(op.f('ix_chunk_relations_source_id'), 'chunk_relations', ['source_id'], unique=False)
    op.create_table('chunk_source_elements', sa.Column('chunk_id', sa.Uuid(), nullable=False), sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('element_id', sa.Uuid(), nullable=False), sa.Column('position', sa.Integer(), nullable=False), sa.Column('start_offset', sa.Integer(), nullable=False), sa.Column('end_offset', sa.Integer(), nullable=False), sa.Column('role', sa.String(length=40), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint('start_offset >= 0 AND end_offset >= start_offset', name=op.f('ck_chunk_source_elements_source_offsets_valid')), sa.ForeignKeyConstraint(['chunk_id', 'chunk_run_id', 'parse_run_id'], ['chunks.id', 'chunks.chunk_run_id', 'chunks.parse_run_id'], name=op.f('fk_chunk_source_elements_chunk_id_chunks')), sa.ForeignKeyConstraint(['element_id', 'parse_run_id'], ['document_elements.id', 'document_elements.parse_run_id'], name=op.f('fk_chunk_source_elements_element_id_document_elements')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_source_elements')), sa.UniqueConstraint('chunk_id', 'position', name='uq_chunk_source_elements_chunk_id_position'))
    op.create_index(op.f('ix_chunk_source_elements_chunk_id'), 'chunk_source_elements', ['chunk_id'], unique=False)
    op.create_index(op.f('ix_chunk_source_elements_element_id'), 'chunk_source_elements', ['element_id'], unique=False)
    op.create_table('chunk_source_pages', sa.Column('chunk_id', sa.Uuid(), nullable=False), sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('parse_run_id', sa.Uuid(), nullable=False), sa.Column('page_id', sa.Uuid(), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.ForeignKeyConstraint(['chunk_id', 'chunk_run_id', 'parse_run_id'], ['chunks.id', 'chunks.chunk_run_id', 'chunks.parse_run_id'], name=op.f('fk_chunk_source_pages_chunk_id_chunks')), sa.ForeignKeyConstraint(['page_id', 'parse_run_id'], ['document_pages.id', 'document_pages.parse_run_id'], name=op.f('fk_chunk_source_pages_page_id_document_pages')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_source_pages')), sa.UniqueConstraint('chunk_id', 'page_id', name='uq_chunk_source_pages_chunk_id_page_id'))
    op.create_index(op.f('ix_chunk_source_pages_chunk_id'), 'chunk_source_pages', ['chunk_id'], unique=False)
    op.create_table('chunk_validation_findings', sa.Column('chunk_run_id', sa.Uuid(), nullable=False), sa.Column('chunk_id', sa.Uuid(), nullable=True), sa.Column('severity', sa.String(length=20), nullable=False), sa.Column('code', sa.String(length=80), nullable=False), sa.Column('message', sa.String(length=300), nullable=False), sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column('id', sa.Uuid(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.ForeignKeyConstraint(['chunk_id', 'chunk_run_id'], ['chunks.id', 'chunks.chunk_run_id'], name=op.f('fk_chunk_validation_findings_chunk_id_chunks')), sa.ForeignKeyConstraint(['chunk_run_id'], ['chunk_runs.id'], name=op.f('fk_chunk_validation_findings_chunk_run_id_chunk_runs')), sa.PrimaryKeyConstraint('id', name=op.f('pk_chunk_validation_findings')))
    op.create_index(op.f('ix_chunk_validation_findings_chunk_run_id'), 'chunk_validation_findings', ['chunk_run_id'], unique=False)
    op.create_index(op.f('ix_chunk_validation_findings_code'), 'chunk_validation_findings', ['code'], unique=False)
    op.add_column('outbox_messages', sa.Column('kind', sa.String(length=20), server_default='PARSING', nullable=False))
    op.add_column('outbox_messages', sa.Column('chunk_run_id', sa.Uuid(), nullable=True))
    op.drop_constraint(op.f('uq_outbox_messages_job_id'), 'outbox_messages', type_='unique')
    op.create_unique_constraint(op.f('uq_outbox_messages_job_id'), 'outbox_messages', ['job_id', 'generation', 'kind'])
    op.create_foreign_key(op.f('fk_outbox_messages_chunk_run_id_chunk_runs'), 'outbox_messages', 'chunk_runs', ['chunk_run_id'], ['id'])
    _install_guards()

def downgrade() -> None:
    _prepare_downgrade()
    op.drop_constraint(op.f('fk_outbox_messages_chunk_run_id_chunk_runs'), 'outbox_messages', type_='foreignkey')
    op.drop_constraint(op.f('uq_outbox_messages_job_id'), 'outbox_messages', type_='unique')
    op.create_unique_constraint(op.f('uq_outbox_messages_job_id'), 'outbox_messages', ['job_id', 'generation'], postgresql_nulls_not_distinct=False)
    op.drop_column('outbox_messages', 'chunk_run_id')
    op.drop_column('outbox_messages', 'kind')
    op.drop_index(op.f('ix_chunk_validation_findings_code'), table_name='chunk_validation_findings')
    op.drop_index(op.f('ix_chunk_validation_findings_chunk_run_id'), table_name='chunk_validation_findings')
    op.drop_table('chunk_validation_findings')
    op.drop_index(op.f('ix_chunk_source_pages_chunk_id'), table_name='chunk_source_pages')
    op.drop_table('chunk_source_pages')
    op.drop_index(op.f('ix_chunk_source_elements_element_id'), table_name='chunk_source_elements')
    op.drop_index(op.f('ix_chunk_source_elements_chunk_id'), table_name='chunk_source_elements')
    op.drop_table('chunk_source_elements')
    op.drop_index(op.f('ix_chunk_relations_source_id'), table_name='chunk_relations')
    op.drop_table('chunk_relations')
    op.drop_index(op.f('ix_chunk_artifact_relations_chunk_id'), table_name='chunk_artifact_relations')
    op.drop_table('chunk_artifact_relations')
    op.drop_index(op.f('ix_question_options_question_id'), table_name='question_options')
    op.drop_table('question_options')
    op.drop_index(op.f('ix_chunks_tenant_id'), table_name='chunks')
    op.drop_index(op.f('ix_chunks_chunk_type'), table_name='chunks')
    op.drop_index(op.f('ix_chunks_chunk_run_id'), table_name='chunks')
    op.drop_table('chunks')
    op.drop_index(op.f('ix_question_artifacts_chunk_run_id'), table_name='question_artifacts')
    op.drop_table('question_artifacts')
    op.drop_index('uq_chunk_run_pending_version', table_name='chunk_runs', postgresql_where=sa.text("status IN ('PENDING','RUNNING')"))
    op.drop_index('uq_chunk_run_active_version', table_name='chunk_runs', postgresql_where=sa.text('is_active'))
    op.drop_index(op.f('ix_chunk_runs_tenant_id'), table_name='chunk_runs')
    op.drop_index(op.f('ix_chunk_runs_status'), table_name='chunk_runs')
    op.drop_index(op.f('ix_chunk_runs_parse_run_id'), table_name='chunk_runs')
    op.drop_index(op.f('ix_chunk_runs_lease_expires_at'), table_name='chunk_runs')
    op.drop_index(op.f('ix_chunk_runs_ingestion_job_id'), table_name='chunk_runs')
    op.drop_index(op.f('ix_chunk_runs_document_version_id'), table_name='chunk_runs')
    op.drop_table('chunk_runs')
    op.drop_constraint(op.f('uq_table_artifacts_id'), 'table_artifacts', type_='unique')
    op.drop_constraint('uq_parse_runs_id_version_tenant', 'parse_runs', type_='unique')
    op.drop_constraint(op.f('uq_formula_artifacts_id'), 'formula_artifacts', type_='unique')
    op.drop_constraint(op.f('uq_figure_artifacts_id'), 'figure_artifacts', type_='unique')
    _restore_m2()

M3_JOB_GUARD = "\n    CREATE OR REPLACE FUNCTION m1_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$\n    BEGIN\n      IF TG_OP = 'INSERT' THEN\n        IF NEW.status <> 'UPLOADED' OR NEW.retry_count <> 0\n        THEN RAISE EXCEPTION 'Jobs must begin UPLOADED'; END IF;\n        RETURN NEW;\n      END IF;\n      IF ROW(NEW.document_version_id, NEW.tenant_id, NEW.requested_by_user_id,\n             NEW.configuration_version, NEW.config_snapshot, NEW.max_retries)\n         IS DISTINCT FROM ROW(OLD.document_version_id, OLD.tenant_id, OLD.requested_by_user_id,\n             OLD.configuration_version, OLD.config_snapshot, OLD.max_retries)\n      THEN RAISE EXCEPTION 'Job provenance and configuration are immutable'; END IF;\n      IF NEW.status IS DISTINCT FROM OLD.status THEN\n        IF NOT (\n          (OLD.status = 'UPLOADED' AND NEW.status IN ('VALIDATING', 'CANCELLED')) OR\n          (OLD.status = 'VALIDATING' AND NEW.status IN\n            ('QUEUED', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR\n          (OLD.status = 'QUEUED' AND NEW.status IN\n            ('PARSING', 'FAILED', 'QUARANTINED', 'CANCELLED')) OR\n          (OLD.status = 'PARSING' AND NEW.status IN\n            ('NORMALIZING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR\n          (OLD.status = 'NORMALIZING' AND NEW.status IN\n            ('ENRICHING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR\n          \n          (OLD.status = 'READY_FOR_CHUNKING' AND NEW.status IN ('CHUNKING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR\n          (OLD.status = 'CHUNKING' AND NEW.status IN ('VALIDATING_CHUNKS','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR\n          (OLD.status = 'VALIDATING_CHUNKS' AND NEW.status IN ('READY_FOR_EMBEDDING','FAILED','QUARANTINED','NEEDS_REVIEW','CANCELLED')) OR\n          (OLD.status IN ('READY_FOR_EMBEDDING','FAILED','NEEDS_REVIEW') AND NEW.status = 'READY_FOR_CHUNKING'\n            AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries) OR\n          (OLD.status = 'ENRICHING' AND NEW.status IN\n            ('READY_FOR_CHUNKING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED')) OR\n          (OLD.status IN ('FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING','READY_FOR_EMBEDDING')\n           AND NEW.status = 'CANCELLED') OR\n          -- Retry of a failure, and explicit operator-requested reparse of a completed or\n          -- flagged version. Both consume one unit of the same bounded retry budget.\n          (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING','READY_FOR_EMBEDDING')\n           AND NEW.status = 'VALIDATING'\n           AND NEW.retry_count = OLD.retry_count + 1 AND NEW.retry_count <= NEW.max_retries))\n        THEN RAISE EXCEPTION 'Illegal ingestion transition'; END IF;\n      END IF;\n      IF NEW.retry_count <> OLD.retry_count AND NOT\n        (OLD.status IN ('FAILED', 'NEEDS_REVIEW', 'READY_FOR_CHUNKING','READY_FOR_EMBEDDING')\n         AND NEW.status IN ('VALIDATING','READY_FOR_CHUNKING') AND NEW.retry_count = OLD.retry_count + 1)\n      THEN RAISE EXCEPTION 'Retry count can change only on a retry or reparse'; END IF;\n      RETURN NEW;\n    END $$;\n    "

def _m3_statuses() -> None:
    from migrations.versions.m2_document_parsing import M2_STATUSES, _replace_status_constraint
    _replace_status_constraint(M2_STATUSES + ("VALIDATING_CHUNKS","READY_FOR_EMBEDDING"), 19)

def _restore_m2() -> None:
    from migrations.versions.m2_document_parsing import M2_STATUSES, _replace_status_constraint, _install_m2_job_guard
    _replace_status_constraint(M2_STATUSES,18)
    _install_m2_job_guard()

def _prepare_downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM ingestion_jobs WHERE status IN ('CHUNKING','VALIDATING_CHUNKS','READY_FOR_EMBEDDING'))
      THEN RAISE EXCEPTION 'Cancel M3 jobs before downgrading; no states are silently rewritten'; END IF;
    END $$;
    """)
    op.execute("DROP TRIGGER m3_parse_superseded ON parse_runs")
    op.execute("DROP TRIGGER m3_job_cancelled ON ingestion_jobs")
    op.execute("DROP FUNCTION m3_invalidate_chunks()")
    for table in ("chunks","chunk_source_elements","chunk_source_pages","chunk_artifact_relations",
                  "chunk_relations","question_artifacts","question_options","chunk_validation_findings"):
        op.execute(f"DROP TRIGGER m3_dataset_guard ON {table}")
    op.execute("DROP TRIGGER m3_run_guard ON chunk_runs")
    op.execute("DROP FUNCTION m3_dataset_guard(), m3_run_guard()")
    # A stage-specific outbox cannot be represented by the old unique job/generation key.
    op.execute("DELETE FROM outbox_messages WHERE kind = 'CHUNKING'")

def _install_guards() -> None:
    op.execute(M3_JOB_GUARD)
    op.execute("""
    CREATE FUNCTION m3_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.is_active
        THEN RAISE EXCEPTION 'Chunk runs must begin pending and inactive'; END IF;
        IF NOT EXISTS (SELECT 1 FROM document_versions v JOIN ingestion_jobs j ON j.document_version_id=v.id
          JOIN parse_runs p ON p.document_version_id=v.id WHERE v.id=NEW.document_version_id
          AND v.document_id=NEW.document_id AND v.tenant_id=NEW.tenant_id
          AND p.id=NEW.parse_run_id AND p.tenant_id=NEW.tenant_id
          AND j.id=NEW.ingestion_job_id AND j.tenant_id=NEW.tenant_id
          AND p.is_active AND p.status='SUCCEEDED')
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
          AND p.validation_result IN ('PASS','PASS_WITH_WARNINGS'))
          OR NOT EXISTS (SELECT 1 FROM chunks c WHERE c.chunk_run_id=NEW.id)
          OR EXISTS (SELECT 1 FROM chunks c WHERE c.chunk_run_id=NEW.id AND
            NOT EXISTS (SELECT 1 FROM chunk_source_elements s WHERE s.chunk_id=c.id))
        THEN RAISE EXCEPTION 'An active dataset requires valid current parse and complete sources'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER m3_run_guard BEFORE INSERT OR UPDATE ON chunk_runs
      FOR EACH ROW EXECUTE FUNCTION m3_run_guard();

    CREATE FUNCTION m3_dataset_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
          AND e.parse_run_id=NEW.parse_run_id AND NEW.end_offset <= length(coalesce(e.normalized_text,'')))
        THEN RAISE EXCEPTION 'Source offsets do not resolve'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;

    CREATE FUNCTION m3_invalidate_chunks() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME='parse_runs' THEN
        IF OLD.is_active AND NOT NEW.is_active THEN
          UPDATE chunk_runs SET is_active=false WHERE parse_run_id=NEW.id AND is_active;
        END IF;
      ELSE
        IF NEW.status='CANCELLED' THEN
          UPDATE chunk_runs SET is_active=false WHERE document_version_id=NEW.document_version_id AND is_active;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER m3_parse_superseded AFTER UPDATE OF is_active ON parse_runs
      FOR EACH ROW EXECUTE FUNCTION m3_invalidate_chunks();
    CREATE TRIGGER m3_job_cancelled AFTER UPDATE OF status ON ingestion_jobs
      FOR EACH ROW EXECUTE FUNCTION m3_invalidate_chunks();
    """)
    for table in ("chunks","chunk_source_elements","chunk_source_pages","chunk_artifact_relations",
                  "chunk_relations","question_artifacts","question_options","chunk_validation_findings"):
        op.execute(f"CREATE TRIGGER m3_dataset_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION m3_dataset_guard()")
