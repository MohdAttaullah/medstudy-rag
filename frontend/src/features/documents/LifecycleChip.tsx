import { Link } from 'react-router-dom';
import { Icon } from '../navigation/icons';
import { duration, sinceOnServer, STAGE_LABEL, STATE } from './lifecycle';
import type { LifecycleSummary } from '../../types/lifecycle';

/**
 * One compact line of lifecycle for a Library row: the state in words, what it is doing, and one
 * link to the place where it can be followed or resolved. The full pipeline lives on the
 * document's own page; a table of seven-stage steppers would bury the list it is meant to label.
 */
export function LifecycleChip({ documentId, title, summary, receivedAt, now }: {
  documentId: string;
  title: string;
  summary: LifecycleSummary | null | undefined;
  receivedAt: number;
  now: number;
}) {
  if (!summary) return null;
  const state = STATE[summary.state];
  let detail = '';
  if (summary.state === 'PROCESSING') {
    const elapsed = sinceOnServer(summary.run_started_at, summary.server_time, receivedAt, now);
    const stage = summary.current_stage ? STAGE_LABEL[summary.current_stage] : '';
    detail = [stage, elapsed !== null ? duration(elapsed) : ''].filter(Boolean).join(' · ');
  } else if (summary.state === 'REVIEW_REQUIRED') {
    detail = `${summary.blocking_count} blocking`;
  }
  const link = summary.state === 'PROCESSING' ? 'View progress'
    : summary.state === 'REVIEW_REQUIRED' ? 'Review'
      : summary.state === 'FAILED' || summary.state === 'DELETION_INCOMPLETE' ? 'Details' : '';
  return <span className="lifecycle-chip-cell">
    <span className={`lifecycle-chip tone-${state.tone}`}>
      <span className={summary.state === 'PROCESSING' ? 'chip-spin' : undefined} aria-hidden="true">
        <Icon name={state.icon} small />
      </span>
      <span>{state.short}{detail && <span className="chip-detail"> · {detail}</span>}</span>
    </span>
    {link && <Link className="chip-link" to={`/documents/${documentId}#lifecycle`}>
      {link}<span className="visually-hidden">: {title}</span>
    </Link>}
  </span>;
}
