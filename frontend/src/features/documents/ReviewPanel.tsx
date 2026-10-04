import { Link } from 'react-router-dom';
import { Icon } from '../navigation/icons';
import { guidanceFor, STAGE_LABEL } from './lifecycle';
import { RemediationActions } from './RemediationActions';
import type { FindingGroup, Lifecycle } from '../../types/lifecycle';

/**
 * Why processing stopped, and what to do about it.
 *
 * Blocking issues come first and open; warnings are grouped and folded away, so thirteen copies
 * of one harmless warning cannot bury the single issue that stopped the document. Inspecting the
 * issue is always the first step offered. The reprocessing paths below it are only those the
 * server says can succeed from here and the user is allowed to run — a chunk set that failed
 * review is never offered for embedding, and nothing offers to accept a passage too large to embed.
 */

const SEVERITY_WORD: Record<string, string> = {
  CRITICAL: 'Critical', ERROR: 'Blocking', WARNING: 'Warning', INFO: 'Note',
};

function pages(group: FindingGroup) {
  const listed = [...new Set(group.samples.map(sample => sample.page).filter(page => page !== null))];
  if (!listed.length) return null;
  const more = group.count > group.samples.length ? ' and more' : '';
  return `${listed.length === 1 ? 'Page' : 'Pages'} ${listed.join(', ')}${more}`;
}

function inspectHref(lifecycle: Lifecycle, group: FindingGroup, target: 'chunk' | 'parse') {
  const review = lifecycle.review!;
  const sample = group.samples[0];
  if (target === 'chunk' && review.stage === 'CHUNK' && review.run_id) {
    // Opens the inspector on this chunk, with the list filtered to what blocks, so the reviewer
    // lands on the problem rather than at the top of every passage.
    const query = new URLSearchParams({ status: group.blocking ? 'blocking' : 'warning' });
    if (sample?.chunk_id) query.set('chunk', sample.chunk_id);
    return `/chunk-runs/${review.run_id}?${query}`;
  }
  // The parse inspector serves both: parse findings, and chunk findings that start in extraction.
  if (!lifecycle.version_id) return null;
  const run = review.stage === 'PARSE' ? review.run_id : null;
  if (!run) return null;
  return `/documents/${lifecycle.document_id}/versions/${lifecycle.version_id}/parse/${run}`
    + (sample?.page ? `?page=${sample.page}` : '');
}

function Issue({ lifecycle, group, open }: { lifecycle: Lifecycle; group: FindingGroup; open: boolean }) {
  const guidance = guidanceFor(group);
  const where = pages(group);
  const target = guidance?.inspect ?? (lifecycle.review?.stage === 'PARSE' ? 'parse' : 'chunk');
  const href = target ? inspectHref(lifecycle, group, target) : null;
  return <details className={`issue ${group.blocking ? 'issue-blocking' : 'issue-warning'}`} open={open}>
    <summary>
      <span className="issue-mark" aria-hidden="true">
        <Icon name={group.blocking ? 'failed' : 'conflict'} small />
      </span>
      <span className="issue-title">{guidance?.title ?? group.message}</span>
      <span className="issue-count">{group.count > 1 ? `× ${group.count}` : ''}</span>
      <span className={`issue-severity sev-${group.severity.toLowerCase()}`}>
        {SEVERITY_WORD[group.severity] ?? group.severity}
      </span>
    </summary>
    <div className="issue-body">
      {guidance && <p>{guidance.explanation}</p>}
      <p className="issue-source">
        <span className="mono">{group.code}</span> — {group.message}
        {where && <> · {where}</>}
      </p>
      {group.blocking && href && <Link className="button-link issue-inspect" to={href}>
        <Icon name="inspect" small />{group.count === 1 ? 'Inspect this issue' : 'Inspect the first occurrence'}
      </Link>}
    </div>
  </details>;
}

export function ReviewPanel({ lifecycle }: { lifecycle: Lifecycle }) {
  const review = lifecycle.review;
  if (!review) return null;

  const blocking = review.groups.filter(group => group.blocking);
  const warnings = review.groups.filter(group => !group.blocking);
  const exhausted = lifecycle.actions.some(item => item.reason === 'NO_RETRIES_LEFT');
  const next = lifecycle.stages.find(stage => stage.state === 'PENDING');

  return <section className="review-panel" aria-labelledby="review-title">
    <div className="review-head">
      <span className="review-mark" aria-hidden="true"><Icon name="conflict" /></span>
      <div>
        <h2 id="review-title">Review required</h2>
        <p>
          Processing paused{next ? ` before “${STAGE_LABEL[next.code].toLowerCase()}”` : ''} because
          {' '}{review.blocking_count === 1 ? 'one issue needs' : `${review.blocking_count} issues need`}
          {' '}a person to look at {review.blocking_count === 1 ? 'it' : 'them'}. Nothing was indexed
          from this version, so it cannot appear in Ask answers until this is resolved.
        </p>
      </div>
    </div>

    <ul className="review-counts" aria-label="Summary">
      <li className="count-blocking">
        <Icon name="failed" small />{review.blocking_count} blocking {review.blocking_count === 1 ? 'issue' : 'issues'}
      </li>
      {review.warning_count > 0 && <li className="count-warning">
        <Icon name="conflict" small />{review.warning_count} {review.warning_count === 1 ? 'warning' : 'warnings'} (not blocking)
      </li>}
    </ul>

    {blocking.length > 0 && <>
      <h3>Why processing stopped</h3>
      <div className="issue-list">
        {blocking.map(group => <Issue key={`${group.code}:${group.severity}`}
          lifecycle={lifecycle} group={group} open />)}
      </div>
    </>}

    <h3>What to do next</h3>
    <ol className="review-steps">
      <li><strong>Inspect the issue.</strong> Open it above and check whether the source was extracted
        correctly. That decides which of the steps below can fix it.</li>
      <li>
        <strong>Then, if needed, reprocess.</strong>
        <RemediationActions lifecycle={lifecycle} />
      </li>
      {review.stage === 'PARSE' && lifecycle.actions.length > 0 && <li>
        If the extraction is correct despite the findings, a curator can accept it from the
        parse inspector using the reviewed-acceptance workflow.
      </li>}
      {review.stage === 'CHUNK' && <li className="muted">
        Passages cannot be accepted as they are. A passage too large to embed would have to be cut
        off to be searchable, so it is rebuilt instead.
      </li>}
    </ol>
    {exhausted && <p className="review-repeat" role="note"><Icon name="info" small />
      The retry budget for this document is used up ({review.max_retries} of {review.max_retries}).
      Upload a corrected file as a new version, or ask an administrator.</p>}

    {warnings.length > 0 && <details className="review-warnings">
      <summary>
        Warnings ({review.warning_count + review.info_count}) — these did not stop processing
      </summary>
      <div className="issue-list">
        {warnings.map(group => <Issue key={`${group.code}:${group.severity}`}
          lifecycle={lifecycle} group={group} open={false} />)}
      </div>
    </details>}
  </section>;
}
