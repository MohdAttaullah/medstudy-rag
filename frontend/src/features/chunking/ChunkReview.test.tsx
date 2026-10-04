import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { lifecycleView, stages } from '../../test-lifecycle';
import { chunkState, measurement, retrievalBudget } from './review';
import { covered } from './ChunkInspector';
import type { Lifecycle } from '../../types/lifecycle';

/**
 * The Chunk Inspector when a passage set stopped for review: a reviewer must see where to start,
 * which passage blocks and why, what each fix does — and must never be offered a fix that cannot
 * work or that they are not allowed to run.
 */

const RUN = 'run-blocked';
const run = {
  id: RUN, document_id: 'doc-1', document_version_id: 'version-1', parse_run_id: 'parse-1',
  ingestion_job_id: 'job-1', generation: 1, status: 'NEEDS_REVIEW', validation_result: 'NEEDS_REVIEW',
  is_active: false, chunker_name: 'medical-structure', chunker_version: '1.0.0',
  configuration_version: 'chunking-m3-v1', policy_fingerprint: 'abc', tokenizer_name: 'ncbi/MedCPT-Article-Encoder',
  tokenizer_version: 'rev', input_fingerprint: 'in', metrics: {}, error_code: null, error_message: null,
  config_snapshot: { table_max_tokens: 384, child_target_tokens: 384, explanation_max_tokens: 384 },
};
const base = {
  chunk_run_id: RUN, parse_run_id: 'parse-1', parent_chunk_id: null, question_id: null,
  raw_text: '', chunk_hash: 'h', chunk_metadata: {}, next_sibling_ids: [], artifacts: [],
};
const blocking = {
  ...base, id: 'chunk-blk', chunk_type: 'TABLE_PART', sequence_number: 21,
  normalized_text: 'Intrinsic muscles | Origin', retrieval_text: 'Intrinsic muscles | Origin',
  token_count: 396, retrieval_token_count: 396, page_start: 10, page_end: 10,
  // Recorded before measurements were stored: the inspector must still say 396 / 384.
  findings: [{ severity: 'ERROR', code: 'CHUNK_OVERSIZED', message: 'A retrieval unit is too large to be embedded without truncation.', details: {} }],
};
const warning = {
  ...base, id: 'chunk-warn', chunk_type: 'FIGURE_CONTEXT', sequence_number: 3,
  normalized_text: '', retrieval_text: 'Source figure; no source caption or explanatory text.',
  token_count: 0, retrieval_token_count: 9, page_start: 2, page_end: 2,
  findings: [{ severity: 'WARNING', code: 'CHUNK_FIGURE_NO_TEXT', message: 'The figure has no source text.', details: {} }],
};
const clean = {
  ...base, id: 'chunk-ok', chunk_type: 'TEXT_CHILD', sequence_number: 1,
  normalized_text: 'Muscles of the larynx.', retrieval_text: 'Muscles of the larynx.',
  token_count: 5, retrieval_token_count: 5, page_start: 1, page_end: 1, findings: [],
};
const summary = {
  chunks: 34, questions: 0, findings: 14, blocking_findings: 1, warning_findings: 13, info_findings: 0,
  blocking_chunks: 1, warning_chunks: 13, clean_chunks: 20, dataset_findings: 0,
};
const findings = [
  { id: 'f1', chunk_id: 'chunk-blk', severity: 'ERROR', code: 'CHUNK_OVERSIZED', message: blocking.findings[0].message, details: {} },
  ...Array.from({ length: 13 }, (_, i) => ({
    id: `w${i}`, chunk_id: i === 0 ? 'chunk-warn' : `fig-${i}`, severity: 'WARNING', code: 'CHUNK_FIGURE_NO_TEXT',
    message: 'The figure has no source text.', details: {},
  })),
];

