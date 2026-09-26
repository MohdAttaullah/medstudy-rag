import { Link } from 'react-router-dom';
import { useSourceImage } from './SourceImage';
import type {
  AskCitation, AskFigure, AskResponse, ConversationTurnView,
} from '../../types/retrieval';

/**
 * What this surface needs, which both a live answer and a stored turn already satisfy.
 *
 * A reloaded turn renders through exactly the same component as the response that created it, so
 * a conversation read back from the server cannot present an outcome differently from the way it
 * was first shown — there is one renderer, not two that could drift.
 */
export type AnswerRecord = Pick<
  AskResponse | ConversationTurnView,
  'outcome' | 'verified' | 'answer' | 'message' | 'reason_codes' | 'citations' | 'sources'
  | 'figures'
>;

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

/**
 * One piece of evidence, shown as the thing it is: the exact words in the source.
 *
 * The excerpt is the text M8 verified the claim against, stored at verification time and read back
 * here — not re-resolved from the document, and never assembled in the browser. A reader can hold
 * this beside the page it names and find the same sentence.
 */
function Citation({ citation }: {citation: AskCitation}) {
  const assessment = ASSESSMENT.has(citation.source_type);
  const box = citation.spans.find(span => span.bbox && span.bbox.every(v => v !== null));
  const page = citation.pages.length ? `Page ${citation.pages.join(', ')}` : 'Page not recorded';
  return <article className="evidence-card">
    <p className="eyebrow">SOURCE [{citation.ordinal}]</p>
    <h4>{citation.document_title}</h4>
    <p className="evidence-where">{page} · {citation.chunk_type.replaceAll('_', ' ').toLowerCase()}
      {' · '}{citation.source_type.replaceAll('_', ' ').toLowerCase()}
      {' · authority '}{citation.authority_level.toLowerCase()}</p>
    {assessment && <p role="note" className="notice">
      Assessment material. A recorded examiner answer is not, by itself, a medical reference.
    </p>}
    <blockquote className="evidence-quote">{citation.cited_text}</blockquote>
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
 * A source figure beside an answer. Supplementary material, never evidence.
 *
 * It is here because the server found a provenance link — the citation is this figure, or the
 * citation's verified text names it by label — and the card says which. The image is served by the
 * authorized parse route, so no object-store key is ever exposed and the tenant check is the one
 * every document read already goes through. Nothing interpreted the picture.
 */
function FigureCard({ figure }: {figure: AskFigure}) {
  const base = `/api/v1/documents/${figure.document_id}/versions/${figure.document_version_id}`
    + `/parse-runs/${figure.parse_run_id}/figures/${figure.figure_id}`;
  const page = figure.page ?? 1;
  const viewer = `/documents/${figure.document_id}/versions/${figure.document_version_id}`
    + `/parse/${figure.parse_run_id}?page=${page}#artifact-${figure.figure_id}`;
  const title = figure.caption?.trim() || 'untitled in the source';
  // The image is fetched with the session's bearer token and shown from an object URL: the route
  // requires authorization, and an <img src> cannot carry a header. See SourceImage.
  const { url, failed } = useSourceImage(`${base}/image`);
  return <article className="figure-card">
    <a className="figure-thumb" href={url ?? viewer} target={url ? '_blank' : undefined}
      rel="noreferrer">
      {/* Alt text is the source's own caption, never a description of what the picture shows. */}
      {url
        ? <img src={url} alt={`Source figure as printed: ${title}`} />
        : <span className="figure-placeholder" aria-hidden="true" />}
    </a>
    <div>
      <h4>{figure.label ? `Figure ${figure.label}` : 'Source figure'}</h4>
      <p className="evidence-where">
        {figure.document_title} · {figure.page ? `page ${figure.page}` : 'page not recorded'}
      </p>
      {figure.caption && <p className="figure-caption">{figure.caption}</p>}
      <p className="figure-note" role="note">
        Source figure — not interpreted by AI.{' '}
        {figure.linked_by === 'CITED_EVIDENCE'
          ? 'The cited evidence is this figure.'
          : 'The cited source text refers to this figure.'}
      </p>
      {failed && <p className="figure-unavailable" role="note">
        The stored image could not be loaded. Open the source page to see it in the document.
      </p>}
      <div className="actions">
        {url && <a href={url} target="_blank" rel="noreferrer">Open full image</a>}
        <Link to={viewer}>Open source page</Link>
      </div>
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
export function Answer({ result }: {result: AnswerRecord}) {
  if (result.outcome !== 'VERIFIED') {
    const { severity, mark, label } = OUTCOMES[result.outcome] ?? OUTCOMES.FAILED;
    return <section className={`panel outcome outcome-${severity}`} aria-labelledby="ask-outcome">
      <p className="eyebrow"><span className="outcome-mark" aria-hidden="true">{mark}</span> {label}</p>
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
      {result.outcome === 'OUT_OF_SCOPE' && <p>
        No sources were searched and no model was called. Rephrasing the question as what the
        indexed sources say about a topic will be answered normally.
      </p>}
      {!!result.reason_codes.length && <details><summary>Why</summary>
        <ul className="service-list">{result.reason_codes.map(code =>
          <li key={code}>
            <span>{REASONS[code] ?? 'A check recorded this code without a plain-language summary.'}</span>
            <span className="mono">{code}</span>
          </li>)}</ul></details>}
    </section>;
  }

  return <section className="panel outcome outcome-verified" aria-labelledby="ask-answer">
    <p className="eyebrow"><span className="outcome-mark" aria-hidden="true">✓</span>{' '}
      VERIFIED AGAINST RETRIEVED SOURCES</p>
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
    <p className="muted">Each card is the exact source text this answer was checked against.</p>
    {result.citations.map(citation => <Citation key={citation.citation_id} citation={citation} />)}

    {!!result.figures?.length && <>
      <h3>Related source figures</h3>
      <p className="muted">
        Shown because the cited evidence links to them. They are source material, not evidence: no
        image was read, and no statement above rests on one.
      </p>
      {result.figures.map(figure => <FigureCard key={figure.figure_id} figure={figure} />)}
    </>}
  </section>;
}

/**
 * How each outcome is announced.
 *
 * Severity drives a colour, but never alone: every state also carries a distinct mark and its own
 * words, so the difference survives greyscale, colour blindness and a screen reader. The marks are
 * decorative — `aria-hidden` — because the label beside them already says it in text.
 */
const OUTCOMES: Record<string, {severity: string; mark: string; label: string}> = {
  INSUFFICIENT_EVIDENCE: {severity: 'caution', mark: '◍', label: 'NO ANSWER SHOWN'},
  CONFLICTING_EVIDENCE: {severity: 'caution', mark: '◍', label: 'SOURCES DISAGREE'},
  UNVERIFIED: {severity: 'warning', mark: '!', label: 'NOT VERIFIED'},
  FAILED: {severity: 'error', mark: '✕', label: 'SERVICE PROBLEM'},
  OUT_OF_SCOPE: {severity: 'neutral', mark: 'i', label: 'OUTSIDE WHAT THIS WORKSPACE DOES'},
};

/** Reason codes in a reviewer's words. The code itself stays visible beside each one. */
const REASONS: Record<string, string> = {
  SEMANTICALLY_UNSUPPORTED: 'A statement in the proposed answer was not established by the evidence cited with it.',
  CLAIM_NOT_CITED: 'A statement carried no citation, so nothing could check it.',
  CLAIM_CONTRADICTED: 'The evidence stated something incompatible with a statement in the proposed answer.',
  NUMERIC_MISMATCH: 'A number, dose or unit did not match the evidence.',
  NEGATION_MISMATCH: 'A statement reversed a negation the evidence stated.',
  CERTAINTY_OVERSTATED: 'A statement presented as settled what the evidence only suggested.',
  VISUAL_CLAIM: 'A statement depended on reading a figure, which this system does not do.',
  EVIDENCE_CONFLICT: 'Two sources disagreed and the disagreement was not resolved.',
  REPAIR_FAILED: 'One corrected attempt was requested and it also failed verification.',
  VERIFIER_FAILED: 'The verifier could not run, so nothing was approved.',
  EVIDENCE_DOES_NOT_ADDRESS_QUESTION: 'The retrieved sources are about a different subject.',
  RETRIEVAL_CORPUS_EMPTY: 'No indexed document was available to search.',
  OUT_OF_SCOPE_PERSONAL_ADVICE: 'The question asked for advice about a specific person.',
};

const TITLES: Record<string, string> = {
  INSUFFICIENT_EVIDENCE: 'Insufficient evidence',
  CONFLICTING_EVIDENCE: 'Sources disagree',
  UNVERIFIED: 'Could not verify an answer',
  FAILED: 'The answering service failed',
  OUT_OF_SCOPE: 'Not a question this workspace answers',
};
