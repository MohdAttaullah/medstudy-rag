import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { lifecycleView } from '../../test-lifecycle';

const version = {
  id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'READY_FOR_RETRIEVAL',
  searchable: false, created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1',
  archived_at: null,
};
const document_ = {
  id: 'doc-1', title: 'Synthetic reference', source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED',
  created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1', latest_version: version,
  archived_at: null,
};
const embeddingVersion = {
  id: 'ev-1', model_provider: 'huggingface', model_id: 'ncbi/MedCPT-Article-Encoder',
  model_revision: 'd05a736da4bb84ee4057b7f7999485be6ed85465',
  tokenizer_revision: 'd05a736da4bb84ee4057b7f7999485be6ed85465',
  model_checksum: 'a5d5ffe4d8666c1d0aa15f371b94fc3492ca8f927e5621abd4b3ee9fc845b0f3',
  embedding_dimension: 768, pooling_strategy: 'CLS', normalization: 'NONE',
  distance_metric: 'DOT', max_input_tokens: 512, dtype: 'float32',
  input_builder_version: 'medcpt-two-field-v1', configuration_version: 'embedding-m4-v1',
  semantics_fingerprint: 'fingerprint-1', library_versions: { torch: '2.14.0' },
  created_at: '2026-09-05T00:00:00Z',
};
const embeddingRun = {
  id: 'er-1', document_id: 'doc-1', document_version_id: 'version-1', chunk_run_id: 'cr-1',
  embedding_version_id: 'ev-1', ingestion_job_id: 'job-1', generation: 0,
  status: 'SUCCEEDED', is_active: true, eligible_chunk_count: 12, embedded_chunk_count: 12,
  reused_chunk_count: 2, failed_chunk_count: 0, skipped_chunk_count: 0,
  input_fingerprint: 'input-1', policy_fingerprint: 'policy-1',
  metrics: { indexed_points: 12 }, error_code: null, error_message: null,
  started_at: '2026-09-05T00:00:00Z', completed_at: '2026-09-05T00:00:09Z', duration_ms: 9000,
  created_at: '2026-09-05T00:00:00Z',
};
const indexRun = {
  id: 'ir-1', document_id: 'doc-1', document_version_id: 'version-1', chunk_run_id: 'cr-1',
  embedding_run_id: 'er-1', embedding_version_id: 'ev-1', attempt: 0,
  physical_collection: 'medrag_chunks_v1_9f291d23639d', alias: 'medrag_chunks_active',
  vector_name: 'medcpt_dense', schema_version: 'v1', status: 'VERIFIED', is_active: true,
  expected_point_count: 12, indexed_point_count: 12, verified_point_count: 12, upsert_batches: 1,
  policy_fingerprint: 'index-1', metrics: {}, error_code: null, error_message: null,
  started_at: '2026-09-05T00:00:05Z', completed_at: '2026-09-05T00:00:09Z',
  activated_at: '2026-09-05T00:00:09Z', duration_ms: 4000, created_at: '2026-09-05T00:00:05Z',
};
const summary = {
  document_version_id: 'version-1', ingestion_status: 'READY_FOR_RETRIEVAL',
  embedding_run: embeddingRun, embedding_version: embeddingVersion, index_run: indexRun,
  embedding_runs: 1, finding_counts: {}, chunk_types: { TEXT_CHILD: 9, TABLE_PART: 3 },
};
const embedding = {
  id: 'ce-1', embedding_run_id: 'er-1', chunk_id: 'chunk-1', point_id: 'point-1',
  vector_name: 'medcpt_dense', input_hash: 'a'.repeat(64), vector_checksum: 'b'.repeat(64),
  dimension: 768, token_count: 143, vector_norm: 8.738, truncated: false, reused: false,
};
const chunk = {
  id: 'chunk-1', chunk_run_id: 'cr-1', parse_run_id: 'run-1', parent_chunk_id: null,
  question_id: null, chunk_type: 'TEXT_CHILD', sequence_number: 4,
  raw_text: 'Synthetic body.', normalized_text: 'Synthetic body.',
  retrieval_text: 'Context: Chapter one\n\nSynthetic body.',
  token_count: 12, retrieval_token_count: 18, page_start: 2, page_end: 2,
  chunk_hash: 'c'.repeat(64), chunk_metadata: {},
};
const statistics = {
  index_run_id: 'ir-1', collection: 'medrag_chunks_v1_9f291d23639d', vector_name: 'medcpt_dense',
  dimension: 768, distance_metric: 'DOT', expected_points: 12, live_points: 12,
  alias: 'medrag_chunks_active', alias_target: 'medrag_chunks_v1_9f291d23639d', reachable: true,
};

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body =
      url.includes('/auth/me') ? {
        user_id: 'user-1', display_name: 'Local tester', role: 'admin',
        permissions: ['document:read', 'document:manage', 'ingestion:read', 'ingestion:reembed'],
      } :
      url.includes('/index-runs/ir-1/statistics') ? statistics :
      url.includes('/index-runs/ir-1') ? indexRun :
      url.includes('/embedding-runs/er-1/embeddings') ? page([embedding]) :
      url.includes('/embedding-runs/er-1/validation-findings') ? page([]) :
      url.includes('/embedding-runs/er-1') ? embeddingRun :
      url.includes('/embedding-versions') ? page([embeddingVersion]) :
      url.includes('/chunks/chunk-1') ? chunk :
      url.includes('/embedding') ? summary :
      url.includes('/parse') ? { document_version_id: 'version-1', ingestion_status: 'READY_FOR_RETRIEVAL', parse_run: null, parse_runs: 0 } :
      url.includes('/chunk-runs') ? page([]) :
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

