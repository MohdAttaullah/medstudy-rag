import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import { Icon } from '../navigation/icons';
import type { DeletionPreview } from '../../types/lifecycle';

/**
 * Permanent deletion, confirmed by typing a word rather than by one more click.
 *
 * The dialog says exactly what will go — counted by the server, not guessed — and what will not
 * come back. Archive stays the everyday way to take a document out of use; this is for content
 * that must not be kept at all. Answers that cited the document survive, but their citations
 * become "Source deleted" with no excerpt, because keeping the excerpt would be keeping the text.
 */

const WORD = 'DELETE';

const plural = (count: number, one: string, many = `${one}s`) =>
  `${count.toLocaleString()} ${count === 1 ? one : many}`;

export function DeleteDocumentDialog({ documentId, title, onClose, onDeleted }: {
  documentId: string;
  title: string;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const { token } = useSession();
  const queries = useQueryClient();
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const box = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const heading = useId();
  const description = useId();
  const preview = useQuery({
    queryKey: ['document', documentId, 'deletion-preview'],
    queryFn: () => api<DeletionPreview>(token, `/documents/${documentId}/deletion-preview`),
    staleTime: 0,
  });

  // Focus moves into the dialog, stays there, and goes back where it came from on close.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    field.current?.focus();
    return () => before?.focus?.();
  }, []);

  function keep(event: KeyboardEvent) {
    if (event.key === 'Escape' && !busy) { event.stopPropagation(); onClose(); return; }
    if (event.key !== 'Tab' || !box.current) return;
    const focusable = [...box.current.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    )];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  const blocked = preview.data?.blocked_reason === 'PROCESSING';
  const confirmed = typed === WORD;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!confirmed || blocked) return;
    setBusy(true); setError('');
    try {
      await api(token, `/documents/${documentId}`, {
        method: 'DELETE', body: JSON.stringify({ confirm: typed }),
      });
      queries.removeQueries({ queryKey: ['document', documentId] });
      await queries.invalidateQueries({ queryKey: ['documents'] });
      await queries.invalidateQueries({ queryKey: ['jobs'] });
      await queries.invalidateQueries({ queryKey: ['conversation'] });
      onDeleted();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The document could not be deleted.');
      setBusy(false);
    }
  }

  return <div className="dialog-scrim" onMouseDown={event => {
    if (event.target === event.currentTarget && !busy) onClose();
  }}>
    <div className="dialog danger-dialog" role="dialog" aria-modal="true" ref={box}
      aria-labelledby={heading} aria-describedby={description} onKeyDown={keep}>
      <div className="dialog-head">
        <span className="dialog-mark" aria-hidden="true"><Icon name="delete" /></span>
        <h2 id={heading}>Delete “{title}” permanently?</h2>
      </div>
      <p id={description}>
        This cannot be undone. The document will be removed from the library and from search, and
        its files and extracted content will be erased. To keep it but stop using it, archive it
        instead.
      </p>

      {preview.isPending ? <p className="muted">Counting what will be removed…</p>
        : preview.isError ? <p role="alert" className="error">{preview.error.message}</p>
          : <>
            <h3>What will be removed</h3>
            <ul className="delete-list">
              <li>The original {preview.data.versions === 1 ? 'file' : 'files'} — {plural(preview.data.versions, 'version')}</li>
              <li>Extracted text, tables and figures{preview.data.pages ? ` from ${plural(preview.data.pages, 'page')}` : ''}</li>
              <li>{plural(preview.data.chunks, 'searchable passage')} and {plural(preview.data.vectors, 'search vector')}</li>
              <li>The keyword index entries and processing history for this document</li>
            </ul>
            <h3>What stays</h3>
            <ul className="delete-list keep-list">
              {preview.data.citing_answers > 0
                ? <li>{plural(preview.data.citing_answers, 'earlier answer')} that cited this document
                  {' '}{preview.data.citing_answers === 1 ? 'stays' : 'stay'} in {preview.data.citing_answers === 1 ? 'its' : 'their'} conversation,
                  {' '}but {preview.data.citing_answers === 1 ? 'its citation shows' : 'their citations show'} “Source deleted”
                  {' '}and no longer quote{preview.data.citing_answers === 1 ? 's' : ''} it.</li>
                : <li>No earlier answers cite this document.</li>}
              <li>An audit record that a deletion happened — who, when and how much — without the
                document’s title or content.</li>
            </ul>
            {blocked && <p className="notice-danger" role="alert"><Icon name="failed" small />
              This document is still being processed. Cancel processing and wait for it to stop
              before deleting it.</p>}
          </>}

      <form onSubmit={submit}>
        <label htmlFor={`${heading}-confirm`}>Type <strong>{WORD}</strong> to confirm</label>
        <input id={`${heading}-confirm`} ref={field} value={typed} autoComplete="off"
          spellCheck={false} disabled={busy} onChange={event => setTyped(event.target.value)}
          aria-invalid={typed.length > 0 && !confirmed ? true : undefined} />
        {error && <p role="alert" className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="danger"
            disabled={!confirmed || busy || blocked || !preview.isSuccess}>
            {busy ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
      </form>
    </div>
  </div>;
}
