import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import { date, Pagination, StatusBadge } from '../../components/DocumentWidgets';
import type { Job, Page } from '../../types/documents';
export function JobHistory({ jobId }: { jobId: string }) {
  const { token } = useSession();
  const job = useQuery({ queryKey: ['jobs', 'detail', jobId], queryFn: () => api<Job>(token, '/ingestion/jobs/' + jobId), refetchInterval: 5000 });
  return <section className="history"><h3>Ingestion history</h3>
    {job.isPending ? <p>Loading history…</p> : job.isError ? <p role="alert">History unavailable.</p> : <>
      <ol>{job.data.events.map(event => <li key={event.id}><StatusBadge status={event.to_status} />
        <span>{date(event.created_at)} · {event.service_identity} · attempt {event.retry_number + 1}</span>
        {event.error_detail && <p className="error">{event.error_detail}</p>}</li>)}</ol>
      <p className="muted">Configuration: {job.data.configuration_version}</p>
      <p className="muted">{job.data.queue_received_at ? 'Worker receipt confirmed.' : 'Awaiting worker receipt; the job is durable in PostgreSQL.'}</p>
      <p className="muted">Chunking ends at READY_FOR_EMBEDDING. Embedding and indexing are not implemented.</p>
    </>}</section>;
}
export function Jobs({ documentId }: { documentId?: string }) {
  const { token, identity } = useSession();
  const queries = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState('');
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const jobs = useQuery({ queryKey: ['jobs', documentId, offset, status],
    queryFn: () => api<Page<Job>>(token, `/ingestion/jobs?offset=${offset}${documentId ? '&document_id=' + documentId : ''}${status ? '&status=' + status : ''}`), refetchInterval: 5000 });
  async function action(job: Job, name: 'retry' | 'reparse' | 'rechunk' | 'cancel') {
    setBusy(job.id); setError('');
    try { await api(token, '/ingestion/jobs/' + job.id + '/' + name, { method: 'POST', ...(name === 'rechunk' ? { body: JSON.stringify({ force: true }) } : {}) });
      for (const queryKey of [['jobs'], ['document']]) await queries.invalidateQueries({ queryKey }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The action failed.'); } finally { setBusy(''); }
  }
  return <section className="panel"><div className="section-heading"><h2>Ingestion jobs</h2><label>Status filter<select value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }}>
    <option value="">All states</option>{['UPLOADED', 'VALIDATING', 'QUEUED', 'PARSING', 'NORMALIZING', 'ENRICHING', 'READY_FOR_CHUNKING', 'CHUNKING', 'VALIDATING_CHUNKS', 'READY_FOR_EMBEDDING', 'FAILED', 'QUARANTINED', 'NEEDS_REVIEW', 'CANCELLED'].map(item => <option key={item}>{item}</option>)}</select></label></div>
    {error && <p role="alert" className="error">{error}</p>}
    {jobs.isPending ? <p>Loading jobs…</p> : jobs.isError ? <p role="alert">Could not load ingestion jobs.</p> : <>
      {!jobs.data.items.length ? <p>No ingestion jobs in this view.</p> : <div className="job-list">
        {jobs.data.items.map(job => <article className="job-card" key={job.id}><div className="section-heading">
          <Link to={'/documents/' + job.document_id}>{job.document_title} · v{job.version_number}</Link><StatusBadge status={job.status} /></div>
          <dl className="detail-grid"><div><dt>Job ID</dt><dd className="mono">{job.id}</dd></div><div><dt>Stage</dt><dd>{job.current_stage}</dd></div>
            <div><dt>Created</dt><dd>{date(job.created_at)}</dd></div><div><dt>Started</dt><dd>{date(job.started_at)}</dd></div>
            <div><dt>Retries</dt><dd>{job.retry_count} / {job.max_retries}</dd></div><div><dt>Correlation ID</dt><dd className="mono">{job.correlation_id}</dd></div></dl>
          {job.last_error_message && <p className="error">{job.last_error_code}: {job.last_error_message}</p>}
          <div className="actions"><button className="secondary" onClick={() => setSelected(selected === job.id ? '' : job.id)}>Inspect history</button>
            {identity?.permissions.includes('ingestion:retry') && <button className="secondary" disabled={busy === job.id || job.status !== 'FAILED' || job.retry_count >= job.max_retries} onClick={() => void action(job, 'retry')}>Retry</button>}
            {identity?.permissions.includes('ingestion:reparse') && <button className="secondary" disabled={busy === job.id || !['READY_FOR_CHUNKING', 'READY_FOR_EMBEDDING', 'NEEDS_REVIEW', 'FAILED'].includes(job.status) || job.retry_count >= job.max_retries} onClick={() => void action(job, 'reparse')}>Reparse</button>}
            {identity?.permissions.includes('ingestion:rechunk') && <button className="secondary" disabled={busy === job.id || !['READY_FOR_CHUNKING', 'READY_FOR_EMBEDDING', 'NEEDS_REVIEW', 'FAILED'].includes(job.status) || job.retry_count >= job.max_retries} onClick={() => void action(job, 'rechunk')}>Rechunk</button>}
            {identity?.permissions.includes('ingestion:cancel') && <button className="secondary" disabled={busy === job.id || job.status === 'CANCELLED'} onClick={() => void action(job, 'cancel')}>Cancel job</button>}
          </div>{selected === job.id && <JobHistory jobId={job.id} />}</article>)}</div>}
      <Pagination offset={offset} total={jobs.data.total} onChange={setOffset} />
    </>}</section>;
}
