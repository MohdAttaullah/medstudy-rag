import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';

/**
 * Curator review of a flagged parse.
 *
 * What these protect: a curator cannot accept a parse without looking at what was flagged and
 * saying why, the automated verdict and findings stay on screen after acceptance, and a role
 * without the capability is told what is happening rather than shown a button that fails.
 */

const version = {
  id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'NEEDS_REVIEW',
  searchable: false, created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1',
  archived_at: null,
};
const document_ = {
  id: 'doc-1', title: 'Medical Microbiology', source_type: 'TEXTBOOK',
  authority_level: 'UNREVIEWED', created_at: '2026-09-05T00:00:00Z',
  created_by_user_id: 'user-1', latest_version: version, archived_at: null,
};
const flaggedRun = {
  id: 'run-1', document_version_id: 'version-1', ingestion_job_id: 'job-1', attempt: 1,
  parser_name: 'docling', parser_provider: 'docling-project', parser_version: '2.126.0',
  configuration_version: 'parsing-m2-v1', configuration_fingerprint: 'abcdef0123456789',
  status: 'FAILED', is_active: false, validation_result: 'NEEDS_REVIEW',
  ocr_mode: 'AUTO', ocr_engine: 'rapidocr', tables_enabled: true, formulas_enabled: true,
  figures_enabled: true, previews_enabled: true, page_count: 2, source_page_count: 2,
  element_count: 5, table_count: 0, figure_count: 2, formula_count: 0, ocr_page_count: 0,
  raw_artifact_bytes: 1000, duration_ms: 4200, started_at: '2026-09-05T00:00:00Z',
  completed_at: '2026-09-05T00:00:05Z', correlation_id: 'correlation-1',
  error_code: 'PARSE_NEEDS_REVIEW', error_message: 'Parse quality requires human review.',
  created_at: '2026-09-05T00:00:00Z', finding_counts: { ERROR: 1 },
};
const acceptedRun = { ...flaggedRun, status: 'REVIEWED_ACCEPTED', is_active: true };
const parsePage = {
  id: 'page-44', parse_run_id: 'run-1', page_number: 44, width: 612, height: 783, rotation: 0,
  element_count: 3, source_text_chars: 56, ocr_used: false, ocr_evidence: null,
  has_preview: false, preview_media_type: null,
  extracted_text: 'BASIC CONCEPTS IN THE IMMUNE RESPONSE',
};
const finding = {
  id: 'finding-1', parse_run_id: 'run-1', page_id: 'page-44', page_number: 44,
  document_element_id: null, scope: 'page', severity: 'ERROR', code: 'PAGE_CONTENT_LOST',
  message: 'This page has a source text layer but produced no parsed content.',
  details: { parsed_chars: 37, source_text_chars: 56 }, created_at: '2026-09-05T00:00:05Z',
};
const decision = {
  id: 'decision-1', parse_run_id: 'run-1', decision: 'ACCEPT', reviewer_user_id: 'user-1',
  rationale: 'Page 44 omits only a decorative section marker.',
  validation_result_at_decision: 'NEEDS_REVIEW', finding_count: 1,
  findings_digest: 'aabbccddeeff00112233445566778899', configuration_fingerprint: 'abcdef0123456789',
  correlation_id: 'correlation-2', created_at: '2026-09-12T00:00:00Z',
};

const CURATOR = ['document:read', 'ingestion:read', 'ingestion:accept'];
const READER = ['document:read', 'ingestion:read'];

let permissions = CURATOR;
let recorded: unknown = null;
let posted: Array<{ url: string; body: unknown }> = [];

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }

beforeEach(() => {
  permissions = CURATOR;
  recorded = null;
  posted = [];
  Object.assign(URL, {
    createObjectURL: () => 'blob:preview',
    revokeObjectURL: () => undefined,
  });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST' && url.includes('/review')) {
      posted.push({ url, body: JSON.parse(String(init.body)) });
      recorded = decision;
      return new Response(JSON.stringify(decision), { status: 200 });
    }
    const body =
      url.includes('/auth/me') ? {
        user_id: 'user-1', display_name: 'Local curator', role: 'curator', permissions,
      } :
      url.includes('/parse-runs/run-1/review') ? recorded :
      url.includes('/parse-runs/run-1/pages/page-44') ? parsePage :
      url.includes('/parse-runs/run-1/pages') ? page([parsePage]) :
      url.includes('/parse-runs/run-1/findings') ? page([finding]) :
      url.includes('/parse-runs/run-1/elements') ? page([]) :
      url.includes('/parse-runs/run-1/tables') ? page([]) :
      url.includes('/parse-runs/run-1/figures') ? page([]) :
      url.includes('/parse-runs/run-1/formulas') ? page([]) :
      url.includes('/parse-runs/run-1') ? (recorded ? acceptedRun : flaggedRun) :
      url.includes('/documents/doc-1') ? document_ :
      page([]);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function inspector() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/documents/doc-1/versions/version-1/parse/run-1']}>
      <App />
    </MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByText(/Local curator/);
}

describe('curator review of a flagged parse', () => {
  it('shows what was flagged and refuses to submit without a rationale and confirmation', async () => {
    await inspector();
    await screen.findByRole('heading', { name: 'Review required' });
    // Once in the review panel and once in the findings list: the curator sees what was
    // flagged in the place they decide, without it leaving the record above.
    expect((await screen.findAllByText(/PAGE_CONTENT_LOST/)).length).toBe(2);

    const submit = screen.getByRole('button', { name: 'Accept this parse' });
    expect(submit.hasAttribute('disabled')).toBe(true);

    fireEvent.change(screen.getByLabelText(/Rationale/), { target: { value: 'too short' } });
    expect(submit.hasAttribute('disabled')).toBe(true);

    fireEvent.change(screen.getByLabelText(/Rationale/), {
      target: { value: 'Page 44 omits only a decorative section marker.' },
    });
    expect(submit.hasAttribute('disabled')).toBe(true);

    fireEvent.click(screen.getByLabelText(/I have reviewed the flagged pages/));
    expect(submit.hasAttribute('disabled')).toBe(false);
  });

  it('records the decision the curator actually wrote', async () => {
    await inspector();
    await screen.findByRole('heading', { name: 'Review required' });
    fireEvent.change(screen.getByLabelText(/Rationale/), {
      target: { value: '  Page 517 body text is retained on page 516.  ' },
    });
    fireEvent.click(screen.getByLabelText(/I have reviewed the flagged pages/));
    fireEvent.click(screen.getByRole('button', { name: 'Accept this parse' }));

    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].body).toEqual({
      decision: 'ACCEPT',
      rationale: 'Page 517 body text is retained on page 516.',
    });
  });

  it('keeps the validation result and findings visible after acceptance', async () => {
    recorded = decision;
    await inspector();
    await screen.findByRole('heading', { name: 'Review decision' });
    expect(screen.getByText(decision.rationale)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Accept this parse' })).toBeNull();
    // The automated verdict is still on screen, unchanged, next to the acceptance.
    expect(screen.getAllByText(/Needs review before further processing/).length).toBeGreaterThan(0);
    expect(screen.getByText('Accepted after review')).toBeTruthy();
    expect(await screen.findByText(/PAGE_CONTENT_LOST/)).toBeTruthy();
  });

  it('tells a role without the capability what is happening instead of offering a dead button', async () => {
    permissions = READER;
    await inspector();
    await screen.findByRole('heading', { name: 'Review required' });
    expect(screen.getByText(/your role cannot record that decision/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Accept this parse' })).toBeNull();
  });
});
