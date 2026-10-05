import { ChunkSummaryPanel } from '../chunking/ChunkSummaryPanel';
import { EmbeddingSummaryPanel } from '../embedding/EmbeddingSummaryPanel';
import { useEffect, useState, type FormEvent } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { Document, Metadata, Page, Version } from '../../types/documents';
import { date, Pagination, size } from '../../components/DocumentWidgets';
import { AccessGate, useSession } from '../library/Session';
import { label, MetadataFields, UploadForm } from '../library/UploadForm';
import { Icon } from '../navigation/icons';
import { Jobs } from '../operations/Jobs';
import { ParseSummaryPanel } from '../parsing/ParseSummaryPanel';
import { DeleteDocumentDialog } from './DeleteDocumentDialog';
import { ProcessingLifecycle } from './ProcessingLifecycle';
import { ReviewPanel } from './ReviewPanel';
import { useLifecycle } from './useLifecycle';
import { versionActionsSettled } from './lifecycle';
export function DocumentDetails() {
  const { id } = useParams();
  return <><p className="eyebrow">SOURCE PROVENANCE</p><h1>Document details</h1><AccessGate><Detail id={id!} /></AccessGate></>;
}
/**
 * One document, in the order a person needs it: what it is, whether it can be used yet, what (if
 * anything) needs doing, then its versions — and only after that the run-level detail an engineer
 * wants when something has gone wrong, folded away until asked for.
 */
