import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, ApiError, askWithProgress } from '../../api/client';
import { Icon } from '../navigation/icons';
import { AccessGate, useSession } from '../library/Session';
import { Answer } from './Answer';
import { BUSY_LABEL, ProgressStepper, runningStage } from './ProgressStepper';
import { Timing } from './Timing';
import type {
  AskResponse, ConversationTurnView, ConversationView, StageEvent,
} from '../../types/retrieval';

/**
 * The Ask page: one conversation, read top to bottom.
 *
 * It used to render the whole conversation index above the chat, so the composer sat below a list
 * that grew without bound and the live progress landed off-screen on a workspace with any history.
 * The index now lives in the rail; this page shows the conversation you are in and nothing else.
 *
 * It still holds no part of the answer rule: it does not know what SUFFICIENT means, cannot see a
 * draft, and has no branch that could display one. The server returns an answer only behind an M8
 * PASS, and the response contract makes any other shape unconstructable.
 */
export function Ask() {
  return <AccessGate><Conversation /></AccessGate>;
}

function Conversation() {
  const { token, conversation: held, setConversation } = useSession();
  const queries = useQueryClient();
  const [question, setQuestion] = useState('');
  // The open conversation is held in two places, and neither of them is this component. The
  // session outlives navigation — the rail links to a bare /ask — and the address carries it too,
  // so a reload or a pasted link reopens the same conversation. Only the id is held either way;
  // the turns are always read from the server.
  const [params, setParams] = useSearchParams();
  const named = params.get('conversation');
  const conversationId = named ?? held;
  // One key per submission. A retry of the same submission returns the stored turn instead of
  // spending another provider call.
  const key = useRef<string>(crypto.randomUUID());
  const [stages, setStages] = useState<StageEvent[]>([]);
  const foot = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);

  // Reconcile the address and the session once, on arrival, and let `open` be authoritative from
  // then on. Reconciling on every render would race New conversation: the session clears, the
  // address has not caught up, and the stale address puts the conversation straight back.
  const reconciled = useRef(false);
  useEffect(() => {
    if (reconciled.current) return;
    reconciled.current = true;
    if (named && named !== held) setConversation(named);
    else if (!named && held) {
      const next = new URLSearchParams(params);
      next.set('conversation', held);
      setParams(next, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function open(id: string | null) {
    setConversation(id);
    const next = new URLSearchParams(params);
    if (id) next.set('conversation', id); else next.delete('conversation');
    setParams(next, { replace: !id });
  }

  const ask = useMutation({
    mutationFn: (text: string) => askWithProgress<AskResponse>(
      token,
      '/ask',
      // The question, an optional conversation to continue, and an idempotency key. Nothing that
      // could assert what the answer is or whether it was verified.
      { question: text, conversation_id: conversationId, idempotency_key: key.current },
      event => setStages(current => [...current, event as StageEvent]),
    ),
    onSuccess: result => {
      open(result.conversation_id);
      setQuestion('');
      void queries.invalidateQueries({ queryKey: ['conversations'] });
      void queries.invalidateQueries({ queryKey: ['conversation', result.conversation_id] });
    },
  });

  const history = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => api<ConversationView>(token, `/conversations/${conversationId}`),
    enabled: Boolean(conversationId) && !ask.isPending,
  });

  const result = ask.data;
  const error = ask.error as ApiError | null;
  const turns = history.data?.turns ?? [];
  // The turn just answered is rendered from the response and filtered out of the reloaded history,
  // because rendering it from both replaces the node the moment the reload lands — a flicker, and
  // briefly a different element under the reader's eyes.
  const earlier = turns.filter(turn => turn.turn_id !== result?.turn_id);
  const stage = runningStage(stages);
  const busy = stage ? BUSY_LABEL[stage] : 'Checking evidence…';
  const empty = !earlier.length && !result && !ask.isPending && !error;

  // Keep the newest exchange in view as it grows. The composer keeps focus; only the thread moves.
  // Optional-called because not every environment implements it, and a missing convenience must
  // never take the page down with it.
  useEffect(() => {
    foot.current?.scrollIntoView?.({ block: 'end', behavior: 'smooth' });
  }, [earlier.length, result?.turn_id, ask.isPending]);

  // The composer starts at about two lines and grows with the text to the ceiling the stylesheet
  // sets, after which it scrolls. Measured from the element rather than counted from the string,
  // because wrapping depends on the rendered width.
  useEffect(() => {
    const node = field.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${node.scrollHeight}px`;
  }, [question]);

  // Enter sends; Shift+Enter is a newline. The button remains the primary, labelled way to submit.
  function shortcut(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit(event);
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    // One request at a time: a second submission would spend another provider call on a question
    // already being answered, and the first request's idempotency key would no longer protect it.
    if (!question.trim() || ask.isPending) return;
    key.current = crypto.randomUUID();
    setStages([]);
    ask.mutate(question.trim());
  }

  return <div className={`chat${empty ? ' chat-idle' : ''}`}>
    {/* The safety meaning is unchanged and the full wording is one click away. What changed is
        the footprint: as a full-width card it was taking roughly a third of the first screen,
        above the answer, on every single visit. */}
    <div className="scope-notice" role="note">
      <Icon name="info" small />
      <details className="scope-text">
        <summary>Educational reference only — not for patient-specific medical advice.</summary>
        <p>This workspace answers only from the sources you indexed. Every statement in an answer
          is checked against the sources cited beside it. It is not medical advice, it is not for
          a specific patient, and it abstains rather than guessing.</p>
      </details>
    </div>

    <div className="chat-thread">
      {empty && <section className="chat-empty">
        <h1>Ask your indexed medical sources</h1>
        {/* No sample questions: a fixed list names one book's contents in every workspace, and
            reads as though the product were built around that book. Suggestions, if they come
            back, must be drawn from the workspace's own retrieval-ready sources. */}
        <p className="intro">
          Ask a question about the documents available in this workspace. Answers are released
          only when supported by traceable evidence; a question the indexed documents do not cover
          is refused rather than filled in.
        </p>
        <p className="muted">
          Ask about a concept, compare information across sources, or summarize a topic covered by
          your indexed material.
        </p>
      </section>}

      {history.isPending && conversationId && !result &&
        <p className="muted">Loading this conversation…</p>}

      {earlier.map(turn => <article className="turn" key={turn.turn_id}>
        <Question text={turn.question} />
        <Answer result={turn} />
      </article>)}

      {ask.isPending && <article className="turn">
        <Question text={question} />
        <ProgressStepper events={stages} active outcome={null} />
      </article>}

      {/* A finished request puts the answer first. The stepper stays where it was while the
          request ran — under the question — but once it settles it is a record of how the answer
          was produced, so it belongs after the answer rather than above it, where its outcome
          line was repeating the answer's own. */}
      {result && <article className="turn">
        <Question text={result.question} />
        <Answer result={result} />
        <ProgressStepper events={stages} active={false} outcome={result.outcome}
          reasons={result.reason_codes} />
        <Timing stages={result.stages} />
      </article>}

      {error && !result && <article className="turn">
        <Question text={question} />
        <section className="answer answer-exception outcome outcome-error">
          <p className="eyebrow outcome-line">
            <span className="outcome-mark" data-mark="failed" aria-hidden="true">
              <Icon name="failed" small />
            </span>
            Technical failure
          </p>
          <h2>The request could not be completed</h2>
          <p role="alert" className="error">{error.message}</p>
          <p>This is a technical failure, not a statement about the evidence. You can try again.</p>
        </section>
      </article>}

      <div ref={foot} />
    </div>

    <form onSubmit={submit} className="composer">
      <div className="composer-box">
        <label htmlFor="question" className="visually-hidden">Your educational medical question</label>
        <textarea id="question" ref={field} rows={2} value={question} maxLength={2000}
          aria-describedby="composer-note"
          onChange={event => setQuestion(event.target.value)}
          onKeyDown={shortcut}
          placeholder="Ask a question about your source material…" />
        <div className="composer-actions">
          <span className="composer-note" id="composer-note">
            Every claim is checked against a traceable source.
          </span>
          {/* The guard against a second submission is the same one as before: a request already
              in flight holds the idempotency key, and starting another would spend a second
              provider call on a question that is already being answered. */}
          <button type="submit" className="composer-submit"
            disabled={ask.isPending || !question.trim()}
            aria-describedby={ask.isPending ? 'progress-heading' : undefined}>
            {ask.isPending ? busy : <>Ask with evidence<Icon name="chevron-right" small /></>}
          </button>
        </div>
      </div>
    </form>
  </div>;
}

function Question({ text }: { text: string }) {
  return <div className="bubble-user">
    <p className="visually-hidden">You asked:</p>
    <p>{text}</p>
  </div>;
}

export type { ConversationTurnView };