function review(overrides: Partial<Lifecycle> = {}): Lifecycle {
  return lifecycleView({
    state: 'REVIEW_REQUIRED', status: 'NEEDS_REVIEW', current_stage: 'PASSAGES', terminal: true,
    stages: stages({ RECEIVED: 'COMPLETED', READING: 'COMPLETED', PASSAGES: 'BLOCKED' }),
    review: {
      stage: 'CHUNK', run_id: RUN, blocking_count: 1, warning_count: 13, info_count: 0,
      identical_earlier_attempts: 1, retries_left: 2, max_retries: 3,
      reviewed_chunker_version: '1.0.0', current_chunker_version: '1.1.0',
      groups: [{ code: 'CHUNK_OVERSIZED', severity: 'ERROR', blocking: true, count: 1,
        message: blocking.findings[0].message, samples: [{ page: 10, chunk_id: 'chunk-blk' }] }],
    },
    actions: [
      { action: 'retry', available: false, reason: 'NOT_APPLICABLE' },
      { action: 'reparse', available: true, reason: null },
      { action: 'rechunk', available: true, reason: null },
      { action: 'reembed', available: false, reason: 'NOT_APPLICABLE' },
      { action: 'reindex-sparse', available: false, reason: 'NOT_APPLICABLE' },
    ],
    ...overrides,
  });
}

let lifecycle = review();
let calls: { url: string; method: string; body: string | null }[] = [];
const ADMIN = ['document:read', 'document:manage', 'ingestion:read', 'ingestion:rechunk', 'ingestion:reparse'];
let permissions = ADMIN;

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }
beforeEach(() => {
  lifecycle = review(); calls = []; permissions = ADMIN;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null });
    const status = new URL(url, 'http://x').searchParams.get('status');
    const body =
      url.includes('/auth/me') ? { user_id: 'u', display_name: 'Tester', role: 'admin', permissions } :
      url.includes('/ingestion/jobs/job-1/') ? { id: 'job-1', status: 'CHUNKING' } :
      url.includes('/review-summary') ? summary :
      url.includes(`/chunk-runs/${RUN}/chunks`) ? page(
        status === 'blocking' ? [blocking] : status === 'warning' ? [warning]
          : status === 'clean' ? [clean] : [clean, warning, blocking]) :
      url.includes(`/chunk-runs/${RUN}/validation-findings`) ? page(findings) :
      url.includes(`/chunk-runs/${RUN}/questions`) ? page([]) :
      url.includes(`/chunk-runs/${RUN}`) ? run :
      url.includes('/chunks/chunk-blk/sources') ? page([]) :
      url.includes('/chunks/chunk-blk') ? blocking :
      url.includes('/lifecycle') ? lifecycle :
      page([]);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function show(path: string) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'k' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByRole('button', { name: 'Account and workspace' });
}

describe('where to start', () => {
  it('opens a stopped passage set on its findings, with the blocker counted apart from warnings', async () => {
    await show(`/chunk-runs/${RUN}`);
    const findingsTab = await screen.findByRole('tab', { name: /Findings/ });
    await waitFor(() => expect(findingsTab).toHaveAttribute('aria-selected', 'true'));
    expect(findingsTab).toHaveTextContent('14');
    expect(findingsTab).toHaveTextContent('1 blocking');
    expect(findingsTab).toHaveTextContent('13 warnings');
    expect(screen.getByRole('tab', { name: /Chunks/ })).toHaveTextContent('34');
    expect(screen.getByRole('tab', { name: /Questions/ })).toHaveTextContent('0');
    expect(screen.getByRole('heading', { name: 'Stopped for review' })).toBeVisible();
  });

  it('flags the findings view as the place to start when another view is open', async () => {
    await show(`/chunk-runs/${RUN}?tab=chunks`);
    const findingsTab = await screen.findByRole('tab', { name: /Findings/ });
    await waitFor(() => expect(findingsTab).toHaveTextContent('1 blocking · start here'));
    expect(findingsTab.className).toContain('tab-attention');
  });

  it('explains what each view holds, from what the code puts there', async () => {
    await show(`/chunk-runs/${RUN}?tab=questions`);
    expect(await screen.findByText(/Exam questions extracted from question-bank, question-paper and answer-key documents/)).toBeVisible();
    expect(screen.getByText(/none is ever inferred/)).toBeVisible();
  });

  it('moves between views with the arrow keys, as a tab list does', async () => {
    await show(`/chunk-runs/${RUN}?tab=chunks`);
    const chunksTab = await screen.findByRole('tab', { name: /Chunks/ });
    expect(screen.getByRole('tablist', { name: 'Inspector views' })).toBeVisible();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'tab-chunks');
    fireEvent.keyDown(chunksTab, { key: 'ArrowRight' });
    await waitFor(() => expect(screen.getByRole('tab', { name: /Questions/ })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByRole('tab', { name: /Questions/ })).toHaveFocus();
  });
});

