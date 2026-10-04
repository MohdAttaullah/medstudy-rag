import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import { date, StatusBadge } from '../../components/DocumentWidgets';
import type { ParseSummary } from '../../types/parsing';
import { RESULT_LABEL, findingSummary } from './labels';

/**
 * Parse state for one document version.
 *
 * Only data that exists is shown. There are no chunks, embeddings, vectors or retrieval
 * figures in M2, so none are displayed or implied here.
 */
export function ParseSummaryPanel({ documentId, versionId }: { documentId: string; versionId: string }) {
  const { token } = useSession();
  const summary = useQuery({
    queryKey: ['parse', documentId, versionId],
    queryFn: () => api<ParseSummary>(token, `/documents/${documentId}/versions/${versionId}/parse`),
    refetchInterval: 5000,
  });
  if (summary.isPending) return <section className="panel"><h3>Parsing</h3><p>Loading parse state…</p></section>;
  if (summary.isError) return <section className="panel"><h3>Parsing</h3><p role="alert">Parse state unavailable.</p></section>;
  const { parse_run: run, ingestion_status: status, parse_runs: attempts } = summary.data;
  return <section className="panel parse-summary">
    <div className="section-heading"><h3>Parsing</h3><StatusBadge status={status} /></div>
    {!run ? <p className="muted">
      This version has not been parsed yet. Parsing starts after the worker receives the queued job.
    </p> : <>
      <dl className="detail-grid">
        <div><dt>Parser</dt><dd>{run.parser_name} {run.parser_version} <span className="muted">({run.parser_provider})</span></dd></div>
        <div><dt>Parser configuration</dt><dd>{run.configuration_version} <span className="mono muted">{run.configuration_fingerprint.slice(0, 12)}</span></dd></div>
        <div><dt>Parse run</dt><dd className="mono">{run.id}</dd></div>
        <div><dt>Attempt</dt><dd>{run.attempt} of {attempts} recorded {attempts === 1 ? 'run' : 'runs'}{run.is_active ? ' · active' : ''}</dd></div>
        <div><dt>Pages</dt><dd>{run.page_count ?? '—'}{run.source_page_count !== null && run.source_page_count !== run.page_count ? ` (source reports ${run.source_page_count})` : ''}</dd></div>
        <div><dt>Elements</dt><dd>{run.element_count ?? '—'}</dd></div>
        <div><dt>Tables</dt><dd>{run.table_count ?? '—'}</dd></div>
        <div><dt>Figures</dt><dd>{run.figure_count ?? '—'}</dd></div>
        <div><dt>Formulas</dt><dd>{run.formula_count ?? '—'}</dd></div>
        <div><dt>OCR</dt><dd>{run.ocr_mode === 'OFF' ? 'Disabled' : `${run.ocr_mode.toLowerCase()} · ${run.ocr_engine ?? 'engine not recorded'}`}{run.ocr_page_count ? ` · ${run.ocr_page_count} page(s) recovered by OCR` : ''}</dd></div>
        <div><dt>Validation</dt><dd>{run.validation_result ? RESULT_LABEL[run.validation_result] : 'Not completed'}</dd></div>
        <div><dt>Completed</dt><dd>{date(run.completed_at)}</dd></div>
      </dl>
      <p className="muted">{findingSummary(run.finding_counts)}</p>
      {run.error_code && <p role="alert" className="error">{run.error_code}: {run.error_message}</p>}
      <p className="muted">
        Parsed structure is not searchable on its own. It becomes searchable only after its passages are embedded and both indexes verify.
      </p>
      <Link className="button-link" to={`/documents/${documentId}/versions/${versionId}/parse/${run.id}`}>
        Open parse inspector
      </Link>
    </>}
  </section>;
}
