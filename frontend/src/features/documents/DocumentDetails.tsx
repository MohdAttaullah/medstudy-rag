import { useState, type FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { Document, Metadata, Page, Version } from '../../types/documents';
import { date, Pagination, size, StatusBadge } from '../../components/DocumentWidgets';
import { AccessGate, useSession } from '../library/Session';
import { label, MetadataFields, UploadForm } from '../library/UploadForm';
import { Jobs } from '../operations/Jobs';
export function DocumentDetails() {
  const { id } = useParams();
  return <><p className="eyebrow">SOURCE PROVENANCE</p><h1>Document details</h1><AccessGate><Detail id={id!} /></AccessGate></>;
}
function Detail({ id }: { id: string }) {
  const { token, identity } = useSession();
  const queries = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<Metadata | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const doc = useQuery({ queryKey: ['document', id], queryFn: () => api<Document>(token, '/documents/' + id), refetchInterval: 5000 });
  const versions = useQuery({ queryKey: ['document', id, 'versions', offset], queryFn: () => api<Page<Version>>(token, '/documents/' + id + '/versions?offset=' + offset), refetchInterval: 5000 });
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
  return <><section className="panel"><div className="section-heading"><h2>{value.title}</h2><span>{value.archived_at ? 'Archived' : 'Active publication'}</span></div>
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
    <section className="panel"><h2>Original file versions</h2>
      {versions.isPending ? <p>Loading versions…</p> : versions.isError ? <p role="alert">Versions unavailable.</p> : <>
        {versions.data.items.map(version => <article className="version-card" key={version.id}><div className="section-heading">
          <h3>Version {version.version_number} · {version.edition || 'Edition unspecified'}</h3><StatusBadge status={version.ingestion_status} /></div>
          <p>{version.original_filename} · {size(version.file_size_bytes)} · {version.publication_year || 'Year unspecified'}</p>
          <p>Uploaded {date(version.created_at)}</p><p className="mono checksum">SHA-256: {version.sha256}</p>
          <p className="muted">Original retained. Not searchable; parsing and indexing have not started.</p>
          <button className="secondary" onClick={() => void download(version)}>Download original</button></article>)}
        <Pagination offset={offset} total={versions.data.total} onChange={setOffset} /></>}
    </section>{!value.archived_at && <UploadForm documentId={id} />}<Jobs documentId={id} /></>;
}
