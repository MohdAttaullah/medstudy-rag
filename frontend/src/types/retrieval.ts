/**
 * M5 wire types.
 *
 * There is no answer field, no confidence and no vector, and none may be added: this milestone
 * returns ranked evidence candidates with their provenance, and a client that could render an
 * "answer" would be rendering something the server never produced.
 */

export interface QueryEncoderVersion {
  id: string; model_id: string; model_revision: string; tokenizer_revision: string;
  model_checksum: string; tokenizer_checksum: string; embedding_dimension: number;
  pooling_strategy: string; normalization: string; distance_metric: string;
  max_query_tokens: number; dtype: string; normalization_version: string;
  configuration_version: string; semantics_fingerprint: string;
  library_versions: Record<string, unknown>;
}

export interface SparseAnalyzer {
  version: string; analyzer_name: string; analyzer_version: string;
  unicode_normalization: string; case_policy: string; compound_policy: string;
  stopwords: string[]; min_term_length: number; max_term_length: number; expansion: string;
}

export interface SparseIndexVersion {
  id: string; analyzer_name: string; analyzer_version: string; unicode_normalization: string;
  case_policy: string; compound_policy: string; stopword_policy: string; stopword_count: number;
  min_term_length: number; max_term_length: number; expansion: string;
  configuration_version: string; analyzer_fingerprint: string;
}

export interface SparseIndex {
  id: string; document_version_id: string; chunk_run_id: string; sparse_index_version_id: string;
  status: string; is_active: boolean; expected_chunk_count: number; indexed_chunk_count: number;
  verified_chunk_count: number; term_count: number; posting_count: number; total_length: number;
  corpus_fingerprint: string | null; metrics: Record<string, unknown>;
  activated_at: string | null; duration_ms: number | null;
  error_code: string | null; error_message: string | null;
}

export interface SparseFinding {
  id: string; sparse_index_id: string; chunk_id: string | null;
  severity: string; code: string; message: string; details: Record<string, unknown>;
}

export interface SparseIndexSummary {
  document_version_id: string; ingestion_status: string; sparse_indexes: number;
  sparse_index: SparseIndex | null; sparse_index_version: SparseIndexVersion | null;
  finding_counts: Record<string, number>; chunk_types: Record<string, number>;
  /** Searchable, never answerable. */
  retrieval_ready: boolean; lanes_aligned: boolean;
}

export interface RetrievalStatus {
  tenant_id: string; document_versions: number; chunk_runs: number;
  dense_index_runs: number; sparse_indexes: number;
  embedding_version_id: string | null; sparse_index_version_id: string | null;
  query_encoder: QueryEncoderVersion | null; analyzer: SparseAnalyzer | null;
  modes: string[]; default_mode: string; dense_top_k: number; sparse_top_k: number;
  final_top_k: number; rrf_k: number; bm25_k1: number; bm25_b: number;
  degradation_policy: string; corpus_error: string | null; answering_enabled: false;
}

export interface Provenance {
  document_id: string; document_version_id: string; chunk_run_id: string;
  document_title: string; source_type: string; authority_level: string;
  subject: string | null; specialty: string | null; chunk_type: string;
  page_start: number | null; page_end: number | null; sequence_number: number;
  parent_chunk_id: string | null; question_id: string | null; source_element_ids: string[];
}

/** Ranks and scores are retrieval diagnostics. They are not confidence and not correctness. */
export interface Candidate {
  chunk_id: string; fused_rank: number; fused_score: number;
  dense_rank: number | null; dense_score: number | null;
  sparse_rank: number | null; sparse_score: number | null;
  lanes: string[]; matched_terms: string[]; preview: string; provenance: Provenance;
}

export interface LaneHit {
  chunk_id: string; rank: number; score: number;
  chunk_type: string | null; source_type: string | null; authority_level: string | null;
  matched_terms: string[];
}

export interface RetrievalTrace {
  correlation_id: string; mode: string; retrieval_config_version: string;
  retrieval_config_fingerprint: string; query_encoder_version_id: string | null;
  query_encoder_fingerprint: string | null; query_hash: string;
  query_token_count: number | null; normalization_version: string;
  embedding_version_id: string | null; sparse_index_version_id: string | null;
  index_run_ids: string[]; sparse_index_ids: string[]; chunk_run_ids: string[];
  dense_top_k: number; sparse_top_k: number; final_top_k: number; rrf_k: number;
  dense_weight: number; sparse_weight: number; bm25_k1: number; bm25_b: number;
  dense_candidates: number; sparse_candidates: number; fused_candidates: number;
  durations_ms: Record<string, number>; query_vector_cached: boolean;
}

