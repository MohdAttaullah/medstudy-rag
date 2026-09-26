import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, describe, it, vi } from 'vitest';
import { Answer } from './Answer';
import { AccessGate, SessionProvider } from '../library/Session';
import type { AskCitation, AskFigure } from '../../types/retrieval';

/**
 * Source figures beside an answer.
 *
 * What must hold in the interface: a figure is presented as source material and never as the
 * reason the answer is verified. It says it was not interpreted, it says which cited evidence
 * linked it, and its image comes from the authorized document route rather than an object-store
 * URL. What the server did not link, the page does not invent — the client has no figure logic of
 * its own, which is itself the safety property.
 */

const citation: AskCitation = {
  citation_id: 'ct1', ordinal: 1, document_id: 'd1', document_version_id: 'v1',
  parse_run_id: 'pr1', chunk_run_id: 'cr1', document_title: 'Cerebellum and Fourth Ventricle',
  source_type: 'TEXTBOOK', authority_level: 'REFERENCE', chunk_type: 'TEXT_CHILD', pages: [1],
  spans: [{ element_id: 'e1', page: 1, start: 0, end: 20, role: 'PRIMARY', bbox: [1, 2, 3, 4] }],
  artifacts: [],
  cited_text: 'The tentorial surface faces and conforms to the lower surface of the tentorium ( Figs. 1.2-1.4 ).',
};

const figure: AskFigure = {
  figure_id: 'fg1', document_id: 'd1', document_version_id: 'v1', parse_run_id: 'pr1',
  document_title: 'Cerebellum and Fourth Ventricle', page: 2, label: '1.2',
  caption: 'FIGURE 1.2. Tentorial, suboccipital and petrosal surfaces.',
  linked_by: 'CITED_TEXT_REFERENCE', citation_ids: ['ct1'],
};

function verified(figures: AskFigure[]) {
  return {
    outcome: 'VERIFIED' as const, verified: true,
    answer: 'The tentorial surface faces and conforms to the lower surface of the tentorium.',
    message: 'Every statement below was checked against the sources cited with it.',
    reason_codes: [], citations: [citation],
    sources: [{
      document_id: 'd1', document_version_id: 'v1', parse_run_id: 'pr1',
      title: 'Cerebellum and Fourth Ventricle', source_type: 'TEXTBOOK',
      authority_level: 'REFERENCE', pages: [1], citation_ids: ['ct1'],
    }],
    figures,
  };
}

/** Requests the page made, so the authorization on the image can be asserted, not assumed. */
let requests: { url: string; headers: Record<string, string> }[] = [];

