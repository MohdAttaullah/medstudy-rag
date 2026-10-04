import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { lifecycleView } from '../../test-lifecycle';

const version = {
  id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'READY_FOR_EMBEDDING',
  searchable: false, created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1',
  archived_at: null,
};
const document_ = {
  id: 'doc-1', title: 'Synthetic reference', source_type: 'QUESTION_BANK',
  authority_level: 'ASSESSMENT', created_at: '2026-09-05T00:00:00Z',
  created_by_user_id: 'user-1', latest_version: version, archived_at: null,
};
const chunkRun = {
  id: 'chunk-run-1', document_id: 'doc-1', document_version_id: 'version-1',
  parse_run_id: 'run-1', ingestion_job_id: 'job-1', generation: 0,
  status: 'SUCCEEDED', validation_result: 'PASS', is_active: true,
  chunker_name: 'medical-structure', chunker_version: '1.0.0',
  configuration_version: 'chunking-m3-v1', policy_fingerprint: 'fingerprint-1',
  config_snapshot: {}, tokenizer_name: 'ncbi/MedCPT-Article-Encoder',
  tokenizer_version: 'd05a736da4bb/0.23.2', input_fingerprint: 'input-1',
  metrics: {
    chunks: 6, parents: 1, children: 2, tables: 1, table_parts: 1, formulas: 1,
    figures: 0, questions: 1, missing_provenance: 0, omitted_characters: 0,
  },
  error_code: null, error_message: null, started_at: '2026-09-05T00:00:00Z',
  completed_at: '2026-09-05T00:00:03Z', duration_ms: 3000, created_at: '2026-09-05T00:00:00Z',
};
const tableChunk = {
  id: 'chunk-2', chunk_run_id: 'chunk-run-1', parse_run_id: 'run-1', parent_chunk_id: null,
  question_id: null, chunk_type: 'TABLE_PART', sequence_number: 2,
  raw_text: 'Marker | Reading | Unit', normalized_text: 'Marker | Reading | Unit\nAlpha | 131.5 | mmol/L',
  retrieval_text: 'Context: Section one\n\nMarker | Reading | Unit\nAlpha | 131.5 | mmol/L',
  token_count: 24, retrieval_token_count: 30, page_start: 1, page_end: 1,
  chunk_hash: 'hash-table-part', chunk_metadata: {
    part_number: 1, part_count: 2, row_indexes: [1], header_rows: [0],
    headers: 'Marker | Reading | Unit', caption: 'Table 1. Synthetic values.',
    possible_continuation: false,
    cells: [
      { row: 0, column: 0, text: 'Marker', row_span: 1, col_span: 1 },
      { row: 0, column: 1, text: 'Reading', row_span: 1, col_span: 1 },
      { row: 0, column: 2, text: 'Unit', row_span: 1, col_span: 1 },
      { row: 1, column: 0, text: 'Alpha', row_span: 1, col_span: 1 },
      { row: 1, column: 1, text: '131.5', row_span: 1, col_span: 1 },
      { row: 1, column: 2, text: 'mmol/L', row_span: 1, col_span: 1 },
    ],
  },
  artifacts: [{ table_id: 'table-1', figure_id: null, formula_id: null }],
  next_sibling_ids: [],
};
const textChunk = {
  ...tableChunk, id: 'chunk-1', chunk_type: 'TEXT_CHILD', sequence_number: 1,
  parent_chunk_id: 'chunk-0', normalized_text: 'Synthetic source paragraph retained verbatim.',
  retrieval_text: 'Context: Section one\n\nSynthetic source paragraph retained verbatim.',
  chunk_hash: 'hash-text', chunk_metadata: {}, artifacts: [],
};
const sources = [{
  element: {
    id: 'element-1', page_id: 'page-1', page_number: 4, parent_element_id: null,
    element_type: 'PARAGRAPH', depth: 1, ordinal: 0, reading_order: 7,
    raw_text: null,
    normalized_text: 'Synthetic source paragraph retained verbatim. A trailing source sentence.',
    text_normalized: false, source_parser_ref: '#/texts/7', source_label: 'text',
    content_layer: 'body', structure_inferred: false,
    bbox: { x1: 40, y1: 100, x2: 560, y2: 140, origin: 'TOPLEFT' },
  },
  position: 0, start_offset: 0, end_offset: 45, role: 'SOURCE',
}];
const withAnswer = {
  id: 'question-1', question_hash: 'qhash', question_number: '1',
  question_text: 'Which synthetic marker is reported first?', question_type: 'MCQ',
  explicit_answer: 'B', explanation: 'The source explanation as printed.',
  extraction_status: 'EXPLICIT_PATTERN', structure_inferred: true, answer_inferred: false,
  authority: { authority_level: 'ASSESSMENT', source_type: 'QUESTION_BANK' },
  page_start: 4, page_end: 4,
  options: [
    { ordinal: 0, label: 'A', text: 'Synthetic option one' },
    { ordinal: 1, label: 'B', text: 'Synthetic option two' },
  ],
};
const withoutAnswer = {
  ...withAnswer, id: 'question-2', question_number: '2', explicit_answer: null, explanation: null,
  options: [
    { ordinal: 0, label: 'A', text: 'Synthetic option three' },
    { ordinal: 1, label: 'B', text: 'Synthetic option four' },
  ],
};

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body =
      url.includes('/auth/me') ? {
        user_id: 'user-1', display_name: 'Local tester', role: 'admin',
        permissions: ['document:read', 'document:manage', 'ingestion:read', 'ingestion:rechunk'],
      } :
      url.includes('/chunks/chunk-2/sources') ? page(sources) :
      url.includes('/chunks/chunk-1/sources') ? page(sources) :
      url.includes('/chunks/chunk-2') ? tableChunk :
      url.includes('/chunks/chunk-1') ? textChunk :
      url.includes('/chunk-runs/chunk-run-1/chunks') ? page([textChunk, tableChunk]) :
      url.includes('/chunk-runs/chunk-run-1/questions') ? page([withAnswer, withoutAnswer]) :
      url.includes('/chunk-runs/chunk-run-1/validation-findings') ? page([]) :
      url.includes('/chunk-runs/chunk-run-1/review-summary') ? { chunks: 2, questions: 2, findings: 0,
        blocking_findings: 0, warning_findings: 0, info_findings: 0, blocking_chunks: 0,
        warning_chunks: 0, clean_chunks: 2, dataset_findings: 0 } :
      url.includes('/chunk-runs/chunk-run-1') ? chunkRun :
      url.includes('/chunk-runs') ? page([chunkRun]) :
      url.includes('/embedding') ? { document_version_id: 'version-1', ingestion_status: 'READY_FOR_EMBEDDING', embedding_run: null, embedding_version: null, index_run: null, embedding_runs: 0, finding_counts: {}, chunk_types: {} } :
      url.includes('/parse') ? { document_version_id: 'version-1', ingestion_status: 'READY_FOR_EMBEDDING', parse_run: null, parse_runs: 0 } :
      url.includes('/versions') ? page([version]) :
      url.includes('/lifecycle') ? lifecycleView() :
      url.includes('/documents/doc-1') ? document_ :
      page([]);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function show(path: string) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  // Signed in: the header offers the account control, which exists only once a principal is held.
  // This used to wait for the principal's name, which the content area printed above every page;
  // that line now lives inside the account menu in the header.
  await screen.findByRole('button', { name: 'Account and workspace' });
}

