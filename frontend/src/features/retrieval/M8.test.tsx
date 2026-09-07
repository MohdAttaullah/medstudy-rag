import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';

const candidate = { chunk_id: 'c1', fused_rank: 1, fused_score: 0.03, dense_rank: 1, dense_score: 8,
  sparse_rank: 2, sparse_score: 2, lanes: ['DENSE', 'BM25'], matched_terms: [], preview: 'Synthetic passage.',
  provenance: { document_id: 'd1', document_version_id: 'v1', chunk_run_id: 'cr1', document_title: 'Synthetic reference',
    source_type: 'TEXTBOOK', authority_level: 'REFERENCE', chunk_type: 'TEXT_CHILD', page_start: 2, page_end: 2, source_element_ids: ['e1'] } };
const firstStage = { correlation_id: 'x', candidates: [candidate], dense: [], sparse: [], mode: 'HYBRID_RRF',
  warnings: [], answering_enabled: false, trace: { durations_ms: { total_ms: 30 }, index_run_ids: [], sparse_index_ids: [], chunk_run_ids: [] } };
const block = { evidence_id: 'ev1', anchor_chunk_id: 'c1', source_chunk_ids: ['c1'], source_element_ids: ['e1'],
  document_id: 'd1', document_version_id: 'v1', chunk_run_id: 'cr1', parse_run_id: 'pr1', document_title: 'Synthetic reference',
  source_type: 'TEXTBOOK', authority_level: 'REFERENCE', chunk_type: 'TEXT_CHILD', pages: [2], hierarchy: [],
  text: 'Synthetic source passage.', token_count: 8, expansion_reason: 'RERANKED_ANCHOR', requires_visual_evidence: false, artifacts: [] };
const evidenceSet = { evidence_blocks: [block], anchors: ['c1'], expansions: [], total_tokens: 8,
  requires_visual_evidence: false, warnings: [], duplicates_removed: 0, answering_enabled: false, reranking_trace: { durations_ms: {} } };
const sufficiency = { status: 'SUFFICIENT', question_kind: 'ORDINARY_FACTUAL',
  reason_codes: ['SUPPORTED_BY_SOURCE_EVIDENCE'], evaluated_signals: [], supporting_evidence_ids: ['ev1'],
  conflicting_evidence_ids: [], conflicts: [], missing_requirements: [],
  policy_version: 'sufficiency-m7-v1', policy_fingerprint: 'abcdef0123456789' };
const draft = { draft_id: 'dr1', status: 'GROUNDED_DRAFT', verification_status: 'UNVERIFIED_AWAITING_CLAIM_VERIFICATION',
  answer: 'The source states the association.', claims: [{ text: 'The source states the association.', evidence_ids: ['ev1'] }],
  cited_evidence_ids: ['ev1'], uncited_evidence_ids: [], evidence_gap: null, query_hash: '0',
  provider: { provider: 'openai', model_id: 'generator-model', temperature: null, prompt_version: 'grounded-draft-v1', schema_version: 'grounded-draft-schema-v1' },
  grounding_policy_version: 'grounding-m7-v1', grounding_policy_fingerprint: 'g1', sufficiency_policy_fingerprint: 'abcdef0123456789', durations_ms: {} };
const verifierSpec = { verifier: 'model', provider: 'openai', model_id: 'verifier-model',
  prompt_version: 'claim-verification-v1', schema_version: 'claim-verification-schema-v1', independent_of_generator: true };
const supportedClaim = { claim_id: 'cl1', claim_text: 'The source states the association.', claim_type: 'FACTUAL',
  material: true, verdict: 'SUPPORTED', reason_codes: ['SUPPORTED_BY_CITED_EVIDENCE'],
  supporting_evidence_ids: ['ev1'], contradicting_evidence_ids: [], detail: {} };
const failedClaim = { ...supportedClaim, claim_id: 'cl2', claim_text: 'The dose is 50 mg daily.',
  claim_type: 'NUMERIC', verdict: 'CONTRADICTED', reason_codes: ['NUMERIC_MISMATCH'],
  supporting_evidence_ids: [], contradicting_evidence_ids: ['ev1'] };
const passReport = { outcome: 'PASS', claims: [], verifications: [supportedClaim], contradictions: [],
  failed_reason_codes: [], material_claims: 1, supported_claims: 1, repair_count: 0, verifier: verifierSpec,
  claim_extraction_fingerprint: 'ce123456789012', final_policy_fingerprint: 'fp123456789012', durations_ms: { claim_verification_ms: 4 } };
