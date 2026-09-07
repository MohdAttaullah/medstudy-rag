import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';

const citation = {
  citation_id: 'ct1', ordinal: 1, document_id: 'd1', document_version_id: 'v1',
  parse_run_id: 'pr1', chunk_run_id: 'cr1', document_title: 'Synthetic Reference Volume',
  source_type: 'REFERENCE_BOOK', authority_level: 'REFERENCE', chunk_type: 'TEXT_CHILD',
  pages: [3],
  spans: [{ element_id: 'e1', page: 3, start: 0, end: 24, role: 'PRIMARY', bbox: [10, 20, 30, 40] }],
  artifacts: [], cited_text: 'A synthetic source passage.',
};
const tableCitation = {
  ...citation, citation_id: 'ct2', ordinal: 2, chunk_type: 'TABLE_PART', pages: [4],
  spans: [{ element_id: 'e2', page: 4, start: 0, end: 10, role: 'PRIMARY', bbox: null }],
  artifacts: [{ artifact_id: 'a1', kind: 'TABLE', row_indexes: [2], header_rows: [0], image_available: false }],
  cited_text: 'Compound A | 12 h.',
};
const keyCitation = {
  ...citation, citation_id: 'ct3', ordinal: 3, source_type: 'ANSWER_KEY',
  authority_level: 'ASSESSMENT', document_title: 'Synthetic Answer Key', pages: [7],
  spans: [{ element_id: 'e3', page: 7, start: 0, end: 8, role: 'PRIMARY', bbox: [5, 6, 7, 8] }],
};

let outcome = 'VERIFIED';
let captured: Record<string, unknown> = {};

function payload() {
  const verified = outcome === 'VERIFIED';
  return {
    correlation_id: 'x', conversation_id: 'cv1', turn_id: 'tn1', question: 'HLA-B27?',
    outcome, verified, answering_enabled: true,
    answer: verified ? 'The source states the association.' : null,
    claims: verified ? [{ text: 'The source states the association.', citation_ids: ['ct1'] }] : [],
    citations: verified ? [citation, tableCitation, keyCitation] : [],
    sources: verified ? [{ document_id: 'd1', document_version_id: 'v1', parse_run_id: 'pr1', title: 'Synthetic Reference Volume', source_type: 'REFERENCE_BOOK', authority_level: 'REFERENCE', pages: [3, 4], citation_ids: ['ct1', 'ct2'] }] : [],
    message: MESSAGES[outcome],
    reason_codes: verified ? [] : ['SOME_CODE'],
    stages: [{ stage: 'Verifying claims', duration_ms: 2244 }],
    created_at: '2026-09-07T00:00:00+00:00',
  };
}
const MESSAGES: Record<string, string> = {
  VERIFIED: 'Every statement below was checked against the sources cited with it.',
  INSUFFICIENT_EVIDENCE: 'The indexed sources do not support an answer to this question.',
  CONFLICTING_EVIDENCE: 'The indexed sources disagree with each other about this question.',
  UNVERIFIED: 'Evidence was found, but a supported answer could not be verified against it.',
  FAILED: 'The answering service could not complete this request.',
};

beforeEach(() => {
  outcome = 'VERIFIED'; captured = {};
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => 'key-1' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/ask')) captured = JSON.parse(String(init?.body));
    const body = url.includes('/auth/me')
      ? { role: 'reader', display_name: 'Tester', permissions: ['ask:submit', 'conversation:read', 'document:read'] }
      : url.includes('/conversations/') ? { conversation_id: 'cv1', title: 'HLA-B27?', created_at: '', updated_at: '', turns: [payload()] }
      : url.includes('/ask') ? payload() : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function ask() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/ask']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Your educational medical question');
  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'HLA-B27?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
}

it('renders a verified answer with its citations and sources', async () => {
  await ask();
  expect(await screen.findByRole('heading', { name: 'Answer' })).toBeInTheDocument();
  expect(screen.getByText('The source states the association.')).toBeInTheDocument();
  expect(screen.getByText('VERIFIED AGAINST RETRIEVED SOURCES')).toBeInTheDocument();
  expect(screen.getByText('[1] Synthetic Reference Volume')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Sources' })).toBeInTheDocument();
});

it('shows no confidence figure anywhere', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/\d+% (accurate|confident)/i)).not.toBeInTheDocument();
});

it('links each citation to its authoritative source page', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  const links = screen.getAllByRole('link', { name: 'Open source page' });
  expect(links[0]).toHaveAttribute('href', '/documents/d1/versions/v1/parse/pr1?page=3#element-e1');
  // A table citation deep-links to the artifact so the canonical table can be inspected.
  expect(links[1]).toHaveAttribute('href', '/documents/d1/versions/v1/parse/pr1?page=4#artifact-a1');
});

it('reports a real region when one exists and says so plainly when none does', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  expect(screen.getByText(/Region on page 3: 10.0, 20.0, 30.0, 40.0/)).toBeInTheDocument();
  expect(screen.getByText(/Region on page 7: 5.0, 6.0, 7.0, 8.0/)).toBeInTheDocument();
  // No geometry is invented for a source that never recorded one.
  expect(screen.getByText(/No region recorded for this source/)).toBeInTheDocument();
});

it('keeps assessment material visibly distinct from a reference source', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  expect(screen.getByText(/A recorded examiner answer is not, by itself, a medical reference./)).toBeInTheDocument();
});

it('describes table and formula citations by their canonical artifact', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  expect(screen.getByText(/Table source · rows 2 · headers 0/)).toBeInTheDocument();
});

it.each([
  ['INSUFFICIENT_EVIDENCE', 'Insufficient evidence'],
  ['CONFLICTING_EVIDENCE', 'Sources disagree'],
  ['UNVERIFIED', 'Could not verify an answer'],
  ['FAILED', 'The answering service failed'],
])('renders %s as its own distinct state with no answer text', async (state, heading) => {
  outcome = state;
  await ask();
  expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Answer' })).not.toBeInTheDocument();
  expect(screen.queryByText('The source states the association.')).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Sources' })).not.toBeInTheDocument();
});

it('distinguishes a technical failure from missing evidence', async () => {
  outcome = 'FAILED';
  await ask();
  await screen.findByRole('heading', { name: 'The answering service failed' });
  expect(screen.getByText('SERVICE PROBLEM')).toBeInTheDocument();
  expect(screen.queryByText(/do not support an answer/)).not.toBeInTheDocument();
});

it('tells the user nothing was filled in from model knowledge', async () => {
  outcome = 'INSUFFICIENT_EVIDENCE';
  await ask();
  await screen.findByRole('heading', { name: 'Insufficient evidence' });
  expect(screen.getByText(/Nothing was filled in from the model's own knowledge/)).toBeInTheDocument();
});

it('shows bounded progress stages and promises no answer before verification', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/ask']}><App /></MemoryRouter></QueryClientProvider>);
  expect(screen.queryByText(/Searching sources/)).not.toBeInTheDocument();
});

it('sends only the question, the conversation and an idempotency key', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  expect(Object.keys(captured).sort()).toEqual(['conversation_id', 'idempotency_key', 'question']);
  expect(captured.question).toBe('HLA-B27?');
});

it('continues the same conversation on a second question', async () => {
  await ask();
  await screen.findByRole('heading', { name: 'Answer' });
  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'Another?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
  await waitFor(() => expect(captured.conversation_id).toBe('cv1'));
});