beforeEach(() => {
  requests = [];
  // jsdom implements neither of these; the component only needs them to exist.
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: () => 'blob:figure-1',
    revokeObjectURL: () => undefined,
  }));
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    requests.push({ url, headers });
    if (url.includes('/auth/me')) {
      return new Response(JSON.stringify({
        role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development',
        permissions: ['ask:submit', 'document:read'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (url.endsWith('/image')) {
      return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }), {
        status: 200, headers: { 'Content-Type': 'image/png' },
      });
    }
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

/** Signed in, because the figure route is authorized and the token comes from the session. */
async function show(figures: AskFigure[]) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>
      <SessionProvider>
        <AccessGate><Answer result={verified(figures)} /></AccessGate>
      </SessionProvider>
    </MemoryRouter>
  </QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByRole('heading', { name: 'Answer' });
}

describe('a figure the server linked', () => {
  it('shows it as source material, with its own caption and page', async () => {
    await show([figure]);
    const card = screen.getByRole('heading', { name: 'Figure 1.2' }).closest('article')!;
    expect(within(card).getByText(/page 2/)).toBeInTheDocument();
    expect(within(card).getByText(/FIGURE 1.2\. Tentorial, suboccipital/)).toBeInTheDocument();
  });

  it('says plainly that nothing read the picture', async () => {
    await show([figure]);
    expect(screen.getByText(/Source figure — not interpreted by AI/)).toBeInTheDocument();
    // And why it is here, so the link can be checked rather than trusted.
    expect(screen.getByText(/The cited source text refers to this figure/)).toBeInTheDocument();
  });

  it('names the other link when the citation is itself the figure', async () => {
    await show([{ ...figure, linked_by: 'CITED_EVIDENCE' }]);
    expect(screen.getByText(/The cited evidence is this figure/)).toBeInTheDocument();
  });

  it('keeps figures out of the evidence section', async () => {
    await show([figure]);
    // Sources and Citations are the answer's evidence; figures are a separate, later heading.
    const headings = screen.getAllByRole('heading').map(node => node.textContent);
    expect(headings).toContain('Citations');
    expect(headings).toContain('Related source figures');
    expect(headings.indexOf('Related source figures')).toBeGreaterThan(headings.indexOf('Citations'));
    expect(screen.getByText(/They are source material, not evidence/)).toBeInTheDocument();
  });

  it('fetches the image from the authorized document route, never an object-store key', async () => {
    await show([figure]);
    const image = await screen.findByRole('img');
    // Rendered from the fetched bytes, because an <img src> cannot carry a bearer token.
    expect(image).toHaveAttribute('src', 'blob:figure-1');
    const fetched = requests.find(request => request.url.endsWith('/image'))!;
    expect(fetched.url).toBe('/api/v1/documents/d1/versions/v1/parse-runs/pr1/figures/fg1/image');
    expect(fetched.headers.Authorization).toBe('Bearer test-key');
    // No bucket, no object key, no signed storage URL anywhere on the page. (The API path does
    // contain "documents/", which is the route, not a key — the object-store shapes are what
    // must be absent: a bucket name, a storage host, a signature, or a stored file path.)
    const markup = document.body.innerHTML.toLowerCase();
    for (const leak of ['minio', 's3.amazonaws', 'medical-rag-originals', 'x-amz', '/original/', '.pdf']) {
      expect(markup).not.toContain(leak);
    }
    // Nothing on the page links straight at the API or at storage: the bytes were fetched.
    expect(document.querySelectorAll('a[href^="/api"]')).toHaveLength(0);
  });

  it('describes the image by the source caption rather than by its content', async () => {
    await show([figure]);
    const alt = (await screen.findByRole('img')).getAttribute('alt') ?? '';
    expect(alt).toContain('Source figure as printed');
    expect(alt).toContain('FIGURE 1.2.');
  });

  it('offers keyboard-reachable ways to open the image and the page it came from', async () => {
    await show([figure]);
    const full = await screen.findByRole('link', { name: 'Open full image' });
    expect(full).toHaveAttribute('href', 'blob:figure-1');
    // The thumbnail is a link too, so it is reachable without a pointer.
    const thumb = (await screen.findByRole('img')).closest('a')!;
    expect(thumb).toHaveAttribute('href');
    expect(screen.getAllByRole('link', { name: 'Open source page' }).length).toBeGreaterThan(0);
  });

  it('shows an untitled figure without inventing a title for it', async () => {
    await show([{ ...figure, caption: null, label: null, linked_by: 'CITED_EVIDENCE' }]);
    expect(screen.getByRole('heading', { name: 'Source figure' })).toBeInTheDocument();
    expect((await screen.findByRole('img')).getAttribute('alt')).toContain('untitled in the source');
  });
});

describe('what the page will not do', () => {
  it('shows no figure section when the server linked none', async () => {
    await show([]);
    expect(screen.queryByRole('heading', { name: 'Related source figures' })).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
    // And no image was requested, because there was nothing to request.
    expect(requests.filter(request => request.url.endsWith('/image'))).toHaveLength(0);
  });

  it('says so plainly when the stored image cannot be loaded', async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/auth/me')) {
        return new Response(JSON.stringify({
          role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development',
          permissions: ['ask:submit', 'document:read'],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 404 });
    });
    await show([figure]);
    expect(await screen.findByText(/The stored image could not be loaded/)).toBeInTheDocument();
    // The provenance link survives on the figure card, so it is still reachable in the document.
    const card = document.querySelector('.figure-card')!;
    expect(within(card as HTMLElement).getByRole('link', { name: 'Open source page' }))
      .toBeInTheDocument();
    // Nothing offers a full image that does not exist.
    expect(within(card as HTMLElement).queryByRole('link', { name: 'Open full image' })).toBeNull();
  });

  it('shows no figure on an outcome that released no answer', () => {
    render(<MemoryRouter><Answer result={{
      outcome: 'UNVERIFIED', verified: false, answer: null,
      message: 'Evidence was found, but a supported answer could not be verified against it.',
      reason_codes: ['SEMANTICALLY_UNSUPPORTED'], citations: [], sources: [], figures: [],
    }} /></MemoryRouter>);
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Related source figures' })).toBeNull();
  });

  it('never presents a figure as a claim or a citation', async () => {
    await show([figure]);
    // The claim list and the citation list are built from citations; a figure is in neither.
    const cards = screen.getAllByText(/SOURCE \[/);
    expect(cards).toHaveLength(1);
    expect(screen.getByText(/Each card is the exact source text this answer was checked against/))
      .toBeInTheDocument();
  });
});
