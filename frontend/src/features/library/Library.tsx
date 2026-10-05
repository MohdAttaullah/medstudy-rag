import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { Document, Page } from '../../types/documents';
import { date, Pagination } from '../../components/DocumentWidgets';
import { DeleteDocumentDialog } from '../documents/DeleteDocumentDialog';
import { LifecycleChip } from '../documents/LifecycleChip';
import { RowMenu, type RowMenuItem } from '../documents/RowMenu';
import { POLL_MS, useNow } from '../documents/useLifecycle';
import { versionActionsSettled } from '../documents/lifecycle';
import { AccessGate, useSession } from './Session';
import { label, sources, UploadForm } from './UploadForm';
export function Library() {
  return <><p className="eyebrow">SOURCE LIBRARY</p><h1>Your source documents</h1>
    <p className="intro">Upload educational references and follow them until they are ready for Ask. A document is searchable only once it shows Ready.</p>
    <AccessGate><LibraryContent /></AccessGate></>;
}
/** Still changing on its own, so worth asking about again. Everything else waits for a person. */
const moving = (doc: Document) => doc.lifecycle?.state === 'PROCESSING' || doc.lifecycle?.state === 'DELETING';
function LibraryContent() {
  const { token, identity } = useSession();
  const queries = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [source, setSource] = useState('');
  const [archived, setArchived] = useState(false);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState('');
  const [deleting, setDeleting] = useState<Document | null>(null);
  // A deletion confirmed on the document's own page lands here, where the document no longer is.
  const arrived = (useLocation().state as { deleted?: string } | null)?.deleted;
  const [notice, setNotice] = useState(arrived ? `“${arrived}” was permanently deleted.` : '');
  const docs = useQuery({ queryKey: ['documents', offset, source, archived], queryFn: () =>
    api<Page<Document>>(token, `/documents?offset=${offset}&archived=${archived}${source ? '&source_type=' + source : ''}`),
  refetchInterval: query => (query.state.data?.items.some(moving) ? POLL_MS : false) });
  const now = useNow(Boolean(docs.data?.items.some(moving)));
  const can = (permission: string) => Boolean(identity?.permissions.includes(permission));
  async function archive(id: string) {
    setError(''); setNotice(''); setBusyId(id);
    try { await api(token, '/documents/' + id + '/archive', { method: 'POST' }); await queries.invalidateQueries({ queryKey: ['documents'] }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Archive failed.'); } finally { setBusyId(''); }
  }
  function menu(doc: Document): RowMenuItem[] {
    const items: RowMenuItem[] = [];
    if (!doc.archived_at && can('document:manage')) items.push({ label: 'Archive', disabled: busyId === doc.id, onSelect: () => void archive(doc.id) });
    // Not offered while the document is being processed: the server would refuse it.
    if (can('document:delete') && (!doc.lifecycle || versionActionsSettled(doc.lifecycle.state))) items.push({ label: 'Delete permanently', danger: true, onSelect: () => { setNotice(''); setDeleting(doc); } });
    return items;
  }
  return <><UploadForm /><section className="panel"><div className="section-heading"><h2>Documents</h2><div className="filters">
    <label>Source filter<select value={source} onChange={e => { setSource(e.target.value); setOffset(0); }}><option value="">All sources</option>
      {sources.map(type => <option key={type} value={type}>{label(type)}</option>)}</select></label>
    <label className="checkbox-label"><input type="checkbox" checked={archived} onChange={e => { setArchived(e.target.checked); setOffset(0); }} /> Archived</label>
    </div></div>{error && <p role="alert" className="error">{error}</p>}
    <p aria-live="polite" className={notice ? 'success-note' : 'visually-hidden'}>{notice}</p>
    {docs.isPending ? <p>Loading documents…</p> : docs.isError ? <p role="alert">Could not load the document list.</p> : <>
      {docs.data.items.length === 0 ? <p>No documents in this view.</p> : <div className="table-scroll"><table className="library-table">
        <thead><tr><th>Document</th><th>Source / authority</th><th>Version</th><th>Status</th><th>Uploaded</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
        <tbody>{docs.data.items.map(doc => <tr key={doc.id}><td><Link to={'/documents/' + doc.id}>{doc.title}</Link><small>{doc.subject || 'No subject set'}</small></td>
          <td>{label(doc.source_type)}<small>{label(doc.authority_level)}</small></td>
          <td>{doc.latest_version?.edition || 'Edition unspecified'}<small>Version {doc.latest_version?.version_number}</small></td>
          <td><LifecycleChip documentId={doc.id} title={doc.title} summary={doc.lifecycle} receivedAt={docs.dataUpdatedAt} now={now} /></td>
          <td>{date(doc.created_at)}</td>
          <td className="row-actions"><RowMenu label={`More actions for ${doc.title}`} items={menu(doc)} /></td></tr>)}</tbody></table></div>}
      <Pagination offset={offset} total={docs.data.total} onChange={setOffset} />
    </>}</section>
    {deleting && <DeleteDocumentDialog documentId={deleting.id} title={deleting.title}
      onClose={() => setDeleting(null)}
      onDeleted={() => { setNotice(`“${deleting.title}” was permanently deleted.`); setDeleting(null); }} />}</>;
}
