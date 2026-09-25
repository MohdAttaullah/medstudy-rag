import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { stageStates, runningStage } from './ProgressStepper';
import type { StageEvent } from '../../types/retrieval';

/**
 * Live stage progress, driven by the server.
 *
 * The page used to show a fixed list of four stage names for the whole request, so a reader could
 * not tell which stage was running, which had finished, or whether anything was still happening.
 * What is established here is that every state shown comes from an event the server sent, that the
 * page invents nothing while it waits, and that a stage the pipeline skipped is shown as skipped
 * rather than as pending forever.
 */

const STAGES = [
  'PREPARING', 'RETRIEVAL', 'RERANK', 'EVIDENCE', 'GENERATION', 'VERIFICATION', 'FINALIZE',
] as const;

function event(stage: string, state: string, sequence: number): StageEvent {
  return {
    request_id: 'r1', stage: stage as StageEvent['stage'], state: state as StageEvent['state'],
    sequence, started_at: state === 'RUNNING' ? '2026-09-26T10:00:00+00:00' : null,
    completed_at: state === 'RUNNING' ? null : '2026-09-26T10:00:01+00:00',
    elapsed_ms: state === 'RUNNING' ? null : 12.5,
  };
}

function answer(outcome = 'VERIFIED') {
  const verified = outcome === 'VERIFIED';
  return {
    correlation_id: 'r1', conversation_id: 'cv1', turn_id: 't1', question: 'Q?',
    outcome, verified, answering_enabled: true,
    answer: verified ? 'A checked statement about the tentorial surface.' : null,
    claims: [], citations: [], sources: [],
    message: verified ? 'Every statement below was checked.' : 'No answer is shown.',
    reason_codes: verified ? [] : ['SEMANTICALLY_UNSUPPORTED'],
    stages: [{ stage: 'Verifying claims', duration_ms: 1600 }, { stage: 'Total', duration_ms: 12600 }],
    created_at: '2026-09-26T10:00:12+00:00',
  };
}

/** A body that yields SSE frames one at a time, so the interface is observed mid-request. */
function sse(frames: string[], hold?: { release: Promise<void> }) {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const [index, frame] of frames.entries()) {
        if (hold && index === frames.length - 1) await hold.release;
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
}

function frames(events: [string, string][], terminal: object, kind = 'result') {
  const out = events.map(([stage, state], index) =>
    `event: stage\ndata: ${JSON.stringify(event(stage, state, index + 1))}\n\n`);
  out.push(`event: ${kind}\ndata: ${JSON.stringify(terminal)}\n\n`);
  return out;
}

const COMPLETE: [string, string][] = STAGES.flatMap(stage => [
  [stage, 'RUNNING'], [stage, 'COMPLETED'],
] as [string, string][]);

let stream: { frames: string[]; hold?: { release: Promise<void> } };
let posted: { url: string; body: Record<string, unknown> }[] = [];

beforeEach(() => {
  posted = [];
  stream = { frames: frames(COMPLETE, answer()) };
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => 'key-1' });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') posted.push({ url, body: JSON.parse(String(init.body)) });
    if (url.includes('/auth/me')) {
      return new Response(JSON.stringify({
        role: 'admin', display_name: 'Tester', user_id: 'u1', auth_mode: 'development',
        permissions: ['ask:submit', 'conversation:read', 'retrieval:search'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    if (url.endsWith('/ask')) {
      return new Response(sse(stream.frames, stream.hold), {
        status: 200, headers: { 'Content-Type': 'text/event-stream' },
      });
    }
    const body = /\/conversations\/[^?]/.test(url)
      ? { conversation_id: 'cv1', title: 'Q?', created_at: '', updated_at: '', turns: [] }
      : { items: [], total: 0, offset: 0, limit: 25 };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }));
});
afterEach(() => vi.unstubAllGlobals());

async function ask(question = 'What is the tentorial surface?') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <MemoryRouter initialEntries={['/ask']}><App /></MemoryRouter>
  </QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value: 'test-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
  await screen.findByLabelText('Your educational medical question');
  fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
}

function step(title: string) {
  return screen.getByText(title).closest('li')!;
}

