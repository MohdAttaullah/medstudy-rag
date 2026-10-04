import { Fragment } from 'react';
import { Link } from 'react-router-dom';
import { useSourceImage } from './SourceImage';
import { Icon, type IconName } from '../navigation/icons';
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

/** A stored enumeration in the words a reader uses. The raw value stays available in Details. */
const human = (value: string) => value.replaceAll('_', ' ').toLowerCase();

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
 *
 * The reading order is the order a clinician actually needs: which document, which page, then the
 * quote. Chunk type, authority level and the recorded bounding box are real provenance and are
 * still on the page, but behind a disclosure — they were set at the same size and weight as the
 * excerpt, competing with it for the attention of a reader who came to read the source sentence.
 */
function Citation({ citation }: {citation: AskCitation}) {
  // The document this answer cited has since been permanently deleted. Its text went with it, so
  // there is nothing left to quote or open — only the fact that a source once stood here.
  if (citation.source_deleted) return <article className="evidence-card evidence-deleted">
    <p className="eyebrow">SOURCE [{citation.ordinal}]</p>
    <h4><Icon name="delete" small />Source deleted</h4>
    <p className="muted">
      The document this citation pointed to was permanently deleted after this answer was given.
      Its text is no longer kept, so this statement can no longer be checked against it.
    </p>
  </article>;
  const assessment = ASSESSMENT.has(citation.source_type);
  const box = citation.spans.find(span => span.bbox && span.bbox.every(v => v !== null));
  const page = citation.pages.length
    ? `Page ${citation.pages.join(', ')}`
    : 'Page not recorded';
  return <article className="evidence-card">
    {/* The bracketed ordinal is the same marker the answer text carries inline, so a reader can
        match a sentence to its card without counting. */}
    <p className="eyebrow">SOURCE [{citation.ordinal}]</p>
    <h4>{citation.document_title}</h4>
    <p className="evidence-where">{page}</p>
    {assessment && <p role="note" className="assessment-note">
      <Icon name="conflict" small />
      Assessment material. A recorded examiner answer is not, by itself, a medical reference.
    </p>}
    {/* A long excerpt scrolls inside the card. That makes it a keyboard stop — a reader who cannot
        use a pointer still has to be able to scroll it — so it is given an explicit tab stop and
        a name, rather than relying on the browsers that happen to focus scrollable regions. */}
    <blockquote className="evidence-quote" tabIndex={0}
      aria-label={`Source excerpt ${citation.ordinal}, scrollable`}>{citation.cited_text}</blockquote>
    <div className="actions">
      <Link to={sourceHref(citation)}>Open source page</Link>
    </div>
    <details className="evidence-detail">
      <summary>Source details</summary>
      <dl>
        <dt>Document type</dt><dd>{human(citation.source_type)}</dd>
        <dt>Authority</dt><dd>authority {human(citation.authority_level)}</dd>
        <dt>Passage</dt><dd>{human(citation.chunk_type)}</dd>
        <dt>Region</dt><dd className="mono">
          {box
            ? `Region on page ${box.page ?? '—'}: ${box.bbox!.map(v => Number(v).toFixed(1)).join(', ')}`
            : 'No region recorded for this source; the page is shown instead.'}
        </dd>
        {citation.artifacts.map(artifact => <Fragment key={artifact.artifact_id}>
          <dt>{human(artifact.kind)}</dt>
          <dd>
            {artifact.kind === 'TABLE' && <>Table source · rows {artifact.row_indexes.join(', ') || 'unlisted'} · headers {artifact.header_rows.join(', ') || 'none recorded'}</>}
            {artifact.kind === 'FORMULA' && <>Formula source — shown exactly as the document states it; nothing here rewrites the notation.</>}
            {artifact.kind === 'FIGURE' && <>Figure source · {artifact.image_available ? 'original image available in the page viewer' : 'original crop unavailable; open the source page'}</>}
          </dd>
        </Fragment>)}
      </dl>
      <div className="actions">
        <Link to={`/chunk-runs/${citation.chunk_run_id}?chunk=${citation.citation_id}`}>Inspect provenance</Link>
      </div>
    </details>
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
    <div className="figure-body">
      <h4>{figure.label ? `Figure ${figure.label}` : 'Source figure'}</h4>
      <p className="evidence-where">
        {figure.document_title} · {figure.page ? `page ${figure.page}` : 'page not recorded'}
      </p>
      {figure.caption && <p className="figure-caption">{figure.caption}</p>}
      <p className="figure-note" role="note">
        <Icon name="figure" small />
        <span>Source figure — not interpreted by AI.{' '}
          {figure.linked_by === 'CITED_EVIDENCE'
            ? 'The cited evidence is this figure.'
            : 'The cited source text refers to this figure.'}</span>
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
 *
 * Visually, a verified answer is *not* a card. It is the subject of the page, set as text against
 * the canvas with a quiet status line above it. The outcomes that withheld an answer do get a
 * tinted surface, because those are the exceptions a reader has to notice.
 */
export function Answer({ result }: {result: AnswerRecord}) {
  if (result.outcome !== 'VERIFIED') {
    const state = OUTCOMES[result.outcome] ?? OUTCOMES.FAILED;
    return <section className={`answer answer-exception outcome outcome-${state.severity}`}
      aria-labelledby="ask-outcome">
      <OutcomeLine state={state} />
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
      {!!result.reason_codes.length && <details className="evidence-detail"><summary>Why</summary>
        <ul className="service-list">{result.reason_codes.map(code =>
          <li key={code}>
            <span>{REASONS[code] ?? 'A check recorded this code without a plain-language summary.'}</span>
            <span className="mono">{code}</span>
          </li>)}</ul></details>}
    </section>;
  }

  const deleted = new Set(result.citations
    .filter(citation => citation.source_deleted).map(citation => citation.document_version_id));
  return <section className="answer outcome outcome-verified" aria-labelledby="ask-answer">
    <OutcomeLine state={OUTCOMES.VERIFIED} />
    <h2 id="ask-answer">Answer</h2>
    <p role="status">{result.message}</p>
    <div className="answer-text">{result.answer!.split('\n').map((line, index) =>
      <p key={index}>{line}</p>)}</div>

    <h3>Sources</h3>
    <ul className="source-list">{result.sources.map(source =>
      <li key={source.document_version_id}>
        <span>{deleted.has(source.document_version_id) ? 'Source deleted' : source.title}</span>
        {/* Plain metadata, not code: a monospaced face here made an authority level read like an
            identifier. The genuinely technical values keep the monospaced face. */}
        <span className="muted">{human(source.authority_level)} · {source.citation_ids.length} citation{source.citation_ids.length === 1 ? '' : 's'}</span>
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
 * How each outcome is announced: a mark, a word, and a colour — in that order of load-bearing.
 *
 * `data-mark` names the shape rather than describing it, so a test can assert that each severity
 * really does carry its own non-colour marker without depending on which glyph was chosen. The
 * mark is `aria-hidden` because the label beside it already says it in words.
 */
function OutcomeLine({ state }: { state: OutcomeState }) {
  return <p className="eyebrow outcome-line">
    <span className="outcome-mark" data-mark={state.icon} aria-hidden="true">
      <Icon name={state.icon} small />
    </span>
    {state.label}
  </p>;
}

interface OutcomeState { severity: string; icon: IconName; label: string }

const OUTCOMES: Record<string, OutcomeState> = {
  VERIFIED: {severity: 'verified', icon: 'verified', label: 'Verified against sources'},
  INSUFFICIENT_EVIDENCE: {severity: 'caution', icon: 'no-answer', label: 'Not enough evidence'},
  CONFLICTING_EVIDENCE: {severity: 'caution', icon: 'conflict', label: 'Sources disagree'},
  UNVERIFIED: {severity: 'warning', icon: 'unverified', label: 'Could not verify'},
  FAILED: {severity: 'error', icon: 'failed', label: 'Technical failure'},
  OUT_OF_SCOPE: {severity: 'neutral', icon: 'scope', label: 'Outside workspace scope'},
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
