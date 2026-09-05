import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { date, StatusBadge } from '../../components/DocumentWidgets';
import type {
  ParseElement, ParseFigure, ParseFinding, ParseFormula, ParsePage, ParseRun, ParseTable,
} from '../../types/parsing';
import type { Page } from '../../types/documents';
import { RESULT_LABEL, SEVERITY_ORDER, findingSummary, label } from './labels';

/**
 * Development and curation view over one parse run.
 *
 * It shows what the parser actually produced for a chosen page: structured elements in reading
 * order, extracted tables with their real cells, figures with their stored crops, formulas as
 * written, and the validation findings for that page. Nothing is inferred in this view; a
 * relationship the parser did not report is shown as absent rather than guessed.
 */
export function ParseInspector() {
  const { id, versionId, runId } = useParams();
  return <>
    <p className="eyebrow">PARSE PROVENANCE</p>
    <h1>Parse inspector</h1>
    <AccessGate><Inspector documentId={id!} versionId={versionId!} runId={runId!} /></AccessGate>
  </>;
}

function useParse<T>(key: string, path: string, base: string, enabled = true) {
  const { token } = useSession();
  return useQuery({
    queryKey: ['parse-inspect', base, key, path],
    queryFn: () => api<T>(token, base + path),
    enabled,
  });
}

