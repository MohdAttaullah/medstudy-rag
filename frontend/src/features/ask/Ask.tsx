import { useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { Answer } from './Answer';
import type { AskResponse, ConversationView } from '../../types/retrieval';

// Bounded stage names only. The user sees where the request is, never a lane score, a draft token
// or anything that would let an unverified statement appear before verification finishes.
const STAGES = [
  'Searching sources',
  'Reranking evidence',
  'Checking evidence sufficiency',
  'Drafting from evidence',
  'Verifying claims',
];

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
  const { token } = useSession();
  const [question, setQuestion] = useState('');
  const [conversationId, setConversationId] = useState<string | null>(null);
  // One key per submission. A retry of the same submission returns the stored turn instead of
  // spending another provider call.
  const key = useRef<string>(crypto.randomUUID());

  const ask = useMutation({
    mutationFn: (text: string) => api<AskResponse>(token, '/ask', {
      method: 'POST',
      // The question, an optional conversation to continue, and an idempotency key. Nothing that
      // could assert what the answer is or whether it was verified.
      body: JSON.stringify({
        question: text,
        conversation_id: conversationId,
        idempotency_key: key.current,
      }),
    }),
    onSuccess: result => setConversationId(result.conversation_id),
  });

  const history = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => api<ConversationView>(token, `/conversations/${conversationId}`),
    enabled: Boolean(conversationId) && !ask.isPending,
  });

  const error = ask.error as ApiError | null;
  const result = ask.data;

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!question.trim() || ask.isPending) return;
    key.current = crypto.randomUUID();
    ask.mutate(question.trim());
  }

  return <>
    <section className="notice" aria-labelledby="ask-scope">
      <span className="status-dot" aria-hidden="true" />
      <div><h2 id="ask-scope">Educational use only</h2>
        <p>This workspace answers from the sources you indexed. It is not medical advice, it is not
          for a specific patient, and it abstains rather than guessing.</p></div>
    </section>

    <form onSubmit={submit}>
      <label htmlFor="question">Your educational medical question</label>
      <textarea id="question" rows={4} value={question} maxLength={2000}
        onChange={event => setQuestion(event.target.value)}
        placeholder="Ask a question about your source material…" />
      <div className="question-footer">
        <span>Every claim is checked against a traceable source.</span>
        <button type="submit" disabled={ask.isPending || !question.trim()}>
          {ask.isPending ? 'Checking evidence…' : 'Ask with evidence'}</button>
      </div>
    </form>

    {ask.isPending && <section className="panel" aria-live="polite">
      <h2>Working through the evidence</h2>
      <ol className="service-list">{STAGES.map(stage => <li key={stage}><span>{stage}</span></li>)}</ol>
      <p>No answer text is shown until claim verification finishes.</p>
    </section>}

    {error && !result && <section className="panel">
      <p className="eyebrow">SERVICE PROBLEM</p><h2>The request could not be completed</h2>
      <p role="alert" className="error">{error.message}</p>
      <p>This is a technical failure, not a statement about the evidence. You can try again.</p>
    </section>}

    {result && <Answer result={result} />}

    {result && <details><summary>Timing</summary>
      <dl className="detail-grid">{result.stages.map(stage =>
        <div key={stage.stage}><dt>{stage.stage}</dt><dd>{stage.duration_ms.toFixed(0)} ms</dd></div>)}</dl>
    </details>}

    {history.data && history.data.turns.length > 1 && <section className="panel">
      <h2>Earlier in this conversation</h2>
      {history.data.turns.slice(0, -1).reverse().map(turn => <article className="version-card" key={turn.turn_id}>
        <h3>{turn.question}</h3>
        <p>{turn.verified ? 'Answered and verified' : turn.message}</p>
        {turn.verified && <pre className="chunk-preview">{turn.answer}</pre>}
      </article>)}
    </section>}
  </>;
}
