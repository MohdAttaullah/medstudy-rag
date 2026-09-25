import { useState } from 'react';
import { EvidenceInspector } from './EvidenceInspector';
import { DraftInspector } from './DraftInspector';
import { VerificationInspector } from './VerificationInspector';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import type { AnswerResponse, Candidate, DraftResponse, RetrievalStatus, SearchResponse, RerankedResponse } from '../../types/retrieval';

/**
 * What each mode actually runs, read from the endpoint it calls rather than from its name.
 *
 * DENSE_ONLY, BM25_ONLY and HYBRID_RRF post to /retrieval/search, which runs first-stage retrieval
 * and stops. RERANKED posts to /retrieval/rerank, adding the cross-encoder and evidence assembly.
 * GROUNDED_DRAFT posts to /retrieval/draft, which adds the sufficiency gate and one generator
 * call. VERIFIED_ANSWER posts to /retrieval/answer, the same pipeline Ask runs, including claim
 * verification. The last two call a provider; the first four do not.
 *
 * None of them writes anything. The mode is a field of one request, the endpoints resolve
 * configuration per request from the server's own settings, and no route here can change what a
 * later /ask does.
 */
const MODES = [
  ['HYBRID_RRF', 'Hybrid (RRF)', 'Dense + BM25 fused with Reciprocal Rank Fusion.', false],
  ['RERANKED', 'Hybrid + reranking + evidence',
    'Adds CrossEncoder reranking and EvidenceSet construction.', false],
  ['GROUNDED_DRAFT', 'Evidence gate + grounded draft (unverified)',
    'Adds the sufficiency gate and a grounded draft. Stops before claim verification, so nothing '
    + 'it shows has been checked.', true],
  ['VERIFIED_ANSWER', 'Full pipeline + claim verification',
    'The closest diagnostic equivalent of Ask: every stage, including claim verification.', true],
  ['DENSE_ONLY', 'Dense only', 'Semantic retrieval only.', false],
  ['BM25_ONLY', 'BM25 only', 'Keyword retrieval only.', false],
] as const;

/**
 * A developer and operator tool for inspecting first-stage retrieval.
 *
 * It is deliberately not the Ask page. It shows *evidence candidates* and the lane diagnostics
 * that explain their order. There is no generated answer here, no summary of the results, no
 * confidence and no hallucination score, because none of those exist at this milestone and a
 * control that displayed one would be inventing it.
 */
export function RetrievalInspector() {
  return <><p className="eyebrow">RETRIEVAL DIAGNOSTICS</p><h1>Retrieval inspector</h1>
    <p className="intro">
      Run a query against the indexed corpus and compare what each retrieval lane returns.
    </p>
    <AccessGate><Inspector /></AccessGate></>;
}

