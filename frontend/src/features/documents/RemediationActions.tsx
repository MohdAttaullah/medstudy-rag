import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import { Icon } from '../navigation/icons';
import {
  ACTION_PATH, attemptNote, recommended, REMEDIATION, remedies, unavailableReason,
} from './lifecycle';
import type { Lifecycle, ReprocessAction } from '../../types/lifecycle';

/**
 * The reprocessing choices for a document stopped for review, explained before they are used.
 *
 * Which actions exist is the server's decision for the job's real state and the caller's
 * permissions (`lifecycle.actions`); this only presents them. The one that fits the finding is
 * marked recommended, the others stay available with their trade-offs, and the ones that cannot
 * work now are named with the reason rather than offered as buttons. A reader receives no actions
 * from the server and sees none here.
 */
export function RemediationActions({ lifecycle, compact = false }: { lifecycle: Lifecycle; compact?: boolean }) {
  const { token } = useSession();
  const queries = useQueryClient();
  const [busy, setBusy] = useState<ReprocessAction | ''>('');
  const [error, setError] = useState('');
  const [started, setStarted] = useState<ReprocessAction | ''>('');
  const review = lifecycle.review;
  if (!review) return null;

  const available = new Set(lifecycle.actions.filter(item => item.available).map(item => item.action));
  const offered = remedies(review, available);
  const best = recommended(review, offered);
  const note = attemptNote(review);
  const blocked = lifecycle.actions.filter(item => !item.available && item.action !== 'cancel');

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
      setStarted(action);
      // Restarts polling: the lifecycle is no longer terminal.
      for (const queryKey of [['document'], ['jobs'], ['documents'], ['chunks']]) {
        await queries.invalidateQueries({ queryKey });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The action failed.');
    } finally { setBusy(''); }
  }

  if (!lifecycle.actions.length) {
    return <p className="remedy-reader">
      <Icon name="info" small />
      A curator can resolve this by reprocessing the document. You can inspect what was found.
    </p>;
  }

  return <div className="remedies">
    {note && <p className={`review-repeat note-${note.tone}`} role="note">
      <Icon name={note.tone === 'info' ? 'info' : 'conflict'} small />{note.text}
    </p>}
    {offered.length === 0 && <p className="muted">
      No reprocessing action can resolve this from the document’s current state.
    </p>}
    <div className="review-actions">
      {offered.map(action => {
        const info = REMEDIATION[action];
        const isBest = action === best;
        return <div key={action} className={`remedy${isBest ? ' remedy-recommended' : ''}`}>
          <div className="remedy-head">
            <button type="button" className={isBest ? '' : 'secondary'} disabled={busy !== ''}
              aria-describedby={`remedy-${action}`} onClick={() => void run(action)}>
              {busy === action ? 'Starting…' : info.label}
            </button>
            {isBest && <span className="remedy-badge"><Icon name="verified" small />Recommended</span>}
          </div>
          <p id={`remedy-${action}`} className="remedy-does">{info.does}
            {' '}<span className="remedy-when">Use when: {info.when}</span></p>
          {!compact && <details className="remedy-tradeoffs">
            <summary>Pros and cons</summary>
            <div className="tradeoffs">
              <ul aria-label={`${info.label} pros`}>{info.pros.map(p => <li key={p}>{p}</li>)}</ul>
              <ul aria-label={`${info.label} cons`}>{info.cons.map(c => <li key={c}>{c}</li>)}</ul>
            </div>
          </details>}
        </div>;
      })}
    </div>
    {blocked.length > 0 && <details className="remedy-unavailable">
      <summary>Not available now ({blocked.length})</summary>
      <ul>{blocked.map(item => <li key={item.action}>
        <strong>{REMEDIATION[item.action].label}</strong> — {unavailableReason(item.action, item.reason, review)}
      </li>)}</ul>
    </details>}
    {started && <p role="status" className="success-note">
      {REMEDIATION[started].label} started. Progress appears on the document page.
    </p>}
    {error && <p role="alert" className="error">{error}</p>}
  </div>;
}