export interface SearchResponse {
  correlation_id: string; mode: string; candidates: Candidate[];
  dense: LaneHit[]; sparse: LaneHit[]; trace: RetrievalTrace;
  warnings: string[]; answering_enabled: false;
}


export interface EvidenceBlock {
  evidence_id: string; anchor_chunk_id: string; source_chunk_ids: string[];
  source_element_ids: string[]; document_id: string; document_version_id: string;
  chunk_run_id: string; parse_run_id: string; document_title: string;
  source_type: string; authority_level: string; chunk_type: string; pages: number[];
  hierarchy: {element_id: string; text: string}[]; text: string; token_count: number;
  expansion_reason: string; requires_visual_evidence: boolean;
  context_reasons?: string[];
  source_spans?: {element_id: string; start: number; end: number; page: number | null; bbox: (number | null)[]}[];
  artifacts: {artifact_id: string; kind: string; href: string; row_indexes: number[]; header_rows: number[]; image_available: boolean}[];
}
export interface EvidenceSet {
  evidence_blocks: EvidenceBlock[]; anchors: string[]; expansions: string[];
  total_tokens: number; requires_visual_evidence: boolean; warnings: string[];
  duplicates_removed: number; answering_enabled: false;
  reranking_trace: {durations_ms: Record<string, number>};
}
export interface RerankedResponse {
  first_stage: SearchResponse; answering_enabled: false;
  reranked: (Candidate & {reranked_rank: number; reranker_score: number; selected_anchor: boolean})[];
  evidence_set: EvidenceSet;
}

export interface EvaluatedSignal {
  name: string; value: unknown; required: unknown; satisfied: boolean;
}
export interface EvidenceConflict {
  kind: string; description: string; evidence_ids: string[]; document_version_ids: string[];
  detail: Record<string, unknown>;
}
/** No percentage, probability or score: M7 establishes no calibrated medical confidence. */
export interface SufficiencyDecision {
  status: 'SUFFICIENT' | 'INSUFFICIENT' | 'CONFLICTING';
  question_kind: string; reason_codes: string[]; evaluated_signals: EvaluatedSignal[];
  supporting_evidence_ids: string[]; conflicting_evidence_ids: string[];
  conflicts: EvidenceConflict[]; missing_requirements: string[];
  policy_version: string; policy_fingerprint: string;
}
export interface GroundedDraft {
  draft_id: string; status: 'GROUNDED_DRAFT';
  verification_status: 'UNVERIFIED_AWAITING_CLAIM_VERIFICATION';
  answer: string; claims: {text: string; evidence_ids: string[]}[];
  cited_evidence_ids: string[]; uncited_evidence_ids: string[];
  evidence_gap: string | null; query_hash: string;
  provider: {provider: string; model_id: string; temperature: number | null; prompt_version: string; schema_version: string};
  grounding_policy_version: string; grounding_policy_fingerprint: string;
  sufficiency_policy_fingerprint: string; durations_ms: Record<string, number>;
}
export interface Abstention {
  abstained: true; reason: string; reason_codes: string[]; message: string;
  conflicting_evidence_ids: string[];
}
export interface DraftResponse extends RerankedResponse {
  verified: false; sufficiency: SufficiencyDecision;
  draft: GroundedDraft | null; abstention: Abstention | null;
  durations_ms: Record<string, number>;
}