describe('M3 chunk status on the document details page', () => {
  it('shows the real chunker, policy and structural counts', async () => {
    await show('/documents/doc-1');
    expect(await screen.findByText(/medical-structure 1\.0\.0/)).toBeInTheDocument();
    expect(screen.getByText(/chunking-m3-v1/)).toBeInTheDocument();
    expect(screen.getByText(/6 chunks/)).toBeInTheDocument();
    expect(screen.getByText(/1 parent · 2 child · 1 table · 1 formula · 0 figure · 1 question/))
      .toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open chunk inspector' }))
      .toHaveAttribute('href', '/chunk-runs/chunk-run-1');
  });

  it('never presents chunks as retrievable evidence', async () => {
    await show('/documents/doc-1');
    await screen.findByText(/medical-structure 1\.0\.0/);
    const body = window.document.body.textContent ?? '';
    expect(body).toContain('This version is not searchable.');
    for (const absent of ['vectors indexed', 'evidence available', 'embedding complete', 'ready to answer']) {
      expect(body.toLowerCase()).not.toContain(absent);
    }
  });
});

describe('M3 chunk inspector', () => {
  const path = '/chunk-runs/chunk-run-1';

  it('shows source text, retrieval representation, tokens and hash for a chunk', async () => {
    await show(path);
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect chunk 1' }));
    const detail = await screen.findByRole('region', { name: /^Selected chunk/ });
    expect(await within(detail).findByText('Synthetic source paragraph retained verbatim.')).toBeInTheDocument();
    expect(within(detail).getByText(/Context: Section one/)).toBeInTheDocument();
    expect(within(detail).getByText(/SHA-256: hash-text/)).toBeInTheDocument();
    expect(within(detail).getByText(/24 source tokens · 30 retrieval tokens/)).toBeInTheDocument();
  });

  it('links a chunk back to its exact source element and page', async () => {
    await show(path);
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect chunk 1' }));
    const detail = await screen.findByRole('region', { name: /^Selected chunk/ });
    expect(await within(detail).findByText(/SOURCE \/ page 4 \/ reading order 7 \/ offsets 0–45/)).toBeInTheDocument();
    expect(await within(detail).findByRole('link', { name: 'Open source page' }))
      .toHaveAttribute('href', '/documents/doc-1/versions/version-1/parse/run-1?page=4');
  });

  it('renders a table part with its repeated headers and source artifact', async () => {
    await show(path);
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect chunk 2' }));
    const detail = await screen.findByRole('region', { name: /^Selected chunk/ });
    const grid = await within(detail).findByRole('table');
    expect(within(grid).getByRole('columnheader', { name: 'Marker' })).toBeInTheDocument();
    expect(within(grid).getByRole('cell', { name: '131.5' })).toBeInTheDocument();
    expect(within(detail).getByText(/Part 1 of 2 · source rows 1 · repeated header rows 0/)).toBeInTheDocument();
    expect(within(detail).getByText(/Table artifact table-1/)).toBeInTheDocument();
  });

  it('shows an explicit source answer and says plainly when there is none', async () => {
    await show(path);
    fireEvent.click(await screen.findByRole('tab', { name: /^Questions/ }));
    expect(await screen.findByText(/Synthetic option two/)).toBeInTheDocument();
    expect(screen.getByText('B')).toBeInTheDocument();
    expect(screen.getByText('Absent; no answer inferred')).toBeInTheDocument();
  });
});
