import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { lifecycleView } from '../../test-lifecycle';
import * as client from '../../api/client';

const version = { id: 'version-1', document_id: 'doc-1', version_number: 1, edition: 'First',
  original_filename: 'source.pdf', normalized_filename: 'source.pdf', file_size_bytes: 100,
  sha256: 'abc123', publication_year: 2025, ingestion_status: 'QUEUED', created_at: '2026-09-05T00:00:00Z' };
const document = { id: 'doc-1', title: 'Synthetic reference', source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED',
  created_at: '2026-09-05T00:00:00Z', created_by_user_id: 'user-1', latest_version: version, archived_at: null };
const job = { id: 'job-1', document_id: 'doc-1', document_title: 'Synthetic reference', document_version_id: 'version-1',
  version_number: 1, status: 'QUEUED', current_stage: 'QUEUED', retry_count: 0, max_retries: 3,
  created_at: '2026-09-05T00:00:00Z', started_at: null, correlation_id: 'correlation-1', queue_received_at: null,
  configuration_version: 'ingestion-m1-v1', events: [
    { id: 'event-1', to_status: 'UPLOADED', service_identity: 'api', retry_number: 0, created_at: '2026-09-05T00:00:00Z' },
    { id: 'event-2', to_status: 'VALIDATING', service_identity: 'api', retry_number: 0, created_at: '2026-09-05T00:00:01Z' },
    { id: 'event-3', to_status: 'QUEUED', service_identity: 'api', retry_number: 0, created_at: '2026-09-05T00:00:02Z' },
  ] };