describe('M4 embedding status on the document details page', () => {
  it('shows the real model, revision, vector semantics and counts', async () => {
    await show('/documents/doc-1');
    expect(await screen.findByText(/ncbi\/MedCPT-Article-Encoder @ d05a736da4bb/)).toBeInTheDocument();
    expect(screen.getByText(/768-dimensional · CLS pooling · unnormalized · DOT similarity/)).toBeInTheDocument();
    expect(screen.getByText(/12 eligible · 12 embedded · 2 reused · 0 failed/)).toBeInTheDocument();
    expect(screen.getByText(/12 of 12 points verified · vector medcpt_dense/)).toBeInTheDocument();
    expect(screen.getByText(/Embeddings complete/)).toBeInTheDocument();
    expect(screen.getByText(/Index verified/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open index inspector' }))
      .toHaveAttribute('href', '/index-runs/ir-1');
  });

  it('states that a verified index is not an answerable document', async () => {
    await show('/documents/doc-1');
    await screen.findByText(/ncbi\/MedCPT-Article-Encoder/);
    const body = window.document.body.textContent ?? '';
    expect(body).toContain('available to Ask only once the keyword index has also verified');
    expect(body).toContain('Parent chunks are not indexed.');
    for (const absent of ['relevance score', 'ranked results', 'evidence available', 'ready to answer']) {
      expect(body.toLowerCase()).not.toContain(absent);
    }
  });

  it('says plainly when a version has not been embedded', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const body =
        url.includes('/auth/me') ? { user_id: 'user-1', display_name: 'Local tester', role: 'admin', permissions: ['document:read'] } :
        url.includes('/embedding') ? { ...summary, embedding_run: null, embedding_version: null, index_run: null, embedding_runs: 0 } :
        url.includes('/parse') ? { document_version_id: 'version-1', ingestion_status: 'READY_FOR_EMBEDDING', parse_run: null, parse_runs: 0 } :
        url.includes('/chunk-runs') ? page([]) :
        url.includes('/versions') ? page([version]) :
        url.includes('/lifecycle') ? lifecycleView() :
      url.includes('/documents/doc-1') ? document_ : page([]);
      return new Response(JSON.stringify(body), { status: 200 });
    }));
    await show('/documents/doc-1');
    expect(await screen.findByText(/has not been embedded yet/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open index inspector' })).not.toBeInTheDocument();
  });
});

describe('M4 index inspector', () => {
  const path = '/index-runs/ir-1';

  it('shows the collection, vector name, dimension, metric and reconciliation counts', async () => {
    await show(path);
    expect(await screen.findByText('Active vector index')).toBeInTheDocument();
    expect(screen.getByText('medrag_chunks_v1_9f291d23639d')).toBeInTheDocument();
    expect(screen.getByText('medcpt_dense')).toBeInTheDocument();
    expect(screen.getByText('768')).toBeInTheDocument();
    expect(screen.getByText('DOT')).toBeInTheDocument();
    expect(screen.getByText('d05a736da4bb84ee4057b7f7999485be6ed85465')).toBeInTheDocument();
    expect(screen.getByText(/12 point\(s\) present for this run/)).toBeInTheDocument();
    expect(await screen.findByText(/Parent chunks are excluded from first-stage retrieval/)).toBeInTheDocument();
  });

  it('lists point metadata without ever exposing a dense vector', async () => {
    await show(path);
    await screen.findByText('Active vector index');
    expect(await screen.findByText(/768 dimensions · 143 input tokens · norm 8\.738/)).toBeInTheDocument();
    expect(screen.getByText(/Vector SHA-256: b{64}/)).toBeInTheDocument();
    expect(screen.getByText(/Input SHA-256: a{64}/)).toBeInTheDocument();
    const body = window.document.body.textContent ?? '';
    // A 768-number array would show as a long comma-separated run of floats.
    expect(body).not.toMatch(/(-?\d+\.\d+,\s*){20}/);
  });

  it('resolves a point back to its source chunk and its retrieval representation', async () => {
    await show(path);
    fireEvent.click(await screen.findByRole('button', { name: 'Inspect source chunk' }));
    const detail = await screen.findByLabelText('Source chunk');
    expect(await within(detail).findByText(/TEXT_CHILD · pages 2–2/)).toBeInTheDocument();
    expect(within(detail).getByText(/Context: Chapter one/)).toBeInTheDocument();
    expect(within(detail).getByRole('link', { name: 'Open chunk inspector' }))
      .toHaveAttribute('href', '/chunk-runs/cr-1');
  });
});

describe('M4 operations', () => {
  it('offers the real embedding stages and not the answerable state', async () => {
    await show('/operations');
    const filter = await screen.findByLabelText('Status filter');
    const options = within(filter).getAllByRole('option').map(option => option.textContent);
    for (const stage of ['EMBEDDING', 'INDEXING', 'VERIFYING_INDEX', 'READY_FOR_RETRIEVAL']) {
      expect(options).toContain(stage);
    }
    expect(options).not.toContain('READY');
  });
});
