import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { lifecycleView, stages } from '../../test-lifecycle';
import { duration, estimateText, factLine, guidanceFor, remedies, repeatWarning } from './lifecycle';
import { POLL_MS } from './useLifecycle';
import type { Lifecycle, Review } from '../../types/lifecycle';

/**
 * The document lifecycle as a person sees it: real stages, honest time and actionable review.
 *
 * Every state shown here is the server's. The tests feed the page a lifecycle response and assert
 * what the page says about it — including what it must never say: a percentage, a guessed
 * completion time, or a remedy the server has ruled out.
 */

const version = { id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'CHUNKING', created_at: '2026-09-05T00:00:00Z' };

const running: Lifecycle = lifecycleView({
  state: 'PROCESSING', status: 'CHUNKING', current_stage: 'PASSAGES', activity: 'CHUNKING', terminal: false,
  run_started_at: '2026-09-05T00:00:00Z', finished_at: null, current_stage_started_at: '2026-09-05T00:02:00Z',
  heartbeat_at: '2026-09-05T00:02:50Z', server_time: '2026-09-05T00:03:00Z',
  stages: stages({ RECEIVED: 'COMPLETED', READING: 'COMPLETED', PASSAGES: 'RUNNING' }).map(stage =>
    stage.code === 'READING' ? { ...stage, duration_ms: 95_000, facts: { pages: 12, tables: 3 } }
      : stage.code === 'PASSAGES' ? { ...stage, started_at: '2026-09-05T00:02:00Z' } : stage),
  estimate: { available: false, stage: 'PASSAGES', low_seconds: null, high_seconds: null, samples: 4, needed: 8, reason: 'INSUFFICIENT_HISTORY' },
});

const review: Review = {
  stage: 'CHUNK', run_id: 'chunk-run-9', blocking_count: 1, warning_count: 13, info_count: 0,
  identical_earlier_attempts: 1, retries_left: 2, max_retries: 3,
  groups: [
    { code: 'CHUNK_OVERSIZED', severity: 'ERROR', blocking: true, count: 1,
      message: 'TABLE_PART chunk is 396 tokens; the embedding budget is 384.',
      samples: [{ page: 10, chunk_id: 'chunk-77' }] },
    { code: 'CHUNK_FIGURE_NO_TEXT', severity: 'WARNING', blocking: false, count: 13,
      message: 'Figure has no caption or text.', samples: [{ page: 2, chunk_id: 'c1' }, { page: 3, chunk_id: 'c2' }] },
  ],
};

const needsReview: Lifecycle = lifecycleView({
  state: 'REVIEW_REQUIRED', status: 'NEEDS_REVIEW', current_stage: 'PASSAGES', terminal: true,
  finished_at: '2026-09-05T00:04:00Z',
  stages: stages({ RECEIVED: 'COMPLETED', READING: 'COMPLETED', PASSAGES: 'BLOCKED' }),
  review,
  actions: [
    { action: 'reparse', available: true, reason: null },
    { action: 'rechunk', available: true, reason: null },
    { action: 'reembed', available: false, reason: 'NOT_APPLICABLE' },
    { action: 'reindex-sparse', available: false, reason: 'NOT_APPLICABLE' },
  ],
});

const ADMIN = ['document:read', 'document:upload', 'document:manage', 'ingestion:read',
  'ingestion:retry', 'ingestion:reparse', 'ingestion:rechunk', 'ingestion:reembed', 'ingestion:reindex', 'ingestion:cancel'];
const READER = ['document:read', 'ingestion:read'];

let permissions = ADMIN;
let lifecycles: Lifecycle[] = [];
let summary: Record<string, unknown> | null = null;
let calls: { url: string; method: string; body: string | null }[] = [];

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }
function documentRow() {
  return { id: 'doc-1', title: 'Synthetic reference', source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED',
    created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1', latest_version: version, archived_at: null,
    lifecycle: summary };
}

