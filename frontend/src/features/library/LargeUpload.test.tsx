import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import * as client from '../../api/client';

const LIMIT_MIB = 512;
const LIMIT = LIMIT_MIB * 1024 * 1024;

function page(items: unknown[]) { return { items, total: items.length, offset: 0, limit: 20 }; }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.includes('/auth/me')
      ? { user_id: 'user-1', display_name: 'Local tester', role: 'admin',
          permissions: ['document:read', 'document:upload', 'document:manage', 'ingestion:read'] }
      : url.includes('/uploads/limits')
        ? { max_upload_bytes: LIMIT, max_upload_mib: LIMIT_MIB,
            allowed_mime_types: ['application/pdf'], files_per_request: 1 }
        : url.includes('/health/ready') ? { status: 'ready', dependencies: { postgres: true } }
          : page([]);
    return new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function open() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/library']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'local-test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  // Signed in: the header offers the account control, which exists only once a principal is held.
  // This used to wait for the principal's name, which the content area printed above every page;
  // that line now lives inside the account menu in the header.
  await screen.findByRole('button', { name: 'Account and workspace' });
}

/** A File whose reported size we control without allocating that many bytes. */
function sized(name: string, bytes: number) {
  const file = new File(['%PDF-1.7 stub'], name, { type: 'application/pdf' });
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

function choose(file: File) {
  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Medical textbook' } });
  fireEvent.change(screen.getByLabelText('Choose PDF files or drag them here'), { target: { files: [file] } });
}

describe('large PDF upload', () => {
  it('shows the effective limit fetched from the server, not a hardcoded copy', async () => {
    await open();
    await screen.findByText(/Maximum file size: 512 MiB/);
    // And says how multiple selections are transmitted, so the limit is not misread as a batch total.
    expect(screen.getByText(/Files upload one at a time/)).toBeInTheDocument();
  });

  it('refuses an oversized PDF before transferring a single byte', async () => {
    const upload = vi.spyOn(client, 'uploadPdf');
    await open();
    await screen.findByText(/Maximum file size: 512 MiB/);
    choose(sized('huge-textbook.pdf', LIMIT + 1));
    fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/exceeds the maximum supported file size of 512 MiB/);
    expect(upload).not.toHaveBeenCalled();
  });

  it('accepts a large PDF that is within the limit', async () => {
    const upload = vi.spyOn(client, 'uploadPdf').mockResolvedValue({
      document_id: 'doc-1', version_id: 'version-1', job_id: 'job-1', status: 'QUEUED', replayed: false,
    });
    await open();
    await screen.findByText(/Maximum file size: 512 MiB/);
    // 153 MiB: the real textbook the previous 128 MiB limit rejected.
    choose(sized('medical-microbiology.pdf', 153 * 1024 * 1024));
    fireEvent.click(screen.getByRole('button', { name: 'Upload documents' }));
    await waitFor(() => expect(upload).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports a proxy 413 as a size problem rather than "Invalid server response"', () => {
    // nginx answers an oversized body with its own HTML page, so there is no JSON to decode.
    const error = client.nonJsonError(413);
    expect(error.code).toBe('UPLOAD_FILE_TOO_LARGE');
    expect(error.message).toMatch(/larger than the server accepts/);
    expect(error.message).not.toMatch(/Invalid server response/);
  });

  it('still reports other non-JSON responses without leaking proxy internals', () => {
    expect(client.nonJsonError(502).code).toBe('SERVICE_UNAVAILABLE');
    expect(client.nonJsonError(500).message).not.toMatch(/nginx|upstream/i);
  });
});
