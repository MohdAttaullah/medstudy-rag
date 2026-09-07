import { Link } from 'react-router-dom';
import type { AskCitation, AskResponse } from '../../types/retrieval';

const ASSESSMENT = new Set(['QUESTION_BANK', 'QUESTION_PAPER', 'ANSWER_KEY']);

/**
 * Where a citation opens. The M2 parse inspector already renders the authoritative page, its
 * elements and their recorded regions under server-side authorization, so a citation links there
 * rather than to an object-store URL the browser could hold on its own.
 */
export function sourceHref(citation: AskCitation) {
  // The span's own page, not merely the citation's first: a citation spanning pages 3-4 whose
  // region sits on 4 must open 4, or the highlight points at nothing.
  const page = citation.spans[0]?.page ?? citation.pages[0] ?? 1;
  const element = citation.spans[0]?.element_id;
  const artifact = citation.artifacts[0]?.artifact_id;
  const base = `/documents/${citation.document_id}/versions/${citation.document_version_id}/parse/${citation.parse_run_id}?page=${page}`;
  // Deep-link to the exact element or artifact so the region can be highlighted from real
  // provenance; without one the viewer opens the page and highlights nothing.
  return artifact ? `${base}#artifact-${artifact}` : element ? `${base}#element-${element}` : base;
}

function Citation({ citation }: {citation: AskCitation}) {
  const assessment = ASSESSMENT.has(citation.source_type);
  const box = citation.spans.find(span => span.bbox && span.bbox.every(v => v !== null));
  return <article className="version-card">
    <h4>[{citation.ordinal}] {citation.document_title}</h4>
    <p>{citation.source_type.replaceAll('_', ' ').toLowerCase()} · authority {citation.authority_level.toLowerCase()}
      {' · '}{citation.chunk_type.replaceAll('_', ' ').toLowerCase()}
      {citation.pages.length ? ` · page ${citation.pages.join(', ')}` : ' · page not recorded'}</p>
    {assessment && <p role="note" className="notice">
      Assessment material. A recorded examiner answer is not, by itself, a medical reference.
    </p>}
    <pre className="chunk-preview">{citation.cited_text}</pre>
    {citation.artifacts.map(artifact => <p key={artifact.artifact_id}>
      {artifact.kind === 'TABLE' && <>Table source · rows {artifact.row_indexes.join(', ') || 'unlisted'} · headers {artifact.header_rows.join(', ') || 'none recorded'}</>}
      {artifact.kind === 'FORMULA' && <>Formula source — shown exactly as the document states it; nothing here rewrites the notation.</>}
      {artifact.kind === 'FIGURE' && <>Figure source · {artifact.image_available ? 'original image available in the page viewer' : 'original crop unavailable; open the source page'}</>}
    </p>)}
    <p className="mono">
      {box ? `Region on page ${box.page ?? '—'}: ${box.bbox!.map(v => Number(v).toFixed(1)).join(', ')}` : 'No region recorded for this source; the page is shown instead.'}
    </p>
    <div className="actions">
      <Link to={sourceHref(citation)}>Open source page</Link>
      <Link to={`/chunk-runs/${citation.chunk_run_id}?chunk=${citation.citation_id}`}>Inspect provenance</Link>
    </div>
  </article>;
}

/**
 * The answer surface.
 *
 * A substantive answer is rendered only when the server says the outcome is VERIFIED. Every other
 * outcome renders as its own distinct state with its own explanation, because "no evidence",
 * "sources disagree", "the draft failed checking" and "the service broke" call for four different
 * responses from a reader. There is no confidence figure anywhere: an answer is verified against
 * its sources or it is not shown.
 */
export function Answer({ result }: {result: AskResponse}) {
  if (result.outcome !== 'VERIFIED') {
    const tone = result.outcome === 'FAILED' ? 'Service problem' : 'No answer shown';
    return <section className="panel" aria-labelledby="ask-outcome">
      <p className="eyebrow">{tone.toUpperCase()}</p>
      <h2 id="ask-outcome">{TITLES[result.outcome]}</h2>
      <p role="status">{result.message}</p>
      {result.outcome === 'CONFLICTING_EVIDENCE' && <p>
        The competing sources are preserved rather than one being chosen. Nothing here ranks one
        source above another.
      </p>}
      {result.outcome === 'INSUFFICIENT_EVIDENCE' && <p>
        Nothing was filled in from the model's own knowledge. If the corpus does not cover this,
        the workspace says so instead of guessing.
      </p>}
      {!!result.reason_codes.length && <details><summary>Why</summary>
        <ul className="service-list">{result.reason_codes.map(code =>
          <li key={code}><span className="mono">{code}</span></li>)}</ul></details>}
    </section>;
  }

  return <section className="panel" aria-labelledby="ask-answer">
    <p className="eyebrow">VERIFIED AGAINST RETRIEVED SOURCES</p>
    <h2 id="ask-answer">Answer</h2>
    <p role="status">{result.message}</p>
    <div className="answer-text">{result.answer!.split('\n').map((line, index) =>
      <p key={index}>{line}</p>)}</div>

    <h3>Sources</h3>
    <ul className="service-list">{result.sources.map(source =>
      <li key={source.document_version_id}>
        <span>{source.title}</span>
        <span className="mono">{source.authority_level.toLowerCase()} · {source.citation_ids.length} citation{source.citation_ids.length === 1 ? '' : 's'}</span>
      </li>)}</ul>

    <h3>Citations</h3>
    {result.citations.map(citation => <Citation key={citation.citation_id} citation={citation} />)}
  </section>;
}

const TITLES: Record<string, string> = {
  INSUFFICIENT_EVIDENCE: 'Insufficient evidence',
  CONFLICTING_EVIDENCE: 'Sources disagree',
  UNVERIFIED: 'Could not verify an answer',
  FAILED: 'The answering service failed',
};