beforeEach(() => {
  permissions = ADMIN; lifecycles = [running]; summary = null; calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : null });
    const body =
      url.includes('/auth/me') ? { user_id: 'user-1', display_name: 'Local tester', role: permissions === ADMIN ? 'admin' : 'reader', permissions } :
      url.includes('/health/ready') ? { status: 'ready', dependencies: {} } :
      url.includes('/uploads/limits') ? { max_upload_bytes: 1024, max_upload_mib: 1, allowed_mime_types: ['application/pdf'], files_per_request: 1 } :
      url.includes('/lifecycle') ? (lifecycles.length > 1 ? lifecycles.shift() : lifecycles[0]) :
      url.includes('/ingestion/jobs/job-1/') ? { id: 'job-1', status: 'CHUNKING' } :
      url.includes('/parse') ? { document_version_id: 'version-1', ingestion_status: 'CHUNKING', parse_run: null, parse_runs: 0 } :
      url.includes('/chunk-runs') ? page([]) :
      url.includes('/embedding') ? { document_version_id: 'version-1', ingestion_status: 'CHUNKING', embedding_run: null, embedding_version: null, index_run: null, embedding_runs: 0, finding_counts: {}, chunk_types: {} } :
      url.includes('/versions') ? page([version]) :
      url.includes('/documents/doc-1') ? documentRow() :
      url.includes('/documents') ? page([documentRow()]) :
      page([]);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function show(path: string) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByRole('button', { name: 'Account and workspace' });
}
const lifecycleCalls = () => calls.filter(call => call.url.endsWith('/lifecycle')).length;

describe('processing progress', () => {
  it('shows each stage as completed, running or not started, in words', async () => {
    await show('/documents/doc-1');
    const section = (await screen.findByRole('heading', { name: 'Preparing document for Ask' })).closest('section')!;
    const items = within(section).getAllByRole('listitem');
    expect(items.map(item => item.querySelector('.lc-step-name')!.textContent)).toEqual([
      'Upload received', 'Reading the document', 'Preparing searchable passages',
      'Creating search representations', 'Building the semantic search index',
      'Building the keyword search index', 'Ready for Ask',
    ]);
    expect(items[1]).toHaveTextContent('Completed');
    expect(items[1]).toHaveTextContent('1m 35s');
    expect(items[1]).toHaveTextContent('12 pages · 3 tables');
    expect(items[2]).toHaveTextContent('In progress');
    expect(items[2]).toHaveAttribute('aria-current', 'step');
    expect(items[3]).toHaveTextContent('Not started');
    expect(within(section).getByText('Creating passages')).toBeVisible();
  });

  it('never shows a percentage or a progress bar', async () => {
    await show('/documents/doc-1');
    const section = (await screen.findByRole('heading', { name: 'Preparing document for Ask' })).closest('section')!;
    expect(section.textContent).not.toMatch(/\d\s*%/);
    expect(within(section).queryByRole('progressbar')).toBeNull();
  });

  it('counts elapsed time from the server clock, not the browser clock', async () => {
    await show('/documents/doc-1');
    const section = (await screen.findByRole('heading', { name: 'Preparing document for Ask' })).closest('section')!;
    // run_started_at 00:00:00, server_time 00:03:00 — the browser's own date plays no part.
    expect(within(section).getByText('Elapsed').nextElementSibling).toHaveTextContent(/^3m 0[0-2]s$/);
    expect(within(section).getByText('Ready for Ask', { selector: 'dt' }).nextElementSibling).toHaveTextContent('Not yet');
  });

  it("reports a stopped document's processing time as its stages' time, not the last pass", async () => {
    // Reading took 5m 06s on the first pass; a rechunk then ran for 4s. "Took 4s" was untrue.
    lifecycles = [{ ...needsReview, run_started_at: '2026-09-05T01:00:00Z', finished_at: '2026-09-05T01:00:04Z',
      stages: needsReview.stages.map(stage =>
        stage.code === 'RECEIVED' ? { ...stage, duration_ms: 0 }
          : stage.code === 'READING' ? { ...stage, duration_ms: 306_000 }
            : stage.code === 'PASSAGES' ? { ...stage, duration_ms: 4_000 } : stage) }];
    await show('/documents/doc-1');
    const section = (await screen.findByRole('heading', { name: 'Paused for review' })).closest('section')!;
    expect(within(section).getByText('Processing time').nextElementSibling).toHaveTextContent('5m 10s');
    expect(within(section).queryByText('Took')).toBeNull();
  });

  it('says why there is no time estimate instead of inventing one', async () => {
    await show('/documents/doc-1');
    expect(await screen.findByText(/Time remaining is not available yet/)).toHaveTextContent('(4 of 8)');
    expect(screen.queryByText(/^Estimated:/)).toBeNull();
  });

  it('stops polling when the document reaches a waiting state', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    lifecycles = [running, needsReview];
    await show('/documents/doc-1');
    await screen.findByRole('heading', { name: 'Preparing document for Ask' });
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS + 100); });
    await screen.findByRole('heading', { name: 'Review required' });
    const after = lifecycleCalls();
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 4); });
    expect(lifecycleCalls()).toBe(after);
  });

  it('announces a change of stage once, and never the ticking clock', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const next = { ...running, current_stage: 'EMBEDDING' as const, activity: 'EMBEDDING' };
    lifecycles = [running, running, next];
    await show('/documents/doc-1');
    await screen.findByRole('heading', { name: 'Preparing document for Ask' });
    const live = document.querySelector('.lifecycle [aria-live="polite"]')!;
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS + 100); });
    expect(live.textContent).toBe('');
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS + 100); });
    await waitFor(() => expect(live.textContent).toBe('Preparing document for Ask. Creating search representations.'));
  });
});

