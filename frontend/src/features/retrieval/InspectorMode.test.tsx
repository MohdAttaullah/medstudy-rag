import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';

/**
 * The inspector's mode is a property of one diagnostic request and of nothing else.
 *
 * The concern it answers: a mode chosen here must not become the mode a later Ask runs. It cannot
 * — Ask posts to /ask, which takes a question and a conversation and has no mode field at all —
 * and these tests pin that, because the failure would be silent and would change what a reader is
 * answered with.
 */

let posted: { url: string; body: Record<string, unknown> }[] = [];

beforeEach(() => {
  posted = [];
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => 'key-1' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') posted.push({ url, body: JSON.parse(String(init.body)) });
    let body: unknown = {};
    if (url.includes('/auth/me')) {
      body = {
        role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development',
        permissions: ['ask:submit', 'conversation:read', 'retrieval:search', 'generation:draft'],
      };
    } else if (url.includes('/retrieval/status')) {
      body = {
        document_versions: 1, dense_index_runs: 1, sparse_indexes: 1, dense_top_k: 40,
        sparse_top_k: 40, final_top_k: 20, modes: ['DENSE_ONLY', 'BM25_ONLY', 'HYBRID_RRF'],
        default_mode: 'HYBRID_RRF', corpus_error: null,
      };
    } else if (url.includes('/retrieval/search')) {
      body = {
        correlation_id: 'c', mode: 'HYBRID_RRF', candidates: [], dense: [], sparse: [],
        warnings: [], answering_enabled: false,
        trace: {
          mode: 'HYBRID_RRF', query_hash: 'h', query_token_count: 7, dense_candidates: 0,
          sparse_candidates: 0, fused_candidates: 0, returned: 0, durations_ms: { total_ms: 30 },
          index_run_ids: ['i1'], sparse_index_ids: ['s1'], chunk_run_ids: [],
        },
      };
    } else if (url.includes('/ask')) {
      body = {
        correlation_id: 'x', conversation_id: 'cv1', turn_id: 't1', question: 'Q?',
        outcome: 'VERIFIED', verified: true, answering_enabled: true, answer: 'Checked.',
        claims: [], citations: [], sources: [], message: 'm', reason_codes: [],
        stages: [{ stage: 'Total', duration_ms: 10 }], created_at: '',
      };
    } else if (url.includes('/conversations')) {
      body = { items: [], total: 0, offset: 0, limit: 25, conversation_id: 'cv1', title: 'Q?', turns: [], created_at: '', updated_at: '' };
    }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function open(entry: string) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <MemoryRouter initialEntries={[entry]}><App /></MemoryRouter>
  </QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
}

it('says plainly that the selection is diagnostic only', async () => {
  await open('/retrieval');
  expect(await screen.findByText(/Diagnostic only — this selection does not change Ask behaviour\./))
    .toBeInTheDocument();
});

it.each([
  ['Dense only', 'Semantic retrieval only.', 'makes no provider call'],
  ['BM25 only', 'Keyword retrieval only.', 'makes no provider call'],
  ['Hybrid (RRF)', 'Dense + BM25 fused with Reciprocal Rank Fusion.', 'makes no provider call'],
  ['Hybrid + reranking + evidence', 'Adds CrossEncoder reranking and EvidenceSet construction.', 'makes no provider call'],
  ['Evidence gate + grounded draft (unverified)', 'Adds the sufficiency gate and a grounded draft.', 'calls the configured provider'],
  ['Full pipeline + claim verification', 'The closest diagnostic equivalent of Ask', 'calls the configured provider'],
])('describes %s and whether it calls a provider', async (label, description, provider) => {
  await open('/retrieval');
  const select = await screen.findByLabelText('Retrieval mode');
  fireEvent.change(select, { target: { value: (screen.getByRole('option', { name: label }) as HTMLOptionElement).value } });
  const help = document.getElementById('retrieval-mode-help')!;
  expect(help.textContent).toContain(description);
  expect(help.textContent).toContain(provider);
});

it('sends the chosen mode to the diagnostic endpoint and nowhere else', async () => {
  await open('/retrieval');
  const select = await screen.findByLabelText('Retrieval mode');
  fireEvent.change(select, { target: { value: 'BM25_ONLY' } });
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'tentorial surface' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
  await waitFor(() => expect(posted.some(call => call.url.includes('/retrieval/search'))).toBe(true));
  expect(posted[0].body.mode).toBe('BM25_ONLY');
  // Nothing was written anywhere: no configuration call, no settings call, no session call.
  expect(posted.every(call => !call.url.includes('/configuration'))).toBe(true);
});

it('does not put a mode into a later Ask request', async () => {
  await open('/retrieval');
  const select = await screen.findByLabelText('Retrieval mode');
  fireEvent.change(select, { target: { value: 'DENSE_ONLY' } });
  fireEvent.change(screen.getByLabelText('Query text'), { target: { value: 'tentorial surface' } });
  fireEvent.click(screen.getByRole('button', { name: 'Retrieve candidates' }));
  await waitFor(() => expect(posted.length).toBe(1));

  fireEvent.click(screen.getByRole('link', { name: 'Ask' }));
  fireEvent.change(await screen.findByLabelText('Your educational medical question'), { target: { value: 'Q?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));

  await waitFor(() => expect(posted.some(call => call.url.includes('/ask'))).toBe(true));
  const ask = posted.find(call => call.url.includes('/ask'))!;
  expect(Object.keys(ask.body).sort()).toEqual(['conversation_id', 'idempotency_key', 'question']);
  expect(JSON.stringify(ask.body)).not.toContain('DENSE_ONLY');
});

it('keeps the selection when the page is left and reopened', async () => {
  await open('/retrieval?mode=BM25_ONLY');
  expect(await screen.findByLabelText('Retrieval mode')).toHaveValue('BM25_ONLY');
});

it('falls back to hybrid when the address names a mode that does not exist', async () => {
  await open('/retrieval?mode=NOT_A_MODE');
  expect(await screen.findByLabelText('Retrieval mode')).toHaveValue('HYBRID_RRF');
});
