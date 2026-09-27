import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';

const version = {
  id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'READY_FOR_CHUNKING',
  searchable: false, created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1',
  archived_at: null,
};
const document_ = {
  id: 'doc-1', title: 'Synthetic reference', source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED',
  created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1', latest_version: version,
  archived_at: null,
};
const run = {
  id: 'run-1', document_version_id: 'version-1', ingestion_job_id: 'job-1', attempt: 1,
  parser_name: 'docling', parser_provider: 'docling-project', parser_version: '2.126.0',
  configuration_version: 'parsing-m2-v1', configuration_fingerprint: 'abcdef0123456789',
  status: 'SUCCEEDED', is_active: true, validation_result: 'PASS_WITH_WARNINGS',
  ocr_mode: 'AUTO', ocr_engine: 'rapidocr', tables_enabled: true, formulas_enabled: true,
  figures_enabled: true, previews_enabled: true, page_count: 1, source_page_count: 1,
  element_count: 4, table_count: 1, figure_count: 1, formula_count: 1, ocr_page_count: 0,
  raw_artifact_bytes: 12777, duration_ms: 4200, started_at: '2026-09-05T00:00:00Z',
  completed_at: '2026-09-05T00:00:05Z', correlation_id: 'correlation-1',
  error_code: null, error_message: null, created_at: '2026-09-05T00:00:00Z',
  finding_counts: { WARNING: 1 },
};
const parsePage = {
  id: 'page-1', parse_run_id: 'run-1', page_number: 1, width: 612, height: 792, rotation: 0,
  element_count: 4, source_text_chars: 202, ocr_used: false, ocr_evidence: null,
  has_preview: true, preview_media_type: 'image/webp',
  extracted_text: 'Tabular Structure Fixture\n\nTable 1 lists synthetic values.',
};
const elements = [
  {
    id: 'element-1', page_id: 'page-1', page_number: 1, parent_element_id: null,
    element_type: 'HEADING', depth: 1, ordinal: 0, reading_order: 0,
    raw_text: 'Tabular Structure Fixture', normalized_text: 'Tabular Structure Fixture',
    text_normalized: false, source_parser_ref: '#/texts/0', source_label: 'section_header',
    content_layer: 'body', structure_inferred: false,
    bbox: { x1: 72, y1: 75.2, x2: 263.4, y2: 90.1, origin: 'TOPLEFT' },
  },
  {
    id: 'element-2', page_id: 'page-1', page_number: 1, parent_element_id: null,
    element_type: 'TABLE', depth: 1, ordinal: 1, reading_order: 1,
    raw_text: null, normalized_text: null, text_normalized: false,
    source_parser_ref: '#/tables/0', source_label: 'table', content_layer: 'body',
    structure_inferred: false, bbox: null,
  },
];
const table = {
  id: 'table-1', document_element_id: 'element-2', page_number: 1,
  caption_text: 'Table 1. Synthetic parameter table caption.', caption_element_id: 'element-3',
  row_count: 2, column_count: 2, header_row_count: 1, table_group_id: null,
  continuation_of_id: null, possible_continuation: false, continuation_evidence: null,
  malformed: false, bbox: null,
  cells: [
    { text: 'Parameter', row: 0, column: 0, row_span: 1, column_span: 1, column_header: true, row_header: false },
    { text: 'Group A', row: 0, column: 1, row_span: 1, column_span: 1, column_header: true, row_header: false },
    { text: 'Alpha', row: 1, column: 0, row_span: 1, column_span: 1, column_header: false, row_header: false },
    { text: '10', row: 1, column: 1, row_span: 1, column_span: 1, column_header: false, row_header: false },
  ],
  markdown: '| Parameter | Group A |', html: null,
};
const figure = {
  id: 'figure-1', document_element_id: 'element-4', page_number: 1,
  caption_text: 'Figure 1. Synthetic diagram caption.', caption_element_id: 'element-5',
  figure_kind: 'picture', image_media_type: 'image/png', image_width: 389, image_height: 245,
  image_bytes: 4270, has_image: true, bbox: null,
};
const formula = {
  id: 'formula-1', document_element_id: 'element-6', page_number: 1,
  source_expression: 'C = \\frac { A \\cdot B } { D }',
  normalized_expression: 'C = \\frac { A \\cdot B } { D }', notation: 'latex',
  preceding_element_id: 'element-1', following_element_id: null, bbox: null,
};
const finding = {
  id: 'finding-1', page_number: 1, document_element_id: null, scope: 'page', severity: 'WARNING',
  code: 'PAGE_EMPTY', message: 'This page yielded almost no extracted text.', details: {},
  created_at: '2026-09-05T00:00:05Z',
};

let summaryRun: unknown = run;
function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }

