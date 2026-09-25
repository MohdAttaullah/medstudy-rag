import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';

/**
 * A conversation must survive leaving the page.
 *
 * It did not. The open conversation lived in `useState` inside the routed Ask component, so going
 * to Library and back discarded it: the next question opened a second conversation, and the turns
 * the server had been storing all along looked lost. Nothing was ever missing from the database —
 * `GET /conversations` and `GET /conversations/{id}` already existed and simply were not called.
 */

const turn = (id: string, question: string) => ({
  turn_id: id, sequence_number: 1, question, outcome: 'VERIFIED', verified: true,
  answer: `Answer to ${question}`, message: 'Every statement below was checked.',
  reason_codes: [], citations: [], sources: [], created_at: '2026-09-24T00:00:00+00:00',
});

let listed = [
  { conversation_id: 'cv1', title: 'First question?', turn_count: 2, verified_turns: 2, created_at: '', updated_at: '' },
  { conversation_id: 'cv2', title: 'Second question?', turn_count: 1, verified_turns: 0, created_at: '', updated_at: '' },
];
const conversations: Record<string, ReturnType<typeof turn>[]> = {
  cv1: [turn('t1', 'First question?'), turn('t2', 'A follow up?')],
  cv2: [turn('t3', 'Second question?')],
};
let asked: Record<string, unknown> = {};
let requested: string[] = [];

beforeEach(() => {
  asked = {};
  requested = [];
  listed = [
    { conversation_id: 'cv1', title: 'First question?', turn_count: 2, verified_turns: 2, created_at: '', updated_at: '' },
    { conversation_id: 'cv2', title: 'Second question?', turn_count: 1, verified_turns: 0, created_at: '', updated_at: '' },
  ];
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => 'key-1' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    requested.push(url);
    let body: unknown = {};
    if (url.includes('/auth/me')) {
      body = {
        role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development',
        permissions: ['ask:submit', 'conversation:read', 'document:read', 'retrieval:search'],
      };
    } else if (url.includes('/ask')) {
      asked = JSON.parse(String(init?.body));
      const id = (asked.conversation_id as string) ?? 'cv3';
      conversations[id] = [...(conversations[id] ?? []), turn('t9', String(asked.question))];
      listed = [{ conversation_id: id, title: String(asked.question), turn_count: conversations[id].length, verified_turns: 1, created_at: '', updated_at: '' }, ...listed.filter(c => c.conversation_id !== id)];
      body = {
        correlation_id: 'x', conversation_id: id, turn_id: 't9', question: asked.question,
        outcome: 'VERIFIED', verified: true, answering_enabled: true,
        answer: `Answer to ${asked.question}`, claims: [], citations: [], sources: [],
        message: 'Every statement below was checked.', reason_codes: [],
        stages: [{ stage: 'Total', duration_ms: 1000 }], created_at: '',
      };
    } else if (/\/conversations\/[^?]/.test(url)) {
      const id = url.split('/conversations/')[1];
      body = { conversation_id: id, title: listed.find(c => c.conversation_id === id)?.title ?? id, created_at: '', updated_at: '', turns: conversations[id] ?? [] };
    } else if (url.includes('/conversations')) {
      body = { items: listed, total: listed.length, offset: 0, limit: 25 };
    } else if (url.includes('/documents') || url.includes('/ingestion/jobs')) {
      // The navigation test visits Library, which reads its own endpoints.
      body = { items: [], total: 0, offset: 0, limit: 25 };
    }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function workspace(entry = '/ask') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <MemoryRouter initialEntries={[entry]}><App /></MemoryRouter>
  </QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Your educational medical question');
}

it('lists the conversations this principal owns', async () => {
  await workspace();
  expect(await screen.findByRole('button', { name: 'First question?' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Second question?' })).toBeInTheDocument();
  // The client asks for its own conversations and sends no identity of its own.
  expect(requested.some(url => url.includes('/conversations?limit=25'))).toBe(true);
  expect(requested.every(url => !url.includes('tenant'))).toBe(true);
});

it('reloads the stored turns of a conversation from the server', async () => {
  await workspace();
  fireEvent.click(await screen.findByRole('button', { name: 'First question?' }));
  expect(await screen.findByText('Answer to First question?')).toBeInTheDocument();
  expect(await screen.findByText('Answer to A follow up?')).toBeInTheDocument();
});

it('keeps the conversation open across navigation away and back', async () => {
  await workspace();
  fireEvent.click(await screen.findByRole('button', { name: 'First question?' }));
  await screen.findByText('Answer to First question?');

  fireEvent.click(screen.getByRole('link', { name: 'Library' }));
  await screen.findByRole('heading', { name: "Your source documents" });
  fireEvent.click(screen.getByRole('link', { name: 'Ask' }));

  // The same conversation is still open, and its turns come back from the server.
  expect(await screen.findByText('Answer to First question?')).toBeInTheDocument();
});

it('opens a conversation named in the address, so a reload restores it', async () => {
  await workspace('/ask?conversation=cv2');
  expect(await screen.findByText('Answer to Second question?')).toBeInTheDocument();
});

it('continues the open conversation when another question is asked', async () => {
  await workspace('/ask?conversation=cv1');
  await screen.findByText('Answer to First question?');
  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'And then?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
  await waitFor(() => expect(asked.conversation_id).toBe('cv1'));
});

it('starts a different conversation when New conversation is chosen', async () => {
  await workspace('/ask?conversation=cv1');
  await screen.findByText('Answer to First question?');
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
  await waitFor(() => expect(screen.queryByText('Answer to First question?')).not.toBeInTheDocument());

  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'A brand new one?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
  await waitFor(() => expect(asked.conversation_id).toBeNull());
});

it('drops the conversation on sign-out so the next principal starts clean', async () => {
  await workspace('/ask?conversation=cv1');
  await screen.findByText('Answer to First question?');
  fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
  await screen.findByLabelText('Access key');
  // No conversation content survives on the sign-in screen.
  expect(screen.queryByText('Answer to First question?')).not.toBeInTheDocument();
});

it('shows the stage waterfall to a principal holding retrieval diagnostics', async () => {
  await workspace();
  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'How?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
  await screen.findByText('Answer to How?');
  fireEvent.click(screen.getByText('Timing'));
  expect(await screen.findByRole('row', { name: /Total/ })).toBeInTheDocument();
});

it('stores no conversation content in browser storage', async () => {
  await workspace('/ask?conversation=cv1');
  await screen.findByText('Answer to First question?');
  const stored = JSON.stringify({ ...localStorage, ...sessionStorage });
  expect(stored).not.toContain('Answer to First question?');
  expect(stored).not.toContain('First question?');
});
