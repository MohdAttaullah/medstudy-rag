export interface ChunkRun {
  id: string; document_id: string; document_version_id: string; parse_run_id: string;
  status: string; validation_result: string | null; is_active: boolean;
  chunker_name: string; chunker_version: string; configuration_version: string;
  policy_fingerprint: string; tokenizer_name: string; tokenizer_version: string;
  input_fingerprint: string | null; metrics: Record<string, unknown>;
  error_code: string | null; error_message: string | null;
}
export interface Chunk {
  id: string; chunk_run_id: string; parse_run_id: string; parent_chunk_id: string | null;
  question_id: string | null; chunk_type: string; sequence_number: number;
  normalized_text: string; retrieval_text: string; token_count: number; retrieval_token_count: number;
  page_start: number | null; page_end: number | null; chunk_hash: string;
  chunk_metadata: Record<string, unknown>;
  artifacts?: { table_id: string | null; figure_id: string | null; formula_id: string | null }[];
  next_sibling_ids?: string[];
}
export interface Question {
  id: string; question_number: string | null; question_text: string; question_type: string;
  explicit_answer: string | null; explanation: string | null; extraction_status: string;
  authority: Record<string, unknown>; answer_inferred: boolean;
  options: { ordinal: number; label: string; text: string }[];
}
export interface Finding { id: string; chunk_id: string | null; severity: string; code: string; message: string }
