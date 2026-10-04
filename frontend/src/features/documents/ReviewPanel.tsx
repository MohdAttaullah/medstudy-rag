import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import { Icon } from '../navigation/icons';
import {
  ACTION_COPY, ACTION_PATH, guidanceFor, remedies, repeatWarning, STAGE_LABEL,
} from './lifecycle';
import type { FindingGroup, Lifecycle, ReprocessAction } from '../../types/lifecycle';

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
    return `/chunk-runs/${review.run_id}${sample?.chunk_id ? `?chunk=${sample.chunk_id}` : ''}`;
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
  const { token } = useSession();
  const queries = useQueryClient();
  const [busy, setBusy] = useState<ReprocessAction | ''>('');
  const [error, setError] = useState('');
  const review = lifecycle.review;
  if (!review) return null;

  const blocking = review.groups.filter(group => group.blocking);
  const warnings = review.groups.filter(group => !group.blocking);
  const available = new Set(lifecycle.actions.filter(item => item.available).map(item => item.action));
  const offered = remedies(review, available);
  const exhausted = lifecycle.actions.some(item => item.reason === 'NO_RETRIES_LEFT');
  const repeat = repeatWarning(review);
  const next = lifecycle.stages.find(stage => stage.state === 'PENDING');

  async function run(action: ReprocessAction) {
    if (!lifecycle.job_id) return;
    setBusy(action); setError('');
    try {
      await api(token, `/ingestion/jobs/${lifecycle.job_id}/${ACTION_PATH[action]}`, {
        method: 'POST',
        ...(action === 'rechunk' || action === 'reembed'
          ? { body: JSON.stringify({ force: true }) }
          : action === 'reindex-sparse' ? { body: '{}' } : {}),
      });
      // Restarts polling: the lifecycle is no longer terminal.
      await queries.invalidateQueries({ queryKey: ['document'] });
      await queries.invalidateQueries({ queryKey: ['jobs'] });
      await queries.invalidateQueries({ queryKey: ['documents'] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The action failed.');
    } finally { setBusy(''); }
  }

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
      {offered.length > 0 && <li>
        <strong>Then, if needed, reprocess.</strong>
        {repeat && <p className="review-repeat" role="note"><Icon name="info" small />{repeat}</p>}
        <div className="review-actions">
          {offered.map(action => <div key={action} className="review-action">
            <button type="button" className="secondary" disabled={busy !== ''}
              onClick={() => void run(action)}>
              {busy === action ? 'Starting…' : ACTION_COPY[action].label}
            </button>
            <span>{ACTION_COPY[action].explanation}</span>
          </div>)}
        </div>
      </li>}
      {offered.length === 0 && lifecycle.actions.length === 0 && <li>
        A curator can resolve this. You can still inspect the issue to see what was found.
      </li>}
      {review.stage === 'PARSE' && lifecycle.actions.length > 0 && <li>
        If the extraction is correct despite the findings, a curator can accept it from the
        parse inspector using the reviewed-acceptance workflow.
      </li>}
    </ol>
    {exhausted && <p className="review-repeat" role="note"><Icon name="info" small />
      The retry budget for this document is used up ({review.max_retries} of {review.max_retries}).
      Upload a corrected file as a new version, or ask an administrator.</p>}
    {error && <p role="alert" className="error">{error}</p>}

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