let uploaded = false;
let reader = false;
function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }
beforeEach(() => {
  uploaded = false; reader = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.includes('/auth/me') ? { user_id: 'user-1', display_name: 'Local tester', role: reader ? 'reader' : 'admin',
      permissions: reader ? ['document:read', 'ingestion:read'] : ['document:read', 'document:upload', 'document:manage', 'ingestion:read', 'ingestion:retry', 'ingestion:cancel'] } :
      url.includes('/health/ready') ? { status: 'ready', dependencies: { postgres: true } } :
      url.includes('/uploads/limits') ? { max_upload_bytes: 512 * 1024 * 1024, max_upload_mib: 512,
        allowed_mime_types: ['application/pdf'], files_per_request: 1 } :
      url.includes('/ingestion/jobs/job-1') ? job :
      url.includes('/ingestion/jobs') ? page([job]) :
      // The details page's per-version panels, each with nothing recorded yet.
      url.includes('/parse') ? { document_version_id: 'version-1', ingestion_status: 'QUEUED', parse_run: null, parse_runs: 0 } :
      url.includes('/chunk-runs') ? page([]) :
      url.includes('/embedding') ? { document_version_id: 'version-1', ingestion_status: 'QUEUED', embedding_run: null, embedding_version: null, index_run: null, embedding_runs: 0, finding_counts: {}, chunk_types: {} } :
      url.includes('/versions') ? page([version]) :
      url.includes('/lifecycle') ? lifecycleView() :
      url.includes('/documents/doc-1') ? document :
      page(uploaded ? [document] : []);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function show(path = '/library') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  // Signed in: the header offers the account control, which exists only once a principal is held.
  // This used to wait for the principal's name, which the content area printed above every page;
  // that line now lives inside the account menu in the header.
  await screen.findByRole('button', { name: 'Account and workspace' });
}
function fill() {
  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Synthetic reference' } });
  fireEvent.change(screen.getByLabelText('Choose PDF files or drag them here'), {
    target: { files: [new File(['%PDF-safe fixture'], 'source.pdf', { type: 'application/pdf' })] },
  });
}
describe('M1 document workflows', () => {
  it('requires a file and metadata without sending an upload', async () => {
    const upload = vi.spyOn(client, 'uploadPdf');
    await show();
    fireEvent.submit(screen.getByRole('button', { name: 'Upload documents' }).closest('form')!);
    expect(screen.getByRole('alert')).toHaveTextContent('Choose at least one PDF');
    expect(upload).not.toHaveBeenCalled();
  });
  it('shows real transfer progress then the committed queued document', async () => {
    let finish!: (value: unknown) => void;
    vi.spyOn(client, 'uploadPdf').mockImplementation((_token, _file, _meta, _key, progress) => {
      progress(70); return new Promise(resolve => { finish = resolve; });
    });
    await show(); fill();
    fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    expect(await screen.findByLabelText('File transfer: 70%')).toHaveValue(70);
    uploaded = true;
    finish({ document_id: 'doc-1', version_id: 'version-1', job_id: 'job-1', status: 'QUEUED', replayed: false });
    expect(await screen.findByRole('link', { name: 'Open uploaded document' })).toHaveAttribute('href', '/documents/doc-1');
    expect(await screen.findByRole('link', { name: 'Synthetic reference' })).toBeVisible();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
  it('shows a duplicate warning and links to the existing source', async () => {
    vi.spyOn(client, 'uploadPdf').mockRejectedValue(new client.ApiError('UPLOAD_DUPLICATE', 'This file already exists.', { document_id: 'doc-1' }));
    await show(); fill(); fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Duplicate file');
    expect(screen.getByRole('link', { name: 'View existing document' })).toHaveAttribute('href', '/documents/doc-1');
  });
  it('shows storage errors without inventing a successful upload', async () => {
    vi.spyOn(client, 'uploadPdf').mockRejectedValue(new client.ApiError('STORAGE_FAILURE', 'Storage unavailable.'));
    await show(); fill(); fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Storage unavailable');
    expect(screen.queryByRole('link', { name: 'Open uploaded document' })).not.toBeInTheDocument();
  });
  it('reuses the same idempotency key after a network interruption', async () => {
    const upload = vi.spyOn(client, 'uploadPdf').mockRejectedValue(new client.ApiError('NETWORK_ERROR', 'Connection interrupted.'));
    await show(); fill(); fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls[0][3]).toBe(upload.mock.calls[1][3]);
  });
  it('starts a new attempt only after a confirmed terminal failure', async () => {
    const upload = vi.spyOn(client, 'uploadPdf').mockRejectedValueOnce(
      new client.ApiError('STORAGE_FAILURE', 'Storage unavailable.', { retry_with_new_key: 'true' }),
    ).mockResolvedValue({ document_id: 'doc-1', version_id: 'version-1', job_id: 'job-1', status: 'QUEUED' });
    await show(); fill(); fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await screen.findByRole('link', { name: 'Open uploaded document' });
    expect(upload.mock.calls[0][3]).not.toBe(upload.mock.calls[1][3]);
  });
  it('shows the current status when replaying a cancelled upload', async () => {
    vi.spyOn(client, 'uploadPdf').mockResolvedValue({
      document_id: 'doc-1', version_id: 'version-1', job_id: 'job-1', status: 'CANCELLED', replayed: true,
    });
    await show(); fill(); fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await screen.findByRole('link', { name: 'Open uploaded document' });
    expect(screen.getByText('cancelled')).toBeVisible();
    expect(screen.queryByText('Queued ·')).not.toBeInTheDocument();
  });
  it('does not offer upload controls to a reader', async () => {
    reader = true; uploaded = true; await show();
    expect(screen.queryByRole('button', { name: 'Upload documents' })).not.toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Synthetic reference' })).toBeVisible();
  });
  it('shows durable job state and ordered history in Operations', async () => {
    await show('/operations');
    expect(await screen.findByText('correlation-1')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel job' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect history' }));
    expect(await screen.findByRole('heading', { name: 'Ingestion history' })).toBeVisible();
    expect(await screen.findByText('validating')).toBeVisible();
  });
  it('shows original version metadata without fabricated parsed content', async () => {
    await show('/documents/doc-1');
    expect(await screen.findByText('SHA-256: abc123')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Download original' })).toBeEnabled();
    expect(screen.queryByText('Chunk count')).not.toBeInTheDocument();
  });
});
