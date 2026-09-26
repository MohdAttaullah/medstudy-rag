import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';

/**
 * The workspace rail.
 *
 * Three properties are worth pinning. Permissions decide what is listed, so a reader is not shown
 * four inspector pages they cannot open. Collapsing is a local view preference and provably
 * nothing else — it never reaches the server and cannot touch the conversation. And every control
 * keeps a name when the labels are hidden, because an icon-only rail is unusable to a screen
 * reader otherwise.
 */

const CONVERSATIONS = [
  { conversation_id: 'cv1', title: 'Tentorial surface', turn_count: 2, verified_turns: 2, created_at: '', updated_at: '' },
  { conversation_id: 'cv2', title: 'Cerebellar surfaces', turn_count: 1, verified_turns: 1, created_at: '', updated_at: '' },
];

let permissions: string[] = [];
let posted: string[] = [];

function mount(entry = '/ask') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[entry]}><App /></MemoryRouter>
  </QueryClientProvider>);
}

async function signIn(entry = '/ask') {
  mount(entry);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Your educational medical question');
}

function rail() {
  return within(screen.getByRole('complementary', { name: 'Workspace navigation' }));
}

beforeEach(() => {
  permissions = ['ask:submit', 'conversation:read', 'document:read', 'retrieval:search', 'audit:read'];
  posted = [];
  window.localStorage.clear();
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => 'key-1' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') posted.push(url);
    const body = url.includes('/auth/me')
      ? { role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development', permissions }
      : /\/conversations\/[^?]/.test(url)
        ? { conversation_id: 'cv1', title: 'Tentorial surface', created_at: '', updated_at: '', turns: [] }
        : url.includes('/conversations')
          ? { items: CONVERSATIONS, total: 2, offset: 0, limit: 50 }
          : { items: [], total: 0, offset: 0, limit: 25 };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe('what the rail lists', () => {
  it('puts the everyday pages first and folds engineering tools away', async () => {
    await signIn();
    for (const name of ['Ask', 'Library', 'Settings']) {
      expect(rail().getByRole('link', { name })).toBeInTheDocument();
    }
    // Advanced tools are behind one disclosure, closed until asked for.
    expect(rail().queryByRole('link', { name: 'Retrieval inspector' })).toBeNull();
    fireEvent.click(rail().getByRole('button', { name: /Advanced tools/ }));
    expect(rail().getByRole('link', { name: 'Retrieval inspector' })).toBeInTheDocument();
    expect(rail().getByRole('link', { name: 'Audit' })).toBeInTheDocument();
  });

  it('lists a tool only to a principal who may use it', async () => {
    permissions = ['ask:submit', 'conversation:read'];
    await signIn();
    // A reader has no diagnostics permission, so the group has nothing to show and is absent.
    expect(rail().queryByRole('button', { name: /Advanced tools/ })).toBeNull();
    expect(rail().getByRole('link', { name: 'Ask' })).toBeInTheDocument();
  });

  it('shows a curator only the tools their permissions cover', async () => {
    permissions = ['ask:submit', 'conversation:read', 'retrieval:search'];
    await signIn();
    fireEvent.click(rail().getByRole('button', { name: /Advanced tools/ }));
    expect(rail().getByRole('link', { name: 'Retrieval inspector' })).toBeInTheDocument();
    expect(rail().queryByRole('link', { name: 'Audit' })).toBeNull();
    expect(rail().queryByRole('link', { name: 'Operations' })).toBeNull();
  });

  it('shows recent conversations under a New conversation control', async () => {
    await signIn();
    expect(await rail().findByRole('link', { name: 'Tentorial surface' })).toBeInTheDocument();
    expect(rail().getByRole('link', { name: 'Cerebellar surfaces' })).toBeInTheDocument();
    expect(rail().getByRole('link', { name: 'New conversation' })).toBeInTheDocument();
  });

  it('marks the open conversation for assistive technology, not only by colour', async () => {
    await signIn('/ask?conversation=cv2');
    const open = await rail().findByRole('link', { name: 'Cerebellar surfaces' });
    expect(open).toHaveAttribute('aria-current', 'page');
    expect(rail().getByRole('link', { name: 'Tentorial surface' })).not.toHaveAttribute('aria-current');
  });

  it('shows no conversation list at all before sign-in', () => {
    mount();
    expect(screen.queryByRole('link', { name: 'Tentorial surface' })).toBeNull();
  });
});

describe('collapsing', () => {
  it('collapses to icons and keeps every control named', async () => {
    await signIn();
    const toggle = rail().getByRole('button', { name: 'Collapse sidebar' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);

    // Labels are visually hidden, so the names survive for a screen reader and the tooltip.
    expect(rail().getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument();
    expect(rail().getByRole('link', { name: 'Ask' })).toBeInTheDocument();
    expect(rail().getByRole('link', { name: 'New conversation' })).toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Workspace navigation' }).className)
      .toContain('sidebar-collapsed');
  });

  it('remembers the preference in this browser and nowhere else', async () => {
    await signIn();
    fireEvent.click(rail().getByRole('button', { name: 'Collapse sidebar' }));
    await waitFor(() => expect(window.localStorage.getItem('medrag.sidebar.collapsed')).toBe('true'));
    // A view preference is all it is: nothing was sent, and no conversation content was stored.
    expect(posted).toHaveLength(0);
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('Tentorial surface');
  });

  it('restores the remembered preference on the next visit', async () => {
    window.localStorage.setItem('medrag.sidebar.collapsed', 'true');
    await signIn();
    expect(rail().getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument();
  });

  it('does not disturb the open conversation', async () => {
    await signIn('/ask?conversation=cv1');
    fireEvent.click(rail().getByRole('button', { name: 'Collapse sidebar' }));
    fireEvent.click(rail().getByRole('button', { name: 'Expand sidebar' }));
    // Still the same conversation, still loaded from the server.
    expect(await rail().findByRole('link', { name: 'Tentorial surface' }))
      .toHaveAttribute('aria-current', 'page');
  });

  it('survives a browser that refuses storage', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    await signIn();
    expect(rail().getByRole('link', { name: 'Ask' })).toBeInTheDocument();
    getItem.mockRestore();
  });
});

describe('the drawer on a narrow screen', () => {
  it('opens from the header control and closes on the scrim', async () => {
    await signIn();
    const toggle = screen.getByRole('button', { name: 'Navigation' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('complementary', { name: 'Workspace navigation' }).className)
      .toContain('sidebar-open');

    fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }));
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'false'));
  });

  it('closes itself when a destination is chosen', async () => {
    await signIn();
    fireEvent.click(screen.getByRole('button', { name: 'Navigation' }));
    fireEvent.click(rail().getByRole('link', { name: 'Library' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Navigation' }))
      .toHaveAttribute('aria-expanded', 'false'));
  });
});