function Inspector({ documentId, versionId, runId }: { documentId: string; versionId: string; runId: string }) {
  const { token } = useSession();
  const base = `/documents/${documentId}/versions/${versionId}/parse-runs/${runId}`;
  const [pageId, setPageId] = useState('');
  const [preview, setPreview] = useState('');
  const run = useParse<ParseRun>('run', '', base);
  const pages = useParse<Page<ParsePage>>('pages', '/pages?limit=200', base);
  const findings = useParse<Page<ParseFinding>>('findings', '/findings?limit=200', base);

  useEffect(() => {
    if (!pageId && pages.data?.items.length) setPageId(pages.data.items[0].id);
  }, [pages.data, pageId]);

  const page = pages.data?.items.find(item => item.id === pageId);
  const number = page?.page_number;
  const detail = useParse<ParsePage>('page-detail', `/pages/${pageId}`, base, Boolean(pageId));
  const elements = useParse<Page<ParseElement>>(
    'elements', `/elements?limit=500&page_number=${number ?? 0}`, base, Boolean(number));
  const tables = useParse<Page<ParseTable>>(
    'tables', `/tables?page_number=${number ?? 0}`, base, Boolean(number));
  const figures = useParse<Page<ParseFigure>>(
    'figures', `/figures?page_number=${number ?? 0}`, base, Boolean(number));
  const formulas = useParse<Page<ParseFormula>>(
    'formulas', `/formulas?page_number=${number ?? 0}`, base, Boolean(number));

  // Previews and figure crops are private objects; fetch them with the session key, never a
  // public URL, and revoke the object URL when the selection changes.
  useEffect(() => {
    let revoked = '';
    setPreview('');
    if (!page?.has_preview) return;
    void (async () => {
      const response = await fetch(`/api/v1${base}/pages/${page.id}/preview`, {
        headers: { Authorization: 'Bearer ' + token },
      });
      if (!response.ok) return;
      revoked = URL.createObjectURL(await response.blob());
      setPreview(revoked);
    })();
    return () => { if (revoked) URL.revokeObjectURL(revoked); };
  }, [page, base, token]);

  if (run.isPending) return <p>Loading parse run…</p>;
  if (run.isError) return <p role="alert">Parse run unavailable or access denied.</p>;
  const value = run.data;
  const pageFindings = (findings.data?.items ?? []).filter(
    finding => finding.page_number === number || finding.scope === 'document');

  return <>
    <section className="panel">
      <div className="section-heading">
        <h2>Parse run {value.attempt}</h2>
        <StatusBadge status={value.status} />
      </div>
      <dl className="detail-grid">
        <div><dt>Parser</dt><dd>{value.parser_name} {value.parser_version}</dd></div>
        <div><dt>Configuration</dt><dd>{value.configuration_version}</dd></div>
        <div><dt>Validation</dt><dd>{value.validation_result ? RESULT_LABEL[value.validation_result] : 'Not completed'}</dd></div>
        <div><dt>Active dataset</dt><dd>{value.is_active ? 'Yes' : 'No — superseded or not accepted'}</dd></div>
        <div><dt>Pages / elements</dt><dd>{value.page_count ?? '—'} / {value.element_count ?? '—'}</dd></div>
        <div><dt>Tables / figures / formulas</dt><dd>{value.table_count ?? '—'} / {value.figure_count ?? '—'} / {value.formula_count ?? '—'}</dd></div>
        <div><dt>OCR pages</dt><dd>{value.ocr_page_count ?? '—'}{value.ocr_engine ? ` · ${value.ocr_engine}` : ''}</dd></div>
        <div><dt>Completed</dt><dd>{date(value.completed_at)}</dd></div>
      </dl>
      <p className="muted">{findingSummary(value.finding_counts)}</p>
      {value.error_code && <p role="alert" className="error">{value.error_code}: {value.error_message}</p>}
      <Link className="button-link secondary" to={`/documents/${documentId}`}>Back to document</Link>
    </section>

    <section className="panel">
      <div className="section-heading">
        <h2>Pages</h2>
        <label htmlFor="parse-page">Inspect page
          <select id="parse-page" value={pageId} onChange={event => setPageId(event.target.value)}>
            {(pages.data?.items ?? []).map(item =>
              <option key={item.id} value={item.id}>Page {item.page_number}</option>)}
          </select>
        </label>
      </div>
      {pages.isPending ? <p>Loading pages…</p> : !pages.data?.items.length ? <p>No pages were persisted for this run.</p> : !page ? null : <>
        <dl className="detail-grid">
          <div><dt>Page number</dt><dd>{page.page_number} (1-based, printed order)</dd></div>
          <div><dt>Size</dt><dd>{Math.round(page.width)} × {Math.round(page.height)} pt</dd></div>
          <div><dt>Elements</dt><dd>{page.element_count}</dd></div>
          <div><dt>Source text layer</dt><dd>{page.source_text_chars} characters</dd></div>
          <div><dt>OCR</dt><dd>{page.ocr_used ? `Used · ${page.ocr_evidence ?? 'evidence not recorded'}` : 'Not used'}</dd></div>
          <div><dt>Preview</dt><dd>{page.has_preview ? page.preview_media_type : 'Not generated'}</dd></div>
        </dl>
        <div className="parse-page">
          {preview
            ? <img className="page-preview" src={preview} alt={`Rendered preview of page ${page.page_number}`} />
            : <p className="muted">No page preview is stored for this page.</p>}
          <div className="parse-text">
            <h3>Normalized page text</h3>
            <pre>{detail.data?.extracted_text || 'No text was extracted from this page.'}</pre>
          </div>
        </div>
      </>}
    </section>

    <section className="panel">
      <h2>Structured elements</h2>
      {!elements.data?.items.length ? <p>No elements on this page.</p> :
        <ol className="element-list">
          {elements.data.items.map(element => <li key={element.id}>
            <div className="section-heading">
              <span className="state">{label(element.element_type)}</span>
              <span className="muted mono">#{element.reading_order} · depth {element.depth}{element.parent_element_id ? ' · nested' : ''}</span>
            </div>
            <p>{element.normalized_text || <span className="muted">No text (structural element).</span>}</p>
            {element.text_normalized && element.raw_text &&
              <details><summary>Original parser text</summary><pre>{element.raw_text}</pre></details>}
            <p className="muted mono">
              {element.bbox
                ? `bbox ${element.bbox.x1.toFixed(1)}, ${element.bbox.y1.toFixed(1)} → ${element.bbox.x2.toFixed(1)}, ${element.bbox.y2.toFixed(1)} (${element.bbox.origin}, pt)`
                : 'No page coordinates reported by the parser'}
              {element.source_label ? ` · parser label ${element.source_label}` : ''}
            </p>
          </li>)}
        </ol>}
    </section>

    <section className="panel">
      <h2>Tables</h2>
      {!tables.data?.items.length ? <p>No tables on this page.</p> :
        tables.data.items.map(table => <TableInspector key={table.id} base={base} table={table} />)}
    </section>

    <section className="panel">
      <h2>Figures</h2>
      {!figures.data?.items.length ? <p>No figures on this page.</p> :
        figures.data.items.map(figure => <FigureCard key={figure.id} base={base} figure={figure} />)}
    </section>

    <section className="panel">
      <h2>Formulas</h2>
      {!formulas.data?.items.length ? <p>No formulas on this page.</p> :
        <ul className="element-list">{formulas.data.items.map(formula => <li key={formula.id}>
          <pre>{formula.normalized_expression || 'No expression was recovered.'}</pre>
          <p className="muted">
            Notation: {formula.notation ?? 'not reported'} · related text
            {formula.preceding_element_id || formula.following_element_id ? ' linked' : ' not identified'}
          </p>
          <p className="muted">The expression is stored exactly as parsed; it is never rewritten or interpreted.</p>
        </li>)}</ul>}
    </section>

    <section className="panel">
      <h2>Validation findings</h2>
      {!pageFindings.length ? <p>No findings recorded for this page.</p> :
        <ul className="element-list">{[...pageFindings].sort(
          (a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)).map(finding =>
          <li key={finding.id}>
            <div className="section-heading">
              <span className={'state state-' + finding.severity.toLowerCase()}>{finding.severity.toLowerCase()}</span>
              <span className="muted mono">{finding.code}</span>
            </div>
            <p>{finding.message}</p>
            <p className="muted">Scope: {finding.scope}{finding.page_number ? ` · page ${finding.page_number}` : ''}</p>
          </li>)}</ul>}
    </section>
  </>;
}