export interface ClaimVerification {
  claim_id: string; claim_text: string; claim_type: string; material: boolean;
  verdict: 'SUPPORTED' | 'UNSUPPORTED' | 'CONTRADICTED' | 'INSUFFICIENT_EVIDENCE' | 'UNVERIFIABLE';
  reason_codes: string[]; supporting_evidence_ids: string[]; contradicting_evidence_ids: string[];
  detail: Record<string, unknown>;
}
export interface ContradictionFinding {
  kind: string; description: string; claim_ids: string[]; evidence_ids: string[];
  document_version_ids: string[]; detail: Record<string, unknown>;
}
export interface VerifierSpec {
  verifier: string; provider: string; model_id: string; prompt_version: string;
  schema_version: string; independent_of_generator: boolean;
}
/** No score or percentage: M8 establishes no calibrated medical confidence. */
export interface VerificationReport {
  outcome: 'PASS' | 'REGENERATE_ONCE' | 'ABSTAIN';
  claims: {claim_id: string; text: string; claim_type: string; material: boolean; cited_evidence_ids: string[]}[];
  verifications: ClaimVerification[]; contradictions: ContradictionFinding[];
  failed_reason_codes: string[]; material_claims: number; supported_claims: number;
  repair_count: number; verifier: VerifierSpec | null;
  claim_extraction_fingerprint: string; final_policy_fingerprint: string;
  durations_ms: Record<string, number>;
}
export interface VerifiedAnswer {
  answer_id: string; verified: true; verification_status: 'VERIFIED'; answer: string;
  claims: ClaimVerification[]; cited_evidence_ids: string[]; query_hash: string;
  generator: {provider: string; model_id: string}; verifier: VerifierSpec; repair_count: number;
}
export interface VerificationAbstention {
  abstained: true; verified: false; reason: string; reason_codes: string[]; message: string;
  unsupported_claim_ids: string[]; contradicting_evidence_ids: string[];
}
export interface AnswerResponse extends Omit<DraftResponse, 'verified'> {
  verified: boolean;
  verification: VerificationReport | null;
  verified_answer: VerifiedAnswer | null;
  verification_abstention: VerificationAbstention | null;
}

export type AskOutcome = 'VERIFIED' | 'INSUFFICIENT_EVIDENCE' | 'CONFLICTING_EVIDENCE' | 'UNVERIFIED' | 'FAILED' | 'OUT_OF_SCOPE';
export interface CitationSpan {
  element_id: string; page: number | null; start: number; end: number; role: string;
  bbox: [number | null, number | null, number | null, number | null] | null;
}
export interface CitationArtifact {
  artifact_id: string; kind: string; row_indexes: number[]; header_rows: number[]; image_available: boolean;
}
export interface AskCitation {
  citation_id: string; ordinal: number; document_id: string; document_version_id: string;
  parse_run_id: string; chunk_run_id: string; document_title: string; source_type: string;
  authority_level: string; chunk_type: string; pages: number[];
  spans: CitationSpan[]; artifacts: CitationArtifact[]; cited_text: string;
}
export interface AskSource {
  document_id: string; document_version_id: string; parse_run_id: string; title: string;
  source_type: string; authority_level: string; pages: number[]; citation_ids: string[];
}
export interface AskClaim { text: string; citation_ids: string[] }
/** No confidence figure exists: an answer is verified or it is not shown. */
export interface AskResponse {
  correlation_id: string; conversation_id: string; turn_id: string; question: string;
  outcome: AskOutcome; verified: boolean; answering_enabled: true;
  answer: string | null; claims: AskClaim[]; citations: AskCitation[]; sources: AskSource[];
  figures: AskFigure[];
  message: string; reason_codes: string[];
  stages: {stage: string; duration_ms: number}[]; created_at: string;
}
export interface ConversationTurnView {
  turn_id: string; sequence_number: number; question: string; outcome: AskOutcome;
  verified: boolean; answer: string | null; message: string; reason_codes: string[];
  citations: AskCitation[]; sources: AskSource[]; figures: AskFigure[]; created_at: string;
}
/**
 * One state change of one Ask stage, exactly as the server sends it.
 *
 * There is no text field and no progress fraction, because the server has none to give: the channel
 * carries stage identity, state and timing, and nothing a reader is not allowed to see.
 */
export type StageCode =
  | 'PREPARING' | 'RETRIEVAL' | 'RERANK' | 'EVIDENCE' | 'GENERATION' | 'VERIFICATION' | 'FINALIZE';
export type StageState = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'SKIPPED' | 'FAILED';
export interface StageEvent {
  request_id: string; stage: StageCode; state: StageState; sequence: number;
  started_at: string | null; completed_at: string | null; elapsed_ms: number | null;
}
export interface AskFigure {
  figure_id: string; document_id: string; document_version_id: string; parse_run_id: string;
  document_title: string; page: number | null; caption: string | null; label: string | null;
  linked_by: 'CITED_EVIDENCE' | 'CITED_TEXT_REFERENCE'; citation_ids: string[];
}
export interface ConversationSummary {
  conversation_id: string; title: string; turn_count: number; verified_turns: number;
  created_at: string; updated_at: string;
}
export interface ConversationView {
  conversation_id: string; title: string; created_at: string; updated_at: string;
  turns: ConversationTurnView[];
}
