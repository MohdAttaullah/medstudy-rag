export interface EmbeddingVersion {
  id: string; model_provider: string; model_id: string; model_revision: string;
  tokenizer_revision: string; model_checksum: string; embedding_dimension: number;
  pooling_strategy: string; normalization: string; distance_metric: string;
  max_input_tokens: number; dtype: string; input_builder_version: string;
  configuration_version: string; semantics_fingerprint: string;
  library_versions: Record<string, unknown>;
}
export interface EmbeddingRun {
  id: string; document_id: string; document_version_id: string; chunk_run_id: string;
  embedding_version_id: string; status: string; is_active: boolean;
  eligible_chunk_count: number; embedded_chunk_count: number; reused_chunk_count: number;
  failed_chunk_count: number; skipped_chunk_count: number;
  input_fingerprint: string | null; policy_fingerprint: string;
  metrics: Record<string, unknown>; error_code: string | null; error_message: string | null;
  duration_ms: number | null;
}
export interface IndexRun {
  id: string; document_version_id: string; embedding_run_id: string;
  embedding_version_id: string; chunk_run_id: string; attempt: number;
  physical_collection: string; alias: string; vector_name: string; schema_version: string;
  status: string; is_active: boolean; expected_point_count: number;
  indexed_point_count: number; verified_point_count: number; upsert_batches: number;
  metrics: Record<string, unknown>; error_code: string | null; error_message: string | null;
  activated_at: string | null; duration_ms: number | null;
}
/** Vector metadata only. A raw 768-number array is never sent to the browser. */
export interface ChunkEmbedding {
  id: string; embedding_run_id: string; chunk_id: string; point_id: string;
  vector_name: string; input_hash: string; vector_checksum: string; dimension: number;
  token_count: number; vector_norm: number; truncated: boolean; reused: boolean;
}
export interface IndexFinding {
  id: string; embedding_run_id: string | null; index_run_id: string | null;
  chunk_id: string | null; severity: string; code: string; message: string;
  details: Record<string, unknown>;
}
export interface IndexStatistics {
  index_run_id: string; collection: string; vector_name: string; dimension: number;
  distance_metric: string; expected_points: number; live_points: number;
  alias: string; alias_target: string | null; reachable: boolean;
}
export interface EmbeddingSummary {
  document_version_id: string; ingestion_status: string;
  embedding_run: EmbeddingRun | null; embedding_version: EmbeddingVersion | null;
  index_run: IndexRun | null; embedding_runs: number;
  finding_counts: Record<string, number>; chunk_types: Record<string, number>;
}