function TableInspector({ base, table }: { base: string; table: ParseTable }) {
  const { token } = useSession();
  const detail = useQuery({
    queryKey: ['parse-inspect', base, 'table', table.id],
    queryFn: () => api<ParseTable>(token, `${base}/tables/${table.id}`),
  });
  const cells = detail.data?.cells ?? [];
  const grid: string[][] = Array.from({ length: table.row_count }, () => Array(table.column_count).fill(''));
  const headers = new Set<string>();
  for (const cell of cells) {
    if (grid[cell.row] && cell.column < table.column_count) grid[cell.row][cell.column] = cell.text;
    if (cell.column_header) headers.add(`${cell.row}:${cell.column}`);
  }
  return <article className="job-card">
    <div className="section-heading">
      <h3>{table.caption_text ?? 'Table without a parser-declared caption'}</h3>
      <span className="muted">{table.row_count} × {table.column_count}</span>
    </div>
    <p className="muted">
      Page {table.page_number ?? '—'} · {table.header_row_count} header row(s) · {cells.length} cells
      {table.possible_continuation ? ` · possible continuation (${table.continuation_evidence})` : ''}
      {table.malformed ? ' · structure flagged as malformed' : ''}
    </p>
    {detail.isPending ? <p>Loading cells…</p> : !cells.length ? <p role="alert">No cells were recovered for this table.</p> :
      <div className="table-scroll"><table className="parse-table"><tbody>
        {grid.map((row, rowIndex) => <tr key={rowIndex}>
          {row.map((text, columnIndex) => headers.has(`${rowIndex}:${columnIndex}`)
            ? <th key={columnIndex} scope="col">{text}</th>
            : <td key={columnIndex}>{text}</td>)}
        </tr>)}
      </tbody></table></div>}
    {!table.caption_element_id &&
      <p className="muted">No caption relationship was reported by the parser for this table.</p>}
  </article>;
}

function FigureCard({ base, figure }: { base: string; figure: ParseFigure }) {
  const { token } = useSession();
  const [source, setSource] = useState('');
  useEffect(() => {
    let revoked = '';
    if (!figure.has_image) return;
    void (async () => {
      const response = await fetch(`/api/v1${base}/figures/${figure.id}/image`, {
        headers: { Authorization: 'Bearer ' + token },
      });
      if (!response.ok) return;
      revoked = URL.createObjectURL(await response.blob());
      setSource(revoked);
    })();
    return () => { if (revoked) URL.revokeObjectURL(revoked); };
  }, [figure, base, token]);
  return <article className="job-card">
    <div className="section-heading">
      <h3>{figure.caption_text ?? 'Figure without a parser-declared caption'}</h3>
      <span className="muted">{figure.figure_kind ?? 'figure'}</span>
    </div>
    {source
      ? <img className="figure-crop" src={source} alt={figure.caption_text ?? 'Extracted figure from the source document'} />
      : <p className="muted">{figure.has_image ? 'Loading extracted image…' : 'No image artifact was stored for this figure.'}</p>}
    <p className="muted">
      Page {figure.page_number ?? '—'}
      {figure.image_width ? ` · ${figure.image_width} × ${figure.image_height} px` : ''}
      {figure.image_media_type ? ` · ${figure.image_media_type}` : ''}
    </p>
    <p className="muted">The original page remains the source of truth; this crop is provenance, not interpretation.</p>
  </article>;
}
