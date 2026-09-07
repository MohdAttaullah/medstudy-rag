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
const sufficient = {
  status: 'SUFFICIENT', question_kind: 'ORDINARY_FACTUAL',
  reason_codes: ['SUPPORTED_BY_NON_ASSESSMENT_SOURCE', 'SUPPORTED_BY_SOURCE_EVIDENCE'],
  evaluated_signals: [{ name: 'supporting_blocks', value: 1, required: 1, satisfied: true }],
  supporting_evidence_ids: ['ev1'], conflicting_evidence_ids: [], conflicts: [], missing_requirements: [],
  policy_version: 'sufficiency-m7-v1', policy_fingerprint: 'abcdef0123456789' };
const draft = { draft_id: 'dr1', status: 'GROUNDED_DRAFT', verification_status: 'UNVERIFIED_AWAITING_CLAIM_VERIFICATION',
  answer: 'The source states the association.', claims: [{ text: 'The source states the association.', evidence_ids: ['ev1'] }],
  cited_evidence_ids: ['ev1'], uncited_evidence_ids: [], evidence_gap: null, query_hash: '0',
  provider: { provider: 'openai', model_id: 'configured-model', temperature: 0, prompt_version: 'grounded-draft-v1', schema_version: 'grounded-draft-schema-v1' },
  grounding_policy_version: 'grounding-m7-v1', grounding_policy_fingerprint: 'g1', sufficiency_policy_fingerprint: 'abcdef0123456789', durations_ms: {} };
const conflicting = { ...sufficient, status: 'CONFLICTING',
  reason_codes: ['INDEPENDENT_SOURCE_VALUE_CONFLICT'], conflicting_evidence_ids: ['ev1', 'ev2'],
  conflicts: [{ kind: 'INDEPENDENT_SOURCE_VALUE_CONFLICT', description: 'Independent sources state different values for the same labelled quantity.', evidence_ids: ['ev1', 'ev2'], document_version_ids: ['v1', 'v2'], detail: {} }] };
const abstention = { abstained: true, reason: 'CONFLICTING_EVIDENCE', reason_codes: ['INDEPENDENT_SOURCE_VALUE_CONFLICT'],
  message: 'The retrieved sources disagree and this conflict has not been resolved.', conflicting_evidence_ids: ['ev1', 'ev2'] };
const evidenceSet = { evidence_blocks: [block], anchors: ['c1'], expansions: [], total_tokens: 8,
  requires_visual_evidence: false, warnings: [], duplicates_removed: 0, answering_enabled: false, reranking_trace: { durations_ms: {} } };

let abstain = false;
let captured: Record<string, unknown> = {};
beforeEach(() => {
  abstain = false; captured = {};
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/retrieval/draft')) captured = JSON.parse(String(init?.body));
    const body = url.includes('/auth/me') ? { role: 'admin', display_name: 'Tester', permissions: ['retrieval:search', 'generation:draft', 'document:read'] } :
      url.includes('/retrieval/status') ? { document_versions: 1, dense_index_runs: 1, sparse_indexes: 1, corpus_error: null } :
      url.includes('/retrieval/draft') ? { correlation_id: 'x', mode: 'GROUNDED_DRAFT', answering_enabled: false, verified: false,
        first_stage: firstStage, reranked: [{ ...candidate, reranked_rank: 1, reranker_score: 2.5, selected_anchor: true }], evidence_set: evidenceSet,
        sufficiency: abstain ? conflicting : sufficient, draft: abstain ? null : draft, abstention: abstain ? abstention : null,
        durations_ms: { sufficiency_ms: 1, pipeline_total_ms: 900 } } : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function ask() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/retrieval']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Query text');
  fireEvent.change(screen.getByLabelText('Retrieval mode'), { target: { value: 'GROUNDED_DRAFT' } });
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'HLA-B27' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
}

it('shows the sufficiency decision, its signals and the reason codes behind it', async () => {
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Sufficiency' }));
  expect(screen.getByRole('heading', { name: 'Sufficiency decision' })).toBeInTheDocument();
  expect(screen.getByText(/No retrieval, fusion or reranker score takes part/)).toBeInTheDocument();
  expect(screen.getByText('SUPPORTED_BY_SOURCE_EVIDENCE')).toBeInTheDocument();
  expect(screen.getByText('supporting blocks')).toBeInTheDocument();
});

it('labels the draft as unverified and never as an answer', async () => {
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Grounded draft' }));
  expect(screen.getByRole('heading', { name: 'Grounded draft — awaiting verification' })).toBeInTheDocument();
  expect(screen.getByText(/unverified model draft, not an answer/)).toBeInTheDocument();
  expect(screen.getByText('Cites: ev1')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Answer' })).not.toBeInTheDocument();
  expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
});

it('presents a conflict as preserved disagreement, not a chosen source', async () => {
  abstain = true;
  await ask();
  fireEvent.click(await screen.findByRole('button', { name: 'Abstention' }));
  expect(screen.getByRole('heading', { name: 'Abstained' })).toBeInTheDocument();
  expect(screen.getByText(/disagree and this conflict has not been resolved/)).toBeInTheDocument();
  expect(screen.getByText(/Abstaining is an intended outcome/)).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Grounded draft — awaiting verification' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Sufficiency' }));
  expect(screen.getByText(/Competing evidence is preserved. No source was chosen over another./)).toBeInTheDocument();
});

it('never lets the client choose the evidence, provider, model or prompt', async () => {
  await ask();
  await screen.findByRole('button', { name: 'Sufficiency' });
  expect(Object.keys(captured).sort()).toEqual(['mode', 'query']);
});
