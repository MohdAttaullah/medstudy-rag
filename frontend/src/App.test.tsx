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
  it('gates answering behind access and shows no answer before one is produced', () => {
    // Until M9 this asserted that answering was unavailable at all. The verified pipeline now
    // exists, so the invariant that replaces it is narrower and still the important one: the page
    // requires access, and nothing resembling an answer is on screen before a question is asked.
    // The marketing heading it used to check went when Ask became a conversation surface; the
    // gate and the absence of an answer are what mattered, and both are asserted here.
    show('/ask');
    expect(screen.getByRole('heading', { name: 'Development workspace access' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Answer' })).toBeNull();
    expect(screen.queryByLabelText('Your educational medical question')).toBeNull();
    // Nor is there a way to ask before signing in.
    expect(screen.queryByRole('button', { name: 'Ask with evidence' })).toBeNull();
    // A signed-out visitor sees no conversation of anyone's.
    expect(screen.queryByRole('navigation', { name: 'Recent conversations' })).toBeNull();
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