beforeEach(() => {
  summaryRun = run;
  // jsdom does not implement the object-URL helpers. Add them without replacing the URL
  // constructor itself, which fetch and the router both rely on.
  Object.assign(URL, {
    createObjectURL: () => 'blob:preview',
    revokeObjectURL: () => undefined,
  });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/preview') || url.includes('/image')) {
      return new Response(new Blob([new Uint8Array([1, 2, 3])]), { status: 200 });
    }
    const body =
      url.includes('/auth/me') ? {
        user_id: 'user-1', display_name: 'Local tester', role: 'admin',
        permissions: ['document:read', 'document:manage', 'ingestion:read', 'ingestion:reparse'],
      } :
      url.includes('/parse-runs/run-1/pages/page-1') ? parsePage :
      url.includes('/parse-runs/run-1/pages') ? page([parsePage]) :
      url.includes('/parse-runs/run-1/elements') ? page(elements) :
      url.includes('/parse-runs/run-1/tables/table-1') ? table :
      url.includes('/parse-runs/run-1/tables') ? page([table]) :
      url.includes('/parse-runs/run-1/figures') ? page([figure]) :
      url.includes('/parse-runs/run-1/formulas') ? page([formula]) :
      url.includes('/parse-runs/run-1/findings') ? page([finding]) :
      url.includes('/parse-runs/run-1') ? run :
      url.includes('/parse') ? {
        document_version_id: 'version-1', ingestion_status: 'READY_FOR_CHUNKING',
        parse_run: summaryRun, parse_runs: summaryRun ? 1 : 0,
      } :
      url.includes('/chunk-runs') ? page([]) :
      url.includes('/embedding') ? { document_version_id: 'version-1', ingestion_status: 'READY_FOR_CHUNKING', embedding_run: null, embedding_version: null, index_run: null, embedding_runs: 0, finding_counts: {}, chunk_types: {} } :
      url.includes('/versions') ? page([version]) :
      url.includes('/documents/doc-1') ? document_ :
      url.includes('/ingestion/jobs') ? page([]) :
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

describe('M2 parse status on the document details page', () => {
  it('shows real parser, counts, OCR and validation state', async () => {
    await show('/documents/doc-1');
    expect(await screen.findByText(/docling 2\.126\.0/)).toBeInTheDocument();
    expect(screen.getByText('parsing-m2-v1', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('Passed with warnings')).toBeInTheDocument();
    expect(screen.getByText(/Validation findings: 1 warning\./)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open parse inspector' })).toHaveAttribute(
      'href', '/documents/doc-1/versions/version-1/parse/run-1');
  });

  it('never presents chunks, embeddings or retrieval as available', async () => {
    await show('/documents/doc-1');
    await screen.findByText(/docling 2\.126\.0/);
    const body = window.document.body.textContent ?? '';
    for (const absent of ['chunks retrieved', 'vectors indexed', 'evidence available', 'embedding complete']) {
      expect(body.toLowerCase()).not.toContain(absent.toLowerCase());
    }
    expect(body).toContain('Embedding and retrieval are not implemented.');
    expect(body).toContain('This version is not searchable.');
  });

  it('states plainly when a version has not been parsed', async () => {
    summaryRun = null;
    await show('/documents/doc-1');
    expect(await screen.findByText(/has not been parsed yet/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open parse inspector' })).not.toBeInTheDocument();
  });
});

describe('M2 parse inspector', () => {
  const path = '/documents/doc-1/versions/version-1/parse/run-1';

  it('shows page metadata, preview and normalized text', async () => {
    await show(path);
    await screen.findByRole('heading', { name: 'Parse run 1' });
    expect(screen.getByText('1 (1-based, printed order)')).toBeInTheDocument();
    expect(screen.getByText('612 × 792 pt')).toBeInTheDocument();
    expect(screen.getByText('Not used')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByAltText('Rendered preview of page 1')).toBeInTheDocument());
    expect(screen.getByText(/Table 1 lists synthetic values\./)).toBeInTheDocument();
  });

  it('lists structured elements with reading order and coordinates', async () => {
    await show(path);
    expect(await screen.findByText('Heading')).toBeInTheDocument();
    expect(screen.getByText('Table')).toBeInTheDocument();
    expect(screen.getByText(/#0 · depth 1/)).toBeInTheDocument();
    expect(screen.getByText(/bbox 72\.0, 75\.2 → 263\.4, 90\.1 \(TOPLEFT, pt\)/)).toBeInTheDocument();
    expect(screen.getByText('No page coordinates reported by the parser', { exact: false }))
      .toBeInTheDocument();
  });

  it('renders the real table grid with its header cells and caption', async () => {
    await show(path);
    const grid = await screen.findByRole('table');
    expect(within(grid).getByRole('columnheader', { name: 'Parameter' })).toBeInTheDocument();
    expect(within(grid).getByRole('columnheader', { name: 'Group A' })).toBeInTheDocument();
    expect(within(grid).getByRole('cell', { name: 'Alpha' })).toBeInTheDocument();
    expect(within(grid).getByRole('cell', { name: '10' })).toBeInTheDocument();
    expect(screen.getByText(/1 header row\(s\) · 4 cells/)).toBeInTheDocument();
  });

  it('shows the extracted figure crop and the formula exactly as parsed', async () => {
    await show(path);
    await screen.findByRole('heading', { name: 'Figures' });
    await waitFor(() =>
      expect(screen.getByAltText('Figure 1. Synthetic diagram caption.')).toBeInTheDocument());
    expect(screen.getByText(/389 × 245 px/)).toBeInTheDocument();
    expect(screen.getByText('C = \\frac { A \\cdot B } { D }')).toBeInTheDocument();
    expect(screen.getByText(/never rewritten or interpreted/)).toBeInTheDocument();
  });

  it('surfaces validation findings with their severity', async () => {
    await show(path);
    await screen.findByRole('heading', { name: 'Validation findings' });
    expect(screen.getByText('PAGE_EMPTY')).toBeInTheDocument();
    expect(screen.getByText('This page yielded almost no extracted text.')).toBeInTheDocument();
    expect(screen.getByText('warning')).toBeInTheDocument();
  });
});

describe('M2 operations', () => {
  it('offers the real parse stages as filters', async () => {
    await show('/operations');
    const filter = await screen.findByLabelText('Status filter');
    const options = within(filter).getAllByRole('option').map(option => option.textContent);
    for (const stage of ['PARSING', 'NORMALIZING', 'ENRICHING', 'READY_FOR_CHUNKING']) {
      expect(options).toContain(stage);
    }
    // M3 made the chunk stages real and M4 the embedding and index stages. READY is still
    // unreachable: it is reserved for a version that can actually be answered from.
    expect(options).not.toContain('READY');
  });
});
