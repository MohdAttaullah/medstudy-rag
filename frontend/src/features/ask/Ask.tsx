import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, ApiError, askWithProgress } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { Answer } from './Answer';
import { BUSY_LABEL, ProgressStepper, runningStage } from './ProgressStepper';
import { Timing } from './Timing';
import type {
  AskResponse, ConversationSummary, ConversationView, StageEvent,
} from '../../types/retrieval';
import type { Page } from '../../types/documents';

/**
 * The Ask page.
 *
 * It submits a question and renders whatever the server decided. It holds no part of the answer
 * rule: it does not know what SUFFICIENT means, cannot see a draft, and has no branch that could
 * display one. The server returns an answer only behind an M8 PASS, and the response contract
 * makes any other shape unconstructable — so there is nothing here to get wrong.
 */
export function Ask() {
  return <><p className="eyebrow">EDUCATIONAL KNOWLEDGE</p><h1>Evidence comes first.</h1>
    <p className="intro">
      Ask about your indexed source library. An answer appears only when every statement in it has
      been checked against the sources cited beside it.
    </p>
    <AccessGate><Conversation /></AccessGate></>;
}

function Conversation() {
  const { token, conversation: held, setConversation } = useSession();
  const queries = useQueryClient();
  const [question, setQuestion] = useState('');
  // The open conversation is held in two places, and neither of them is this component.
  //
  // It used to be `useState` here, which the router discards the moment the page unmounts: going
  // to Library and back started a new conversation over a history the server had been keeping all
  // along, and the turns looked lost. The session holds it across navigation — the sidebar link
  // goes to a bare /ask, so a search parameter alone would not survive the trip — and the address
  // carries it too, so a reload or a pasted link reopens the same conversation. Only the id is
  // held either way; the turns are read from the server.
  const [params, setParams] = useSearchParams();
  const named = params.get('conversation');
  const conversationId = named ?? held;
  // One key per submission. A retry of the same submission returns the stored turn instead of
  // spending another provider call.
  const key = useRef<string>(crypto.randomUUID());

  // Reconcile the address and the session once, on arrival at the page, and let `open` be
  // authoritative from then on. An address that names a conversation wins — that is a reload or a
  // pasted link. An address that names none adopts the held one rather than clearing it, because
  // the sidebar link goes to a bare /ask and arriving there is navigation, not a decision to
  // leave the conversation; New conversation is how you leave it. Reconciling on every render
  // instead would race that button: the session clears, the address has not caught up yet, and
  // the stale address puts the conversation straight back.
  const reconciled = useRef(false);
  useEffect(() => {
    if (reconciled.current) return;
    reconciled.current = true;
    if (named && named !== held) {
      setConversation(named);
    } else if (!named && held) {
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

  // Stage events for the request in flight. Cleared when a new one starts, so the stepper always
  // describes the request being watched and never a previous one.
  const [stages, setStages] = useState<StageEvent[]>([]);

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

  // Every conversation this principal owns. The server scopes the list to the authenticated
  // tenant and user; the client sends no identifier of its own and could not widen it if it tried.
  const conversations = useQuery({
    queryKey: ['conversations'],
    queryFn: () => api<Page<ConversationSummary>>(token, '/conversations?limit=25'),
    enabled: Boolean(token),
  });

  const history = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => api<ConversationView>(token, `/conversations/${conversationId}`),
    enabled: Boolean(conversationId) && !ask.isPending,
  });

  // What the button says while a request is open: the stage the server last announced.
  const stage = runningStage(stages);
  const busy = stage ? BUSY_LABEL[stage] : 'Checking evidence…';
  const error = ask.error as ApiError | null;
  const result = ask.data;
  // Both lists tolerate a response that carries neither field. A conversation index that fails to
  // load must not take the question box down with it.
  const listed = conversations.data?.items ?? [];
  const turns = history.data?.turns ?? [];
  // The turn just answered is rendered from the response and left alone; the reloaded copy of it
  // is filtered out of the history below. Rendering it from the response first and then from the
  // history would replace the node the moment the reload lands, which reads as a flicker and
  // means the answer a reader is looking at was briefly a different element.
  const earlier = [...turns].reverse().filter(turn => turn.turn_id !== result?.turn_id);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    // One request at a time. A second submission would spend another provider call on a question
    // already being answered, and the idempotency key of the first would no longer protect it.
    if (!question.trim() || ask.isPending) return;
    key.current = crypto.randomUUID();
    setStages([]);
    ask.mutate(question.trim());
  }

  return <>
    <section className="notice" aria-labelledby="ask-scope">
      <span className="status-dot" aria-hidden="true" />
      <div><h2 id="ask-scope">Educational use only</h2>
        <p>This workspace answers from the sources you indexed. It is not medical advice, it is not
          for a specific patient, and it abstains rather than guessing.</p></div>
    </section>

    <section className="panel" aria-labelledby="conversation-list">
      <div className="question-footer">
        <h2 id="conversation-list">Conversations</h2>
        <button type="button" className="secondary" onClick={() => open(null)}
          disabled={!conversationId}>New conversation</button>
      </div>
      {conversations.isPending && <p>Loading your conversations…</p>}
      {conversations.isError && <p role="alert">Your conversations could not be loaded.</p>}
      {conversations.isSuccess && !listed.length &&
        <p className="muted">No conversations yet. Your first question starts one.</p>}
      {!!listed.length && <ul className="service-list">
        {listed.map(item => <li key={item.conversation_id}>
          <button type="button" className="link"
            aria-current={item.conversation_id === conversationId ? 'true' : undefined}
            onClick={() => open(item.conversation_id)}>{item.title}</button>
          <span className="mono">{item.turn_count} turn{item.turn_count === 1 ? '' : 's'}</span>
        </li>)}
      </ul>}
    </section>

    <form onSubmit={submit}>
      <label htmlFor="question">Your educational medical question</label>
      <textarea id="question" rows={4} value={question} maxLength={2000}
        onChange={event => setQuestion(event.target.value)}
        placeholder="Ask a question about your source material…" />
      <div className="question-footer">
        <span>Every claim is checked against a traceable source.</span>
        <button type="submit" disabled={ask.isPending || !question.trim()}
          aria-describedby={ask.isPending ? 'progress-heading' : undefined}>
          {ask.isPending ? busy : 'Ask with evidence'}</button>
      </div>
    </form>

    <ProgressStepper events={stages} active={ask.isPending} outcome={result?.outcome} />

    {error && !result && <section className="panel">
      <p className="eyebrow">SERVICE PROBLEM</p><h2>The request could not be completed</h2>
      <p role="alert" className="error">{error.message}</p>
      <p>This is a technical failure, not a statement about the evidence. You can try again.</p>
    </section>}

    {result && <Answer result={result} />}
    {result && <Timing stages={result.stages} />}

    {history.isPending && conversationId && <p>Loading this conversation…</p>}
    {!!earlier.length && <section className="panel" aria-labelledby="conversation-turns">
      <h2 id="conversation-turns">{result ? 'Earlier in this conversation' : history.data?.title}</h2>
      {earlier.map(turn => <article className="version-card" key={turn.turn_id}>
        <h3>{turn.question}</h3>
        <Answer result={turn} />
      </article>)}
    </section>}
  </>;
}