describe('the blocking passage', () => {
  it('opens on the affected chunk from a deep link and highlights it in the list', async () => {
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const detail = await screen.findByRole('region', { name: /Selected chunk #21/ });
    expect(within(detail).getByText('Blocking', { selector: '.chunk-state' })).toBeVisible();
    expect(await within(detail).findByText('396 / 384 retrieval tokens')).toBeVisible();
    const card = (await screen.findAllByRole('heading', { name: '#21 TABLE_PART' }))
      .map(h => h.closest('li')).find(Boolean)!;
    expect(card).toHaveAttribute('aria-current', 'true');
    expect(card.className).toContain('chunk-blocking');
    expect(screen.getByRole('radio', { name: /Blocking/ })).toBeChecked();
    expect(within(detail).getByRole('button', { name: 'Back to findings' })).toBeVisible();
  });

  it('tells blocking, warning and clean passages apart in words', async () => {
    await show(`/chunk-runs/${RUN}?tab=chunks`);
    const cards = await screen.findAllByRole('listitem');
    const states = cards.map(c => c.querySelector('.chunk-state')?.textContent).filter(Boolean);
    expect(states).toEqual(expect.arrayContaining(['No findings', 'Warning', 'Blocking']));
    const clean = cards.find(c => c.textContent?.includes('#1 TEXT_CHILD'))!;
    expect(clean.className).not.toContain('chunk-blocking');
    expect(clean.className).not.toContain('chunk-warning');
  });

  it('filters the list by validation state', async () => {
    await show(`/chunk-runs/${RUN}?tab=chunks`);
    fireEvent.click(await screen.findByRole('radio', { name: /Warnings/ }));
    await waitFor(() => expect(calls.some(c => c.url.includes('/chunks?') && c.url.includes('status=warning'))).toBe(true));
    expect(await screen.findByRole('heading', { name: '#3 FIGURE_CONTEXT' })).toBeVisible();
  });

  it('links from a finding to its chunk', async () => {
    await show(`/chunk-runs/${RUN}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect affected chunk' }));
    expect(await screen.findByRole('region', { name: /Selected chunk #21/ })).toBeVisible();
    expect(screen.getByRole('tab', { name: /Chunks/ })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('what to do about it', () => {
  it('recommends rechunking once the chunker has changed, and says why', async () => {
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    const rechunk = await within(guide).findByRole('button', { name: 'Rechunk' });
    expect(rechunk.closest('.remedy')).toHaveTextContent('Recommended');
    expect(guide).toHaveTextContent('The chunker has been updated since this attempt (1.0.0 → 1.1.0)');
    expect(guide).toHaveTextContent('If it is correct: Rechunk');
    // Reparse is the path for a wrong extraction, not the default.
    expect(guide).toHaveTextContent('If it is wrong: Reparse first');
    expect(within(guide).getByRole('button', { name: 'Reparse' }).closest('.remedy')).not.toHaveTextContent('Recommended');
  });

  it('never offers re-embedding or a keyword rebuild for passages that failed validation', async () => {
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    await within(guide).findByRole('button', { name: 'Rechunk' });
    expect(within(guide).queryByRole('button', { name: 'Re-embed' })).toBeNull();
    expect(within(guide).queryByRole('button', { name: 'Rebuild keyword index' })).toBeNull();
    expect(within(guide).queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(guide).toHaveTextContent('Re-embed — Needs a valid passage set; these passages failed validation.');
  });

  it('recommends nothing when the same code already failed the same way', async () => {
    lifecycle = review();
    lifecycle = { ...lifecycle, review: { ...lifecycle.review!, current_chunker_version: '1.0.0' } };
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    await within(guide).findByRole('button', { name: 'Rechunk' });
    expect(guide).not.toHaveTextContent('Recommended');
    expect(guide).toHaveTextContent('already stopped on the same issue once with unchanged settings');
  });

  it('starts a rechunk against the job when asked', async () => {
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    fireEvent.click(await within(guide).findByRole('button', { name: 'Rechunk' }));
    await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url.endsWith('/ingestion/jobs/job-1/rechunk'))).toBe(true));
    expect(calls.find(c => c.url.endsWith('/rechunk'))!.body).toBe('{"force":true}');
    expect(await within(guide).findByRole('status')).toHaveTextContent('Rechunk started');
  });

  it('says the document is being rebuilt once a rechunk has moved it out of review', async () => {
    lifecycle = lifecycleView({ state: 'PROCESSING', status: 'EMBEDDING', terminal: false, review: null });
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    expect(await within(guide).findByRole('status')).toHaveTextContent('The document is being processed again');
    expect(within(guide).getByRole('link', { name: 'Follow it on the document page' }))
      .toHaveAttribute('href', '/documents/doc-1#lifecycle');
    expect(within(guide).queryByRole('button', { name: 'Rechunk' })).toBeNull();
  });

  it('says an old review is superseded once the document is ready', async () => {
    lifecycle = lifecycleView();
    await show(`/chunk-runs/${RUN}`);
    expect(await screen.findByText(/has since been rebuilt and is ready for Ask/)).toBeVisible();
  });

  it('shows a reader the problem and no reprocessing control', async () => {
    permissions = ['document:read', 'ingestion:read'];
    lifecycle = review({ actions: [] });
    await show(`/chunk-runs/${RUN}?status=blocking&chunk=chunk-blk`);
    const guide = await screen.findByRole('complementary', { name: 'What should I do?' });
    expect(await within(guide).findByText(/A curator can resolve this/)).toBeVisible();
    expect(within(guide).queryAllByRole('button')).toHaveLength(0);
  });
});

describe('rules', () => {
  it('measures a size finding from its record, else from the chunk and the run policy', () => {
    expect(measurement({ code: 'CHUNK_OVERSIZED', severity: 'ERROR', details: {} },
      { retrieval_token_count: 396, token_count: 390 }, run)).toBe('396 / 384 retrieval tokens');
    expect(measurement({ code: 'CHUNK_OVERSIZED', severity: 'ERROR', details: { measured_tokens: 400, limit_tokens: 384 } },
      null, run)).toBe('400 / 384 retrieval tokens');
    expect(measurement({ code: 'CHUNK_FIGURE_NO_TEXT', severity: 'WARNING', details: {} }, null, run)).toBeNull();
    expect(retrievalBudget({ config_snapshot: {} })).toBeNull();
  });

  it('does not draw an empty cell where a merged cell already is', () => {
    // Row 2's innervation spans rows 2-9; rows 3-9 must not get a cell of their own there.
    const cells = [{ row: 2, column: 3, row_span: 8 }, { row: 3, column: 4 }];
    expect(covered(cells, 3, 3)).toBe(true);
    expect(covered(cells, 9, 3)).toBe(true);
    expect(covered(cells, 10, 3)).toBe(false);
    expect(covered(cells, 2, 3)).toBe(false);
    expect(covered(cells, 3, 4)).toBe(false);
  });

  it('puts a chunk in the state of its worst finding', () => {
    expect(chunkState({ findings: [] })).toBe('clean');
    expect(chunkState({ findings: [{ severity: 'WARNING', code: 'X', message: '', details: {} }] })).toBe('warning');
    expect(chunkState({ findings: [
      { severity: 'WARNING', code: 'X', message: '', details: {} },
      { severity: 'CRITICAL', code: 'Y', message: '', details: {} },
    ] })).toBe('blocking');
  });
});