describe('review required', () => {
  it('explains why processing stopped and separates blocking issues from warnings', async () => {
    lifecycles = [needsReview];
    await show('/documents/doc-1');
    const panel = (await screen.findByRole('heading', { name: 'Review required' })).closest<HTMLElement>('section.review-panel')!;
    expect(panel).toHaveTextContent('1 blocking issue');
    expect(panel).toHaveTextContent('13 warnings (not blocking)');
    expect(panel).toHaveTextContent('cannot appear in Ask answers until this is resolved');
    // The blocking issue is open with plain guidance; the thirteen warnings are one folded group.
    expect(within(panel).getByText('A passage is too large to search')).toBeVisible();
    expect(within(panel).getByText(/longer than the search model can read in full/)).toBeVisible();
    expect(within(panel).getByText(/Page 10/)).toBeVisible();
    const warnings = within(panel).getByText(/Warnings \(13\)/).closest('details')!;
    expect(warnings).not.toHaveAttribute('open');
    expect(within(warnings).getAllByText('Figure without text')).toHaveLength(1);
  });

  it('sends the reader to the exact passage first', async () => {
    lifecycles = [needsReview];
    await show('/documents/doc-1');
    expect(await screen.findByRole('link', { name: 'Inspect this issue' }))
      .toHaveAttribute('href', '/chunk-runs/chunk-run-9?chunk=chunk-77');
  });

  it('never offers re-embedding a chunk set that failed review, and warns before repeating', async () => {
    lifecycles = [needsReview];
    await show('/documents/doc-1');
    const panel = (await screen.findByRole('heading', { name: 'Review required' })).closest<HTMLElement>('section.review-panel')!;
    expect(within(panel).getByRole('button', { name: 'Rechunk' })).toBeEnabled();
    expect(within(panel).getByRole('button', { name: 'Reparse' })).toBeEnabled();
    expect(within(panel).queryByRole('button', { name: 'Re-embed' })).toBeNull();
    expect(within(panel).queryByRole('button', { name: /accept/i })).toBeNull();
    expect(panel).toHaveTextContent('already stopped on the same issue once with unchanged settings');
  });

  it('runs the chosen remedy against the job and resumes following it', async () => {
    lifecycles = [needsReview];
    await show('/documents/doc-1');
    fireEvent.click(await screen.findByRole('button', { name: 'Rechunk' }));
    await waitFor(() => expect(calls.some(call => call.method === 'POST' && call.url.endsWith('/ingestion/jobs/job-1/rechunk'))).toBe(true));
    expect(calls.find(call => call.url.endsWith('/rechunk'))!.body).toBe('{"force":true}');
  });

  it('shows a reader the findings but no reprocessing controls', async () => {
    permissions = READER; lifecycles = [{ ...needsReview, actions: [] }];
    await show('/documents/doc-1');
    const panel = (await screen.findByRole('heading', { name: 'Review required' })).closest<HTMLElement>('section.review-panel')!;
    expect(within(panel).getByText('A passage is too large to search')).toBeVisible();
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    expect(panel).toHaveTextContent('A curator can resolve this.');
  });
});

