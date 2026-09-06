import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi, afterEach } from 'vitest';
import { App } from './App';

function show(path: string) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
  </QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());
describe('M0 workspace', () => {
  it('does not offer medical generation without an implemented evidence pipeline', () => {
    show('/ask');
    expect(screen.getByRole('heading', { name: 'Answering is not available' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
    expect(screen.getByLabelText('Your educational medical question')).toBeDisabled();
  });
  it('protects document workflows with an access gate', () => {
    show('/documents/example');
    expect(screen.getByRole('heading', { name: 'Document details' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Development workspace access' })).toBeVisible();
  });
  it('shows dependency failure from a 503 response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: 'not_ready', dependencies: { postgres: false },
    }), { status: 503 })));
    show('/operations');
    expect(await screen.findByRole('heading', { name: 'Infrastructure not ready' })).toBeVisible();
    expect(screen.getByText('Unavailable')).toBeVisible();
  });
  it('shows API errors rather than fabricated health', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    show('/operations');
    expect(await screen.findByRole('alert')).toHaveTextContent('API is unavailable');
  });
});