function Inspector() {
  const { token } = useSession();
  const [query, setQuery] = useState('');
  // Kept in the URL so leaving the page and coming back does not silently reset the selection.
  // It is a view preference and nothing more: it is read only when this page builds its own
  // request, and no other page reads it.
  const [params, setParams] = useSearchParams();
  const mode = MODES.some(([value]) => value === params.get('mode'))
    ? params.get('mode')! : 'HYBRID_RRF';
  const setMode = (next: string) => {
    const updated = new URLSearchParams(params);
    updated.set('mode', next);
    setParams(updated, { replace: true });
  };
  const [lane, setLane] = useState<'hybrid' | 'dense' | 'sparse' | 'reranked' | 'evidence' | 'sufficiency' | 'draft' | 'claims' | 'answer'>('hybrid');
  const [selected, setSelected] = useState<Candidate | null>(null);

  const status = useQuery({
    queryKey: ['retrieval-status'],
    queryFn: () => api<RetrievalStatus>(token, '/retrieval/status'),
  });

  const search = useMutation({
    mutationFn: (text: string) => api<SearchResponse | RerankedResponse | DraftResponse | AnswerResponse>(
      token,
      mode === 'VERIFIED_ANSWER' ? '/retrieval/answer'
        : mode === 'GROUNDED_DRAFT' ? '/retrieval/draft'
        : mode === 'RERANKED' ? '/retrieval/rerank' : '/retrieval/search',
      {
        method: 'POST',
        // The server owns the evidence, the policy, the provider and the model. The client sends a
        // question and optional filters, and nothing that could steer what an answer is grounded in.
        body: JSON.stringify({ query: text, mode: mode === 'HYBRID_RRF' || mode === 'DENSE_ONLY' || mode === 'BM25_ONLY' ? mode : 'HYBRID_RRF' }),
      },
    ),
    onSuccess: () => { setSelected(null); setLane('hybrid'); },
  });

  const reranked = search.data && 'evidence_set' in search.data ? search.data : undefined;
  const drafted = search.data && 'sufficiency' in search.data ? (search.data as DraftResponse) : undefined;
  const answered = search.data && 'verification' in search.data ? (search.data as AnswerResponse) : undefined;
  const result = reranked?.first_stage ?? (search.data as SearchResponse | undefined);
  const error = search.error as ApiError | null;

  return <>
    <section className="notice" aria-labelledby="retrieval-scope">
      <span className="status-dot" aria-hidden="true" />
      <div><h2 id="retrieval-scope">Retrieved evidence candidates</h2>
        <p>This page retrieves and ranks source passages. It does not answer medical questions,
          and no result here is a statement that the corpus contains an answer.</p></div>
    </section>

    {status.isPending && <p>Loading retrieval status…</p>}
    {status.isError && <p role="alert">Retrieval status unavailable.</p>}
    {status.data && <section className="panel"><h2>Corpus available to this account</h2>
      {status.data.corpus_error
        ? <p role="alert" className="error">
            The corpus cannot be searched: {status.data.corpus_error}.
          </p>
        : <dl className="detail-grid">
          <div><dt>Document versions</dt><dd>{status.data.document_versions}</dd></div>
          <div><dt>Dense index runs</dt><dd>{status.data.dense_index_runs}</dd></div>
          <div><dt>Lexical indexes</dt><dd>{status.data.sparse_indexes}</dd></div>
          <div><dt>Dense candidates</dt><dd>{status.data.dense_top_k}</dd></div>
          <div><dt>Lexical candidates</dt><dd>{status.data.sparse_top_k}</dd></div>
          <div><dt>Fused candidates</dt><dd>{status.data.final_top_k}</dd></div>
          <div><dt>RRF constant</dt><dd>{status.data.rrf_k}</dd></div>
          <div><dt>BM25 k1 / b</dt><dd>{status.data.bm25_k1} / {status.data.bm25_b}</dd></div>
          <div><dt>Unavailable lane</dt><dd>{status.data.degradation_policy}</dd></div>
          <div><dt>Query encoder</dt>
            <dd className="mono">{status.data.query_encoder?.model_id ?? 'not yet recorded'}</dd></div>
        </dl>}
      <p className="muted">Candidate counts and the fusion constant are configured seeds measured
        by the offline evaluation, not calibrated production thresholds.</p>
    </section>}

    <section className="panel"><h2>Query</h2>
      <form onSubmit={event => { event.preventDefault(); if (query.trim()) search.mutate(query); }}>
        <label htmlFor="retrieval-query">Query text</label>
        <textarea id="retrieval-query" rows={3} value={query} onChange={e => setQuery(e.target.value)}
          placeholder="Enter a query to retrieve source passages…" />
        <label htmlFor="retrieval-mode">Retrieval mode</label>
        <select id="retrieval-mode" value={mode} onChange={e => setMode(e.target.value)}
          aria-describedby="retrieval-mode-help">
          {MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <p id="retrieval-mode-help" className="muted">
          {MODES.find(([value]) => value === mode)?.[2]}
          {' '}
          <strong>Diagnostic only — this selection does not change Ask behaviour.</strong>
          {MODES.find(([value]) => value === mode)?.[3]
            ? ' This mode calls the configured provider.'
            : ' This mode makes no provider call.'}
        </p>
        <div className="question-footer">
          <span>The query is sent to retrieval only. Nothing generates text from it.</span>
          <button type="submit" disabled={!query.trim() || search.isPending}>
            {search.isPending ? 'Retrieving…' : 'Retrieve candidates'}
          </button>
        </div>
      </form>
      {error && <p role="alert" className="error">{error.code}: {error.message}</p>}
    </section>

    {result && <>
      {result.warnings.map(warning =>
        <p key={warning} role="alert" className="error">Retrieval warning: {warning}</p>)}

      <div className="actions" aria-label="Retrieval lanes">
        {([['hybrid', `Hybrid (${result.candidates.length})`],
           ['dense', `Dense (${result.dense.length})`],
           ['sparse', `BM25 (${result.sparse.length})`]] as const).map(([value, label]) =>
          <button key={value} aria-pressed={lane === value} onClick={() => setLane(value)}>{label}</button>)}
        {reranked && <><button aria-pressed={lane === 'reranked'} onClick={() => setLane('reranked')}>Reranked</button>
          <button aria-pressed={lane === 'evidence'} onClick={() => setLane('evidence')}>Evidence Set</button></>}
        {drafted && <><button aria-pressed={lane === 'sufficiency'} onClick={() => setLane('sufficiency')}>Sufficiency</button>
          <button aria-pressed={lane === 'draft'} onClick={() => setLane('draft')}>
            {drafted.abstention ? 'Abstention' : 'Grounded draft'}</button></>}
        {answered?.verification && <><button aria-pressed={lane === 'claims'} onClick={() => setLane('claims')}>Claims</button>
          <button aria-pressed={lane === 'answer'} onClick={() => setLane('answer')}>
            {answered.verified ? 'Verified answer' : 'Verification abstention'}</button></>}
      </div>
      {reranked && (lane === 'reranked' || lane === 'evidence') && <EvidenceInspector result={reranked} stage={lane} />}
      {drafted && (lane === 'sufficiency' || lane === 'draft') && <DraftInspector result={drafted} stage={lane} />}
      {answered && (lane === 'claims' || lane === 'answer') && <VerificationInspector result={answered} stage={lane} />}

      {lane === 'hybrid' && <section className="panel"><h2>{result.mode === 'HYBRID_RRF' ? 'Fused candidates' : 'Retrieved candidates'}</h2>
        <p className="muted">Ranked by reciprocal rank fusion. The fused score is a rank-based
          diagnostic; it is not comparable with a lane score and is not a relevance probability.</p>
        {!result.candidates.length && <p>No candidate passage was retrieved for this query.</p>}
        {result.candidates.map(candidate => <article key={candidate.chunk_id} className="version-card">
          <h3>#{candidate.fused_rank} · {candidate.provenance.document_title}</h3>
          <p>{candidate.provenance.chunk_type} · pages {candidate.provenance.page_start ?? 'unlocated'}–
            {candidate.provenance.page_end ?? 'unlocated'} · {candidate.provenance.source_type} ·
            authority {candidate.provenance.authority_level}</p>
          <p className="muted">
            fused {candidate.fused_score.toFixed(5)}
            {candidate.dense_rank !== null && ` · dense rank ${candidate.dense_rank} (${candidate.dense_score?.toFixed(3)})`}
            {candidate.sparse_rank !== null && ` · BM25 rank ${candidate.sparse_rank} (${candidate.sparse_score?.toFixed(3)})`}
            {` · lanes ${candidate.lanes.join(' + ')}`}
          </p>
          {!!candidate.matched_terms.length &&
            <p className="mono">matched: {candidate.matched_terms.slice(0, 12).join(', ')}</p>}
          <pre className="chunk-preview">{candidate.preview}</pre>
          <button onClick={() => setSelected(candidate)}>Inspect provenance</button>
        </article>)}
      </section>}

      {(lane === 'dense' || lane === 'sparse') && <section className="panel">
        <h2>{lane === 'dense' ? 'Dense lane' : 'BM25 lane'}</h2>
        <p className="muted">{lane === 'dense'
          ? 'MedCPT query vector against the verified dense index. Scores are inner products.'
          : 'Lexical BM25 over the versioned analyzer. Scores are BM25 sums.'} Scores from the two
          lanes are on unrelated scales and must not be compared with each other.</p>
        {!(lane === 'dense' ? result.dense : result.sparse).length && <p>This lane returned nothing.</p>}
        <ol className="service-list">
          {(lane === 'dense' ? result.dense : result.sparse).map(hit =>
            <li key={hit.chunk_id}>
              <span className="mono">#{hit.rank} {hit.chunk_id}</span>
              <strong>{hit.score.toFixed(4)}</strong>
            </li>)}
        </ol>
      </section>}

      <section className="panel"><h2>Execution trace</h2>
        <p className="muted">Enough to reproduce this retrieval. The query text itself is not
          recorded; the hash below identifies it instead.</p>
        <dl className="detail-grid">
          <div><dt>Mode</dt><dd>{result.trace.mode}</dd></div>
          <div><dt>Query hash</dt><dd className="mono checksum">{result.trace.query_hash}</dd></div>
          <div><dt>Query tokens</dt><dd>{result.trace.query_token_count ?? 'not encoded'}</dd></div>
          <div><dt>Dense candidates</dt><dd>{result.trace.dense_candidates}</dd></div>
          <div><dt>Lexical candidates</dt><dd>{result.trace.sparse_candidates}</dd></div>
          <div><dt>Fused candidates</dt><dd>{result.trace.fused_candidates}</dd></div>
          <div><dt>RRF constant</dt><dd>{result.trace.rrf_k}</dd></div>
          <div><dt>BM25 k1 / b</dt><dd>{result.trace.bm25_k1} / {result.trace.bm25_b}</dd></div>
          <div><dt>Config fingerprint</dt>
            <dd className="mono checksum">{result.trace.retrieval_config_fingerprint}</dd></div>
          <div><dt>Dense index runs</dt><dd>{result.trace.index_run_ids.length}</dd></div>
          <div><dt>Lexical indexes</dt><dd>{result.trace.sparse_index_ids.length}</dd></div>
          <div><dt>Chunk datasets</dt><dd>{result.trace.chunk_run_ids.length}</dd></div>
        </dl>
        <ul className="service-list">{Object.entries(result.trace.durations_ms).map(([stage, value]) =>
          <li key={stage}><span>{stage.replace('_ms', '').replaceAll('_', ' ')}</span>
            <strong>{value.toFixed(1)} ms</strong></li>)}</ul>
      </section>
    </>}

    {selected && <Provenance candidate={selected} close={() => setSelected(null)} />}
  </>;
}

/** A candidate resolves to its chunk, its document version and the original source page. */
function Provenance({ candidate, close }: { candidate: Candidate; close: () => void }) {
  const source = candidate.provenance;
  return <section className="panel" aria-label="Candidate provenance"><h2>Candidate provenance</h2>
    <button onClick={close}>Close detail</button>
    <dl className="detail-grid">
      <div><dt>Document</dt><dd>{source.document_title}</dd></div>
      <div><dt>Source type</dt><dd>{source.source_type}</dd></div>
      <div><dt>Authority</dt><dd>{source.authority_level}</dd></div>
      <div><dt>Subject</dt><dd>{source.subject ?? '—'}</dd></div>
      <div><dt>Specialty</dt><dd>{source.specialty ?? '—'}</dd></div>
      <div><dt>Chunk type</dt><dd>{source.chunk_type}</dd></div>
      <div><dt>Pages</dt><dd>{source.page_start ?? 'unlocated'}–{source.page_end ?? 'unlocated'}</dd></div>
      <div><dt>Sequence</dt><dd>{source.sequence_number}</dd></div>
      <div><dt>Source elements</dt><dd>{source.source_element_ids.length}</dd></div>
      <div><dt>Chunk</dt><dd className="mono checksum">{candidate.chunk_id}</dd></div>
    </dl>
    {source.question_id && <p className="muted">
      This candidate is assessment material. Being retrievable never makes a question key an
      authoritative source.
    </p>}
    <Link to={`/documents/${source.document_id}`}>Open document</Link>{' · '}
    <Link to={`/chunk-runs/${source.chunk_run_id}?chunk=${candidate.chunk_id}`}>Open chunk inspector</Link>
  </section>;
}
