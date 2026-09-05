import { useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, uploadPdf } from '../../api/client';
import { StatusBadge } from '../../components/DocumentWidgets';
import type { Authority, Metadata, SourceType, UploadResult } from '../../types/documents';
import { useSession } from './Session';
export const sources: SourceType[] = ['GUIDELINE', 'REFERENCE_BOOK', 'TEXTBOOK', 'COURSE_MATERIAL', 'QUESTION_BANK', 'QUESTION_PAPER', 'ANSWER_KEY', 'OTHER'];
export const authorities: Authority[] = ['UNREVIEWED', 'ASSESSMENT', 'REFERENCE', 'HIGH'];
export const label = (value: string) => value.toLowerCase().replaceAll('_', ' ');
const empty: Metadata = { title: '', source_type: 'TEXTBOOK', authority_level: 'UNREVIEWED', publisher: '', specialty: '', subject: '', description: '' };
export function MetadataFields({ value, change }: { value: Metadata; change: (value: Metadata) => void }) {
  const assessment = ['QUESTION_BANK', 'QUESTION_PAPER', 'ANSWER_KEY'].includes(value.source_type);
  return <div className="form-grid">
    <label>Title<input required maxLength={300} value={value.title} onChange={e => change({ ...value, title: e.target.value })} /></label>
    <label>Source type<select value={value.source_type} onChange={e => change({ ...value, source_type: e.target.value as SourceType, authority_level: 'UNREVIEWED' })}>
      {sources.map(type => <option key={type} value={type}>{label(type)}</option>)}</select></label>
    <label>Authority level<select value={value.authority_level} onChange={e => change({ ...value, authority_level: e.target.value as Authority })}>
      {authorities.filter(level => !assessment || ['UNREVIEWED', 'ASSESSMENT'].includes(level)).map(level => <option key={level} value={level}>{label(level)}</option>)}</select></label>
    <label>Publisher<input maxLength={200} value={value.publisher ?? ''} onChange={e => change({ ...value, publisher: e.target.value })} /></label>
    <label>Specialty<input maxLength={120} value={value.specialty ?? ''} onChange={e => change({ ...value, specialty: e.target.value })} /></label>
    <label>Subject<input maxLength={120} value={value.subject ?? ''} onChange={e => change({ ...value, subject: e.target.value })} /></label>
    <label className="wide">Description<textarea maxLength={4000} rows={2} value={value.description ?? ''} onChange={e => change({ ...value, description: e.target.value })} /></label>
  </div>;
}
export function UploadForm({ documentId }: { documentId?: string }) {
  const { token, identity } = useSession();
  const queries = useQueryClient();
  const [files, setFiles] = useState<File[]>([]);
  const [meta, setMeta] = useState<Metadata>(empty);
  const [edition, setEdition] = useState('');
  const [year, setYear] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [message, setMessage] = useState('');
  const [error, setError] = useState<ApiError | null>(null);
  const [completed, setCompleted] = useState<UploadResult[]>([]);
  const keys = useRef(new Map<string, string>());
  if (!identity?.permissions.includes('document:upload')) return null;
  function choose(selected: FileList | File[]) {
    setFiles(Array.from(selected)); setError(null); setMessage(''); setCompleted([]); keys.current.clear();
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(null);
    if (!files.length || (!documentId && !meta.title.trim())) { setError(new ApiError('INVALID_METADATA', 'Choose at least one PDF and enter a title.')); return; }
    if (files.some(file => !file.name.toLowerCase().endsWith('.pdf') || file.size === 0)) { setError(new ApiError('INVALID_FILE', 'Choose nonempty PDF files only.')); return; }
    setBusy(true); setCompleted([]);
    let activeSignature: string | undefined;
    try {
      for (const file of files) {
        setProgress(0); setMessage('Uploading ' + file.name);
        const metadata = { filename: file.name, edition: edition || null, publication_year: year ? Number(year) : null, ...(!documentId ? { document: meta } : {}) };
        const signature = JSON.stringify(metadata) + file.name + file.size + file.lastModified;
        activeSignature = signature;
        const key = keys.current.get(signature) ?? crypto.randomUUID(); keys.current.set(signature, key);
        const result = await uploadPdf<UploadResult>(token, file, metadata, key, percent => {
          setProgress(percent); if (percent === 100) setMessage('Upload transferred. Validating and storing the PDF…');
        }, documentId);
        setCompleted(current => [...current, result]);
        for (const queryKey of [['documents'], ['document'], ['jobs']]) await queries.invalidateQueries({ queryKey });
      }
      setMessage('Upload recorded. Current ingestion status is shown below.');
    } catch (reason) {
      const failure = reason instanceof ApiError ? reason : new ApiError('UPLOAD_FAILED', 'The upload failed.');
      if (activeSignature && failure.details.retry_with_new_key === 'true') keys.current.delete(activeSignature);
      setMessage(''); setError(failure);
    }
    finally { setBusy(false); setProgress(null); }
  }
  return <section className="panel upload-panel"><h2>{documentId ? 'Add a new file version' : 'Upload source documents'}</h2>
    <p>PDF originals only. Shared metadata applies to all selected files.</p><form onSubmit={submit}><fieldset disabled={busy}>
      <div className="drop-zone" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!busy) choose(e.dataTransfer.files); }}>
        <label htmlFor={documentId ? 'version-files' : 'upload-files'}>Choose PDF files or drag them here</label>
        <input id={documentId ? 'version-files' : 'upload-files'} type="file" accept=".pdf,application/pdf" multiple onChange={e => choose(e.target.files ?? [])} />
        {files.length > 0 && <ul>{files.map((file, index) => <li key={index}>{file.name}</li>)}</ul>}
      </div>
      {!documentId && <MetadataFields value={meta} change={setMeta} />}
      <div className="form-grid"><label>Edition<input maxLength={120} value={edition} onChange={e => setEdition(e.target.value)} /></label>
        <label>Publication year<input type="number" min={1400} max={2200} value={year} onChange={e => setYear(e.target.value)} /></label></div>
      <button type="submit">{busy ? 'Uploading…' : documentId ? 'Upload new version' : 'Upload documents'}</button>
    </fieldset>
    {busy && progress !== null && <div className="transfer-progress"><label htmlFor="transfer">File transfer: {progress}%</label><progress id="transfer" max={100} value={progress} /></div>}
    <div aria-live="polite">{message && <p role="status">{message}</p>}</div>
    {error && <div role="alert" className="error"><strong>{error.code === 'UPLOAD_DUPLICATE' ? 'Duplicate file. ' : ''}</strong>{error.message}
      {error.details.document_id && <p><Link to={'/documents/' + error.details.document_id}>View existing document</Link></p>}</div>}
    {completed.length > 0 && <ul className="success-list">{completed.map(result => <li key={result.version_id}><StatusBadge status={result.status} />{' '}
      <Link to={'/documents/' + result.document_id}>Open uploaded document</Link></li>)}</ul>}
    </form></section>;
}