const abstainReport = { ...passReport, outcome: 'ABSTAIN', verifications: [supportedClaim, failedClaim],
  failed_reason_codes: ['NUMERIC_MISMATCH'], material_claims: 2, supported_claims: 1,
  contradictions: [{ kind: 'AUTHORITATIVE_SOURCES_DISAGREE', description: 'Independent reference sources state different values in the same unit.', claim_ids: [], evidence_ids: ['ev1', 'ev2'], document_version_ids: ['v1', 'v2'], detail: {} }] };
const verifiedAnswer = { answer_id: 'a1', verified: true, verification_status: 'VERIFIED',
  answer: 'The source states the association.', claims: [supportedClaim], cited_evidence_ids: ['ev1'],
  query_hash: '0', generator: { provider: 'openai', model_id: 'generator-model' }, verifier: verifierSpec, repair_count: 0 };
const abstention = { abstained: true, verified: false, reason: 'CONTRADICTED_CLAIM',
  reason_codes: ['NUMERIC_MISMATCH'], message: 'A statement in the draft conflicted with the evidence it cited.',
  unsupported_claim_ids: ['cl2'], contradicting_evidence_ids: ['ev1'] };

let pass = true;
let captured: Record<string, unknown> = {};
beforeEach(() => {
  pass = true; captured = {};
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/retrieval/answer')) captured = JSON.parse(String(init?.body));
    const body = url.includes('/auth/me') ? { role: 'admin', display_name: 'Tester', permissions: ['retrieval:search', 'generation:draft', 'generation:verify', 'document:read'] } :
      url.includes('/retrieval/status') ? { document_versions: 1, dense_index_runs: 1, sparse_indexes: 1, corpus_error: null } :
      url.includes('/retrieval/answer') ? { correlation_id: 'x', mode: 'VERIFIED_ANSWER', answering_enabled: false,
        verified: pass, first_stage: firstStage, reranked: [{ ...candidate, reranked_rank: 1, reranker_score: 2.5, selected_anchor: true }],
        evidence_set: evidenceSet, sufficiency, draft, abstention: null,
        verification: pass ? passReport : abstainReport,
        verified_answer: pass ? verifiedAnswer : null,
        verification_abstention: pass ? null : abstention,
        durations_ms: { verification_total_ms: 120 } } : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function ask() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/retrieval']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Query text');
  fireEvent.change(screen.getByLabelText('Retrieval mode'), { target: { value: 'VERIFIED_ANSWER' } });
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'HLA-B27' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
}

it('shows every checked claim with its verdict and the evidence behind it', async () => {
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Claims' }));
  expect(screen.getByRole('heading', { name: 'Claim verification' })).toBeInTheDocument();
  expect(screen.getByText(/Claims are taken from the answer text itself/)).toBeInTheDocument();
  expect(screen.getByText('SUPPORTED_BY_CITED_EVIDENCE')).toBeInTheDocument();
  expect(screen.getByText(/independent of the generator/)).toBeInTheDocument();
});

it('distinguishes a verified answer from the unverified draft above it', async () => {
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Verified answer' }));
  expect(screen.getByRole('heading', { name: 'Verified answer' })).toBeInTheDocument();
  expect(screen.getByText(/not clinical validation, and not medical advice/)).toBeInTheDocument();
  expect(screen.getByText('verified')).toBeInTheDocument();
  expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
});

it('shows failed claims and preserved contradictions rather than hiding them', async () => {
  pass = false;
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Claims' }));
  // The failed claim is retained in the report so a refusal can be audited.
  expect(screen.getByText('The dose is 50 mg daily.')).toBeInTheDocument();
  expect(screen.getByText('NUMERIC_MISMATCH')).toBeInTheDocument();
  expect(screen.getByText(/No source was chosen over another, and rank did not break the tie./)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Verification abstention' }));
  expect(screen.getByRole('heading', { name: 'Abstained — no answer released' })).toBeInTheDocument();
  expect(screen.getByText(/Abstaining is an intended outcome/)).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Verified answer' })).not.toBeInTheDocument();
});

it('warns when the verifier is the same model as the generator', async () => {
  passReport.verifier = { ...verifierSpec, model_id: 'generator-model', independent_of_generator: false };
  try {
    await ask();
    fireEvent.click(await screen.findByRole('button', { name: 'Claims' }));
    expect(screen.getByText(/SAME MODEL AS THE GENERATOR/)).toBeInTheDocument();
  } finally {
    passReport.verifier = verifierSpec;
  }
});

it('never lets the client submit its own draft, evidence or verification result', async () => {
  await ask();
  await screen.findByRole('button', { name: 'Claims' });
  expect(Object.keys(captured).sort()).toEqual(['mode', 'query']);
});