describe('stage state comes from the server', () => {
  it('shows every stage, in the order the server announced them', async () => {
    await ask();
    await screen.findByText('A checked statement about the tentorial surface.');
    const listed = screen.getAllByRole('listitem')
      .filter(item => item.className.includes('step'))
      .map(item => item.querySelector('.step-title')!.textContent);
    expect(listed).toEqual([
      'Preparing your question',
      'Searching indexed sources',
      'Selecting the most relevant passages',
      'Checking whether the evidence is sufficient',
      'Drafting an answer from the evidence',
      'Checking every material claim',
      'Preparing the verified answer and citations',
    ]);
  });

  it('marks a stage running while the server says it is, and completed once it says so', async () => {
    // Hold the terminal frame so the request is observed mid-flight, with VERIFICATION running.
    let release: () => void = () => undefined;
    stream = {
      frames: frames([...COMPLETE.slice(0, 11), ['VERIFICATION', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();

    await waitFor(() => expect(within(step('Checking every material claim')).getByText('Running now')).toBeInTheDocument());
    expect(within(step('Searching indexed sources')).getByText('Completed')).toBeInTheDocument();
    // A stage the server has not mentioned is not started — not guessed at.
    expect(within(step('Preparing the verified answer and citations')).getByText('Not started')).toBeInTheDocument();
    // The running stage explains itself, and says how long it has been running.
    expect(screen.getByText('Checking each material claim against its cited evidence.')).toBeInTheDocument();
    expect(screen.getByText(/Current stage: \d+\.\d s/)).toBeInTheDocument();

    release();
    await screen.findByText('A checked statement about the tentorial surface.');
  });

  it('renders each state with a word, not colour alone', async () => {
    stream = {
      frames: frames(
        [['PREPARING', 'RUNNING'], ['PREPARING', 'COMPLETED'], ['RETRIEVAL', 'SKIPPED'],
         ['RERANK', 'FAILED']],
        answer('FAILED'),
      ),
    };
    await ask();
    await waitFor(() => expect(within(step('Searching indexed sources')).getByText('Skipped')).toBeInTheDocument());
    expect(within(step('Preparing your question')).getByText('Completed')).toBeInTheDocument();
    expect(within(step('Selecting the most relevant passages')).getByText('Failed')).toBeInTheDocument();
    // The mark is decorative; the word carries the state.
    expect(step('Selecting the most relevant passages').querySelector('.step-mark'))
      .toHaveAttribute('aria-hidden', 'true');
  });

  it('announces the running stage politely, once', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([['PREPARING', 'COMPLETED'], ['RETRIEVAL', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();
    const live = await waitFor(() => {
      const region = document.querySelector('[aria-live="polite"].visually-hidden');
      expect(region?.textContent).toContain('Searching indexed sources');
      return region!;
    });
    expect(live.textContent).toContain('Running now');
    expect(document.querySelectorAll('[aria-live="polite"].visually-hidden')).toHaveLength(1);
    release();
  });
});

describe('the interface invents nothing', () => {
  it('shows no stage at all before the server has sent one', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();
    // The request is open and no event has arrived: nothing is claimed about any stage.
    expect(screen.queryByText('Running now')).not.toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    release();
  });

  it('derives states only from events, and treats the unmentioned as not started', () => {
    const states = stageStates([event('PREPARING', 'COMPLETED', 1), event('RETRIEVAL', 'RUNNING', 2)]);
    expect(states).toEqual({
      PREPARING: 'COMPLETED', RETRIEVAL: 'RUNNING', RERANK: 'PENDING', EVIDENCE: 'PENDING',
      GENERATION: 'PENDING', VERIFICATION: 'PENDING', FINALIZE: 'PENDING',
    });
  });

  it('orders by the server sequence, so a late frame cannot rewind a stage', () => {
    const out = stageStates([event('RETRIEVAL', 'COMPLETED', 2), event('RETRIEVAL', 'RUNNING', 1)]);
    expect(out.RETRIEVAL).toBe('COMPLETED');
    expect(runningStage([event('RETRIEVAL', 'COMPLETED', 2), event('RETRIEVAL', 'RUNNING', 1)])).toBeNull();
  });
});

describe('outcomes', () => {
  it('shows retrieval and generation skipped for an out-of-scope question', async () => {
    stream = {
      frames: frames(
        [['PREPARING', 'RUNNING'], ['PREPARING', 'COMPLETED'],
         ['RETRIEVAL', 'SKIPPED'], ['RERANK', 'SKIPPED'], ['EVIDENCE', 'SKIPPED'],
         ['GENERATION', 'SKIPPED'], ['VERIFICATION', 'SKIPPED'],
         ['FINALIZE', 'RUNNING'], ['FINALIZE', 'COMPLETED']],
        answer('OUT_OF_SCOPE'),
      ),
    };
    await ask('What antibiotic should I take for meningitis?');
    await screen.findByRole('heading', { name: 'Not a question this workspace answers' });
    for (const title of ['Searching indexed sources', 'Drafting an answer from the evidence',
                         'Checking every material claim']) {
      expect(within(step(title)).getByText('Skipped')).toBeInTheDocument();
    }
    expect(within(step('Preparing your question')).getByText('Completed')).toBeInTheDocument();
  });

  it('shows generation and verification skipped when the evidence was insufficient', async () => {
    stream = {
      frames: frames(
        [['PREPARING', 'COMPLETED'], ['RETRIEVAL', 'COMPLETED'], ['RERANK', 'COMPLETED'],
         ['EVIDENCE', 'COMPLETED'], ['GENERATION', 'SKIPPED'], ['VERIFICATION', 'SKIPPED'],
         ['FINALIZE', 'COMPLETED']],
        answer('INSUFFICIENT_EVIDENCE'),
      ),
    };
    await ask();
    await screen.findByRole('heading', { name: 'Insufficient evidence' });
    expect(within(step('Checking whether the evidence is sufficient')).getByText('Completed')).toBeInTheDocument();
    expect(within(step('Drafting an answer from the evidence')).getByText('Skipped')).toBeInTheDocument();
    expect(within(step('Checking every material claim')).getByText('Skipped')).toBeInTheDocument();
  });

  it('never shows draft text when verification did not release an answer', async () => {
    stream = { frames: frames(COMPLETE, answer('UNVERIFIED')) };
    await ask();
    await screen.findByRole('heading', { name: 'Could not verify an answer' });
    expect(within(step('Drafting an answer from the evidence')).getByText('Completed')).toBeInTheDocument();
    expect(within(step('Checking every material claim')).getByText('Completed')).toBeInTheDocument();
    expect(screen.queryByText('A checked statement about the tentorial surface.')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Answer' })).not.toBeInTheDocument();
  });

  it('marks the stage that broke when the request fails', async () => {
    stream = {
      frames: frames(
        [['PREPARING', 'COMPLETED'], ['RETRIEVAL', 'COMPLETED'], ['RERANK', 'COMPLETED'],
         ['EVIDENCE', 'COMPLETED'], ['GENERATION', 'RUNNING'], ['GENERATION', 'FAILED'],
         ['FINALIZE', 'COMPLETED']],
        answer('FAILED'),
      ),
    };
    await ask();
    await screen.findByRole('heading', { name: 'The answering service failed' });
    expect(within(step('Drafting an answer from the evidence')).getByText('Failed')).toBeInTheDocument();
    expect(step('Drafting an answer from the evidence').className).toContain('step-failed');
  });

  it('reports a typed error that arrives on the stream', async () => {
    stream = {
      frames: frames([], { error: { code: 'CONVERSATION_NOT_FOUND', message: 'No such conversation.', status: 404 } }, 'error'),
    };
    await ask();
    expect(await screen.findByText('No such conversation.')).toBeInTheDocument();
  });
});

describe('submission', () => {
  it('prevents a second submission while one is in flight', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([['RETRIEVAL', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();
    const button = await waitFor(() => {
      const found = screen.getByRole('button', { name: 'Searching sources…' });
      expect(found).toBeDisabled();
      return found;
    });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(posted.filter(call => call.url.endsWith('/ask'))).toHaveLength(1);
    release();
  });

  it('keeps the question visible and says which stage is running', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([['GENERATION', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask('What is the tentorial surface?');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Drafting…' })).toBeInTheDocument());
    expect(screen.getByLabelText('Your educational medical question')).toHaveValue('What is the tentorial surface?');
    release();
  });

  it('offers no cancel control, because the pipeline supports no cancellation', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([['RERANK', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Selecting passages…' })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /cancel|stop|abort/i })).not.toBeInTheDocument();
    release();
  });

  it('starts a clean timeline for a second question', async () => {
    await ask();
    await screen.findByText('A checked statement about the tentorial surface.');
    stream = {
      frames: frames([['PREPARING', 'RUNNING'], ['PREPARING', 'COMPLETED'], ['RETRIEVAL', 'RUNNING']], answer()),
      hold: { release: Promise.resolve() },
    };
    fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'And then?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask with evidence' }));
    // The stepper describes the new request: stages the first one completed are not carried over.
    await waitFor(() => expect(
      within(step('Selecting the most relevant passages')).getByText('Not started'),
    ).toBeInTheDocument());
  });
});

describe('navigation', () => {
  it('leaves no stale running stage after navigating away and back', async () => {
    let release: () => void = () => undefined;
    stream = {
      frames: frames([['RERANK', 'RUNNING']], answer()),
      hold: { release: new Promise<void>(resolve => { release = () => resolve(); }) },
    };
    await ask();
    await waitFor(() => expect(within(step('Selecting the most relevant passages')).getByText('Running now')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('link', { name: 'Library' }));
    await screen.findByRole('heading', { name: 'Your source documents' });
    fireEvent.click(screen.getByRole('link', { name: 'Ask' }));
    await screen.findByLabelText('Your educational medical question');

    // The request that was running belongs to a page that no longer exists; the returning page
    // claims nothing about it and reads the conversation from the server instead.
    expect(screen.queryByText('Running now')).not.toBeInTheDocument();
    expect(screen.queryByText(/Current stage:/)).not.toBeInTheDocument();
    // And the page is usable rather than stuck: the empty box disables the button, as it always
    // does, and typing a question enables it again.
    expect(screen.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Your educational medical question'), { target: { value: 'Again?' } });
    expect(screen.getByRole('button', { name: 'Ask with evidence' })).toBeEnabled();
    release();
  });
});
