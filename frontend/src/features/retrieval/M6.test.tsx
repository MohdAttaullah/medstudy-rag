import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';

const candidate = { chunk_id: 'c1', fused_rank: 1, fused_score: 0.03, dense_rank: 1, dense_score: 8,
  sparse_rank: 2, sparse_score: 2, lanes: ['DENSE', 'BM25'], matched_terms: ['hla-b27'], preview: 'Synthetic HLA-B27 passage.',
  provenance: { document_id: 'd1', document_version_id: 'v1', chunk_run_id: 'cr1', document_title: 'Synthetic reference',
    source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED', chunk_type: 'TEXT_CHILD', page_start: 2, page_end: 2, source_element_ids: ['e1'] } };
const response = { candidates: [candidate], dense: [{ chunk_id: 'c1', rank: 1, score: 8, matched_terms: [] }],
  sparse: [{ chunk_id: 'c1', rank: 2, score: 2, matched_terms: ['hla-b27'] }], mode: 'HYBRID_RRF', warnings: [], answering_enabled: false,
  trace: { durations_ms: { total_ms: 30 }, index_run_ids: ['i1'], sparse_index_ids: ['s1'], chunk_run_ids: ['cr1'] } };
const block = {evidence_id: 'ev1', anchor_chunk_id:'c1', source_chunk_ids:['c1'], source_element_ids:['e1'], document_id:'d1', document_version_id:'v1', chunk_run_id:'cr1', parse_run_id:'pr1', document_title:'Synthetic reference', source_type:'TEXTBOOK', authority_level:'UNREVIEWED', chunk_type:'TABLE_PART', pages:[2], hierarchy:[{element_id:'h1',text:'Source heading'}], text:'Synthetic table header and row', token_count:12, expansion_reason:'RERANKED_ANCHOR', requires_visual_evidence:true, artifacts:[{artifact_id:'t1',kind:'TABLE',row_indexes:[1],header_rows:[0],image_available:false}]};
const m6 = {first_stage:response, answering_enabled:false, reranked:[{...candidate,fused_rank:8,reranked_rank:1,reranker_score:2.5,selected_anchor:true}], evidence_set:{evidence_blocks:[block],anchors:['c1'],expansions:[],total_tokens:12,requires_visual_evidence:true,warnings:[],duplicates_removed:0,answering_enabled:false,reranking_trace:{durations_ms:{reranking_ms:50}}}};
let fail = false, empty = false;
beforeEach(() => {
  fail = false; empty = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/retrieval/rerank') && fail) return new Response(JSON.stringify({ error: { code: 'RERANKER_UNAVAILABLE', message: 'Reranker unavailable.' } }), { status: 409 });
    const body = url.includes('/auth/me') ? { role: 'admin', display_name: 'Tester', permissions: ['retrieval:search', 'document:read'] } :
      url.includes('/retrieval/status') ? { document_versions: 1, dense_index_runs: 1, sparse_indexes: 1, corpus_error: null } :
      url.includes('/retrieval/rerank') ? {...m6,evidence_set:{...m6.evidence_set,evidence_blocks:empty ? [] : [block]}} :
      url.includes('/retrieval/search') ? { ...response, candidates: empty ? [] : response.candidates } : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());
async function open() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/retrieval']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Query text');
}
async function query() {
  fireEvent.change(screen.getByLabelText('Retrieval mode'), {target:{value:'RERANKED'}});
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'HLA-B27' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
}

it('shows rank changes, bounded evidence, source links and visual source badge', async () => {
  await open(); await query();
  fireEvent.click(await screen.findByRole('button',{name:'Reranked'}));
  expect(screen.getByText('Fused rank 8 → reranked rank 1')).toBeInTheDocument();
  expect(screen.getByText('Reranker score — ranking diagnostic, not medical confidence.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Evidence Set'}));
  expect(screen.getByText('Synthetic table header and row')).toBeInTheDocument();
  expect(screen.getByText(/Visual source required/)).toBeInTheDocument();
  expect(screen.getByRole('link',{name:'Inspect original page'})).toHaveAttribute('href','/documents/d1/versions/v1/parse/pr1?page=2');
  expect(screen.getByText(/headers 0/)).toBeInTheDocument();
  expect(screen.getByRole('link',{name:'Inspect table source'})).toHaveAttribute('href','/documents/d1/versions/v1/parse/pr1?page=2#artifact-t1');
  expect(screen.queryByRole('heading',{name:'Answer'})).not.toBeInTheDocument();
});
it('reports unavailable reranker without M5 fallback', async () => {
  fail=true;await open();await query();
  expect(await screen.findByRole('alert')).toHaveTextContent('RERANKER_UNAVAILABLE');
  expect(screen.queryByRole('button',{name:'Evidence Set'})).not.toBeInTheDocument();
});
it('reports an empty budget result', async () => {
  empty=true;await open();await query();
  fireEvent.click(await screen.findByRole('button',{name:'Evidence Set'}));
  expect(screen.getByText('No source block fits the configured evidence budget.')).toBeInTheDocument();
});
it('requires access before retrieval', async () => {
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/retrieval']}><App /></MemoryRouter></QueryClientProvider>);
  expect(screen.queryByLabelText('Query text')).not.toBeInTheDocument();
});

it('links every contributing chunk of a multi-chunk source record', async () => {
  // The repository keeps every chunk an atomic question was assembled from; the inspector must not
  // collapse that back to the anchor, or the extra provenance is unreachable to a reviewer.
  block.source_chunk_ids = ['c1', 'c2'];
  try {
    await open(); await query();
    fireEvent.click(await screen.findByRole('button',{name:'Evidence Set'}));
    expect(screen.getByRole('link',{name:'Inspect contributing chunk 1 of 2'})).toHaveAttribute('href','/chunk-runs/cr1?chunk=c1');
    expect(screen.getByRole('link',{name:'Inspect contributing chunk 2 of 2'})).toHaveAttribute('href','/chunk-runs/cr1?chunk=c2');
    expect(screen.queryByRole('link',{name:'Inspect chunk provenance'})).not.toBeInTheDocument();
  } finally {
    block.source_chunk_ids = ['c1'];
  }
});