describe('library status', () => {
  it('shows one compact line per document with a link to follow it', async () => {
    summary = { state: 'PROCESSING', current_stage: 'READING', activity: 'PARSING', run_started_at: '2026-09-05T00:00:00Z',
      finished_at: null, server_time: '2026-09-05T00:01:05Z', blocking_count: 0, warning_count: 0 };
    await show('/library');
    const row = (await screen.findByRole('link', { name: 'Synthetic reference' })).closest('tr')!;
    expect(row).toHaveTextContent(/Processing · Reading the document · 1m 0[5-7]s/);
    expect(within(row).getByRole('link', { name: /^View progress ?: Synthetic reference$/ })).toHaveAttribute('href', '/documents/doc-1#lifecycle');
    expect(within(row).queryByRole('list')).toBeNull();
  });

  it('labels a document awaiting review with its blocking count', async () => {
    summary = { state: 'REVIEW_REQUIRED', current_stage: 'PASSAGES', activity: null, run_started_at: null,
      finished_at: null, server_time: '2026-09-05T00:01:05Z', blocking_count: 1, warning_count: 13 };
    await show('/library');
    const row = (await screen.findByRole('link', { name: 'Synthetic reference' })).closest('tr')!;
    expect(row).toHaveTextContent('Review required · 1 blocking');
    expect(within(row).getByRole('link', { name: /^Review ?: Synthetic reference$/ })).toBeVisible();
  });
});

describe('lifecycle wording rules', () => {
  it('gives CHUNK_OVERSIZED different advice at each severity', () => {
    const base = { code: 'CHUNK_OVERSIZED', count: 1, message: '', samples: [] };
    expect(guidanceFor({ ...base, severity: 'ERROR', blocking: true })!.remedies).toEqual(['rechunk', 'reparse']);
    expect(guidanceFor({ ...base, severity: 'WARNING', blocking: false })!.remedies).toEqual([]);
    expect(guidanceFor({ ...base, code: 'SOMETHING_NEW', severity: 'ERROR', blocking: true })).toBeNull();
  });

  it('offers only remedies the server allows, never re-embed for a failed chunk set', () => {
    expect(remedies(review, new Set(['rechunk', 'reparse', 'reembed']))).toEqual(['rechunk', 'reparse']);
    expect(remedies(review, new Set(['reparse']))).toEqual(['reparse']);
    expect(remedies(review, new Set())).toEqual([]);
  });

  it('warns about repeating an identical attempt only when one happened', () => {
    expect(repeatWarning({ ...review, identical_earlier_attempts: 0 })).toBeNull();
    expect(repeatWarning(review)).toContain('2 remaining retries');
  });

  it('prints an estimate only as a labelled range from real samples', () => {
    const none = { available: false, stage: null, low_seconds: null, high_seconds: null, samples: 3, needed: 8 } as const;
    expect(estimateText({ ...none, reason: 'INSUFFICIENT_HISTORY' })).toContain('3 of 8');
    expect(estimateText({ ...none, reason: 'NOT_RUNNING' })).toBeNull();
    expect(estimateText({ ...none, reason: 'NOT_ESTIMATED_FOR_STAGE' })).toBe('Completion time depends on the document’s size and structure.');
    expect(estimateText({ available: true, stage: 'READING', low_seconds: 240, high_seconds: 600, samples: 9, needed: 8, reason: null }))
      .toBe('Estimated: about 4–10 min left in this stage, based on 9 similar documents on this server.');
  });

  it('never describes a small upload as 0.0 MB', () => {
    const received = stages({ RECEIVED: 'COMPLETED' })[0];
    expect(factLine({ ...received, facts: { file_size_bytes: 13_100 } })).toBe('13 KB');
    expect(factLine({ ...received, facts: { pages: 14, file_size_bytes: 8_780_000 } })).toBe('14 pages · 8.4 MB');
  });

  it('formats durations without false precision', () => {
    expect(duration(18_400)).toBe('18s');
    expect(duration(306_000)).toBe('5m 06s');
    expect(duration(4_020_000)).toBe('1h 07m');
  });
});
