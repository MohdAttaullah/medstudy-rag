import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
let fail = false, empty = false;
beforeEach(() => {
  fail = false; empty = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/retrieval/search') && fail) return new Response(JSON.stringify({ error: { code: 'SPARSE_INDEX_NOT_READY', message: 'Lexical lane unavailable.' } }), { status: 409 });
    const body = url.includes('/auth/me') ? { role: 'admin', display_name: 'Tester', permissions: ['retrieval:search', 'document:read'] } :
      url.includes('/retrieval/status') ? { document_versions: 1, dense_index_runs: 1, sparse_indexes: 1, corpus_error: null } :
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
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'HLA-B27' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
}
it('shows candidates, lane diagnostics and source links without answering', async () => {
  await open(); await query();
  await screen.findByText('Synthetic HLA-B27 passage.');
  fireEvent.click(screen.getByRole('button', { name: 'Dense (1)' }));
  expect(screen.getByRole('heading', { name: 'Dense lane' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Hybrid (1)' }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect provenance' }));
  expect(screen.getByRole('link', { name: 'Open chunk inspector' })).toHaveAttribute('href', '/chunk-runs/cr1?chunk=c1');
  expect(screen.queryByRole('heading', { name: 'Answer' })).not.toBeInTheDocument();
});
it('reports required lane failure explicitly', async () => {
  fail = true; await open(); await query();
  expect(await screen.findByRole('alert')).toHaveTextContent('SPARSE_INDEX_NOT_READY');
});
it('reports an empty candidate set', async () => {
  empty = true; await open(); await query();
  expect(await screen.findByText('No candidate passage was retrieved for this query.')).toBeInTheDocument();
});
it('sends the selected mode without client tenant scope', async () => {
  await open(); fireEvent.change(screen.getByLabelText('Retrieval mode'), { target: { value: 'BM25_ONLY' } }); await query();
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/retrieval/search', expect.objectContaining({ body: JSON.stringify({ query: 'HLA-B27', mode: 'BM25_ONLY' }) })));
});
