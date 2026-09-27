import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';

/**
 * NEEDS_REVIEW is reached from three different stages and only the error code says which.
 *
 * A parse-stage review has no active parse dataset, so rechunking or re-embedding it fails with
 * CHUNK_SOURCE_PARSE_NOT_READY after spending one unit of the bounded retry budget. Offering
 * those actions was worse than useless — it looked like a way forward and quietly cost a retry.
 */

const base = {
  document_version_id: 'version-1', current_stage: 'NEEDS_REVIEW',
  requested_by_user_id: 'user-1', configuration_version: 'ingestion-m1-v1', config_snapshot: {},
  correlation_id: 'correlation-1', created_at: '2026-09-05T00:00:00Z', queued_at: null,
  started_at: null, completed_at: null, cancelled_at: null, queue_received_at: null,
  retry_count: 2, max_retries: 3, document_id: 'doc-1', document_title: 'Medical Microbiology',
  version_number: 1, events: [],
};
const parseReview = {
  ...base, id: 'job-parse', status: 'NEEDS_REVIEW',
  last_error_code: 'PARSE_NEEDS_REVIEW',
  last_error_message: 'Parse quality requires human review before further processing.',
};
const chunkReview = {
  ...base, id: 'job-chunk', status: 'NEEDS_REVIEW',
  last_error_code: 'CHUNK_NEEDS_REVIEW',
  last_error_message: 'Chunk quality findings require review.',
};

let jobs = [parseReview, chunkReview];

beforeEach(() => {
  jobs = [parseReview, chunkReview];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.includes('/auth/me')
      ? {
          user_id: 'user-1', display_name: 'Local curator', role: 'curator',
          permissions: [
            'document:read', 'ingestion:read', 'ingestion:retry', 'ingestion:reparse',
            'ingestion:rechunk', 'ingestion:reembed', 'ingestion:cancel', 'ingestion:accept',
          ],
        }
      : url.includes('/ingestion/jobs')
        ? { items: jobs, total: jobs.length, offset: 0, limit: 20 }
        : { items: [], total: 0, offset: 0, limit: 20 };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function operations() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/operations']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  // Signed in: the header offers the account control, which exists only once a principal is held.
  // This used to wait for the principal's name, which the content area printed above every page;
  // that line now lives inside the account menu in the header.
  await screen.findByRole('button', { name: 'Account and workspace' });
  return await screen.findAllByRole('article');
}

describe('reprocessing actions on a flagged job', () => {
  it('does not offer rechunk or re-embed for a parse-stage review', async () => {
    const [parseCard] = await operations();
    for (const name of ['Rechunk', 'Re-embed']) {
      expect(screen.getAllByRole('button', { name }).length).toBeGreaterThan(0);
    }
    const within_ = (label: string) =>
      Array.from(parseCard.querySelectorAll('button')).find(b => b.textContent === label);
    expect(within_('Rechunk')?.hasAttribute('disabled')).toBe(true);
    expect(within_('Re-embed')?.hasAttribute('disabled')).toBe(true);
    // Reparse remains available: it is a genuine way forward and correctly costs a retry.
    expect(within_('Reparse')?.hasAttribute('disabled')).toBe(false);
  });

  it('still offers rechunk for a chunk-stage review, which has a valid parse behind it', async () => {
    const cards = await operations();
    const chunkCard = cards[1];
    const button = Array.from(chunkCard.querySelectorAll('button'))
      .find(b => b.textContent === 'Rechunk');
    expect(button?.hasAttribute('disabled')).toBe(false);
  });

  it('points the curator at the review workflow instead of a dead end', async () => {
    await operations();
    expect(screen.getByText(/Open the document's parse inspector to review the findings/))
      .toBeTruthy();
  });
});