function Detail({ id }: { id: string }) {
  const { token, identity } = useSession();
  const queries = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<Metadata | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const lifecycle = useLifecycle(id);
  // Only the lifecycle polls; these follow it, so a stage change refreshes the panels below once.
  const moving = lifecycle.data ? !lifecycle.data.view.terminal : false;
  const doc = useQuery({ queryKey: ['document', id], queryFn: () => api<Document>(token, '/documents/' + id), refetchInterval: moving ? 15000 : false });
  const versions = useQuery({ queryKey: ['document', id, 'versions', offset], queryFn: () => api<Page<Version>>(token, '/documents/' + id + '/versions?offset=' + offset), refetchInterval: moving ? 15000 : false });
  const stage = lifecycle.data?.view.current_stage;
  const state = lifecycle.data?.view.state;
  useEffect(() => {
    if (stage === undefined) return;
    void queries.invalidateQueries({ queryKey: ['document', id, 'versions'] });
    void queries.invalidateQueries({ queryKey: ['parse', id] });
    void queries.invalidateQueries({ queryKey: ['jobs'] });
  }, [stage, state, id, queries]);
  const loaded = Boolean(lifecycle.data);
  useEffect(() => {
    if (loaded && location.hash === '#lifecycle') document.getElementById('lifecycle')?.scrollIntoView();
  }, [loaded, location.hash]);
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError('');
    try { await api(token, '/documents/' + id, { method: 'PATCH', body: JSON.stringify(editing) }); setEditing(null); await queries.invalidateQueries({ queryKey: ['document', id] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save metadata.'); } finally { setSaving(false); }
  }
  async function download(version: Version) {
    try {
      const response = await fetch(`/api/v1/documents/${id}/versions/${version.id}/source`, { headers: { Authorization: 'Bearer ' + token } });
      if (!response.ok) throw new Error('The original file is unavailable.');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = version.normalized_filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Download failed.'); }
  }
  if (doc.isPending) return <p>Loading document…</p>;
  if (doc.isError) return <p role="alert">Document unavailable or access denied.</p>;
  const value = doc.data;
  const view = lifecycle.data?.view;
  const canDelete = Boolean(identity?.permissions.includes('document:delete'));
  const canAddVersion = Boolean(identity?.permissions.includes('document:upload')) && !value.archived_at;
  // A new version and permanent deletion are refused by the server while the document is being
  // processed, so they are not offered then. If the state cannot be read, they stay offered and the
  // server decides.
  const settled = lifecycle.isError || versionActionsSettled(view?.state);
  return <>
    {/* 1. Overview */}
    <section className="panel"><div className="section-heading"><h2>{value.title}</h2><span>{value.archived_at ? 'Archived' : 'Active publication'}</span></div>
    {error && <p role="alert" className="error">{error}</p>}
    {editing ? <form onSubmit={save}><fieldset disabled={saving}><MetadataFields value={editing} change={setEditing} /><div className="actions">
      <button>Save metadata</button><button type="button" className="secondary" onClick={() => setEditing(null)}>Discard edit</button></div></fieldset></form> : <>
      <p>{value.description || 'No description provided.'}</p><dl className="detail-grid">
        <div><dt>Source</dt><dd>{label(value.source_type)}</dd></div><div><dt>Authority</dt><dd>{label(value.authority_level)}</dd></div>
        <div><dt>Publisher</dt><dd>{value.publisher || '—'}</dd></div><div><dt>Specialty / subject</dt><dd>{value.specialty || '—'} / {value.subject || '—'}</dd></div>
        <div><dt>Uploaded</dt><dd>{date(value.created_at)}</dd></div><div><dt>Uploader ID</dt><dd className="mono">{value.created_by_user_id}</dd></div></dl>
      {!value.archived_at && identity?.permissions.includes('document:manage') && <button className="secondary" onClick={() => setEditing({
        title: value.title, source_type: value.source_type, authority_level: value.authority_level,
        publisher: value.publisher, specialty: value.specialty, subject: value.subject, description: value.description,
      })}>Edit metadata</button>}</>}</section>

    {/* 2–4. Readiness and processing, then anything that needs a person. */}
    <div id="lifecycle" className="lifecycle-anchor">
      {lifecycle.isPending ? <section className="panel"><p>Loading processing state…</p></section>
        : lifecycle.isError ? <section className="panel"><p role="alert">Processing state is unavailable.</p></section>
          : <>
            <ProcessingLifecycle lifecycle={lifecycle.data.view} receivedAt={lifecycle.data.receivedAt} />
            {view?.state === 'FAILED' && view.failure && <section className="panel failure-panel" aria-labelledby="failure-title">
              <h2 id="failure-title"><Icon name="failed" />Why processing failed</h2>
              <p>{view.failure.message || 'The pipeline stopped with an error.'}</p>
              <p className="muted">Nothing from this version was indexed. A curator can retry or reprocess it from the ingestion diagnostics below.</p>
            </section>}
            {view?.review && <ReviewPanel lifecycle={view} />}
          </>}
      {!settled && view?.state === 'PROCESSING' && (canAddVersion || canDelete) &&
        <p className="muted version-note">Version management will be available when processing finishes.</p>}
    </div>

    {/* 5. Versions, with their run-level findings folded away. */}
    <section className="panel"><h2>Original file versions</h2>
      {versions.isPending ? <p>Loading versions…</p> : versions.isError ? <p role="alert">Versions unavailable.</p> : <>
        {versions.data.items.map(version => <article className="version-card" key={version.id}><div className="section-heading">
          <h3>Version {version.version_number} · {version.edition || 'Edition unspecified'}</h3></div>
          <p>{version.original_filename} · {size(version.file_size_bytes)} · {version.publication_year || 'Year unspecified'}</p>
          <p>Uploaded {date(version.created_at)}</p><p className="mono checksum">SHA-256: {version.sha256}</p>
          <button className="secondary" onClick={() => void download(version)}>Download original</button>
          {/* 6. Findings and technical details. */}
          <details className="technical">
            <summary>Findings and technical details</summary>
            <ParseSummaryPanel documentId={id} versionId={version.id} /><ChunkSummaryPanel documentId={id} versionId={version.id} /><EmbeddingSummaryPanel documentId={id} versionId={version.id} />
          </details></article>)}
        <Pagination offset={offset} total={versions.data.total} onChange={setOffset} /></>}
    </section>{!value.archived_at && settled && <UploadForm documentId={id} />}

    {/* 7. Job diagnostics. */}
    <details className="technical diagnostics">
      <summary>Ingestion diagnostics</summary>
      <Jobs documentId={id} />
    </details>

    {canDelete && settled && <section className="panel danger-zone" aria-labelledby="danger-title">
      <h2 id="danger-title">Delete permanently</h2>
      <p>Erase this document, every version, its extracted content and its search entries. This
        cannot be undone. To stop using it but keep it, archive it instead.</p>
      <button type="button" className="danger" onClick={() => setDeleting(true)}>
        <Icon name="delete" small />Delete permanently…
      </button>
    </section>}
    {deleting && <DeleteDocumentDialog documentId={id} title={value.title}
      onClose={() => setDeleting(false)}
      onDeleted={() => navigate('/library', { replace: true, state: { deleted: value.title } })} />}
  </>;
}
