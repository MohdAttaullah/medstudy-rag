import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { Pagination } from '../../components/DocumentWidgets';
import { Icon, type IconName } from '../navigation/icons';
import { guidanceFor } from '../documents/lifecycle';
import { RemediationActions } from '../documents/RemediationActions';
import { useLifecycle } from '../documents/useLifecycle';
import {
  chunkState, isBlocking, measurement, readStatus, readTab, SEVERITY_WORD, STATE_WORD, TAB_HELP,
  type ChunkState, type StatusFilter, type Tab,
} from './review';
import type { Page } from '../../types/documents';
import type { ParseElement } from '../../types/parsing';
import type {
  Chunk, ChunkFinding, ChunkReviewSummary, ChunkRun, Finding, Question,
} from '../../types/chunking';
import type { FindingSeverity, Lifecycle } from '../../types/lifecycle';

type Cell = { row: number; column: number; text?: string; row_span?: number; col_span?: number };
/** Whether (row, column) lies inside a merged cell that starts elsewhere. */
export function covered(cells: Cell[], row: number, column: number) {
  return cells.some(c => (c.row !== row || c.column !== column)
    && c.row <= row && row < c.row + (c.row_span ?? 1)
    && c.column <= column && column < c.column + (c.col_span ?? 1));
}
/** Structured table chunks keep their canonical cells; the grid is rendered, never re-flowed. */
function TableView({ chunk }: { chunk: Chunk }) {
  const meta = chunk.chunk_metadata as {
    caption?: string; headers?: string; part_number?: number; part_count?: number;
    row_indexes?: number[]; header_rows?: number[]; cells?: Cell[]; possible_continuation?: boolean;
    carried_cells?: Cell[]; row_fragment?: { number: number; count: number };
  };
  const cells = meta.cells ?? [];
  if (!cells.length) return null;
  const rows = [...new Set(cells.map(c => c.row))].sort((a, b) => a - b);
  const columns = Math.max(...cells.map(c => c.column + (c.col_span ?? 1)));
  const headers = new Set(meta.header_rows ?? []);
  return <><h3>Source table</h3>
    {meta.caption && <p>{meta.caption}</p>}
    <p className="muted">Part {meta.part_number ?? 1} of {meta.part_count ?? 1} · source rows {(meta.row_indexes ?? []).join(', ') || 'header only'} · repeated header rows {[...headers].join(', ') || 'none declared'}</p>
    {meta.row_fragment && <p className="muted">This row was too long for one passage: fragment {meta.row_fragment.number} of {meta.row_fragment.count}, each repeating the row’s name and the column headers.</p>}
    {!!meta.carried_cells?.length && <p className="muted">
      {meta.carried_cells.length === 1 ? 'A merged cell' : `${meta.carried_cells.length} merged cells`} from
      {' '}an earlier row {meta.carried_cells.length === 1 ? 'also applies' : 'also apply'} to these rows,
      so {meta.carried_cells.length === 1 ? 'it is' : 'they are'} repeated once at the top of this passage.
    </p>}
    {meta.possible_continuation && <p className="muted">The parser flagged this table as a possible continuation. Parts were not merged.</p>}
    <div className="table-scroll"><table className="parse-table"><tbody>
      {rows.map(row => <tr key={row}>{Array.from({ length: columns }, (_, column) => {
        const cell = cells.find(c => c.row === row && c.column === column);
        // A position inside an earlier merged cell is drawn by that cell. An empty cell here would
        // push every later value one column to the right.
        if (!cell && covered(cells, row, column)) return null;
        if (!cell) return <td key={column} />;
        const Tag = headers.has(row) ? 'th' : 'td';
        return <Tag key={column} rowSpan={cell.row_span ?? 1} colSpan={cell.col_span ?? 1}>{cell.text ?? ''}</Tag>;
      })}</tr>)}
    </tbody></table></div></>;
}
function ArtifactLinks({ chunk }: { chunk: Chunk }) {
  const links = (chunk.artifacts ?? []).flatMap(a => ([['Table', a.table_id], ['Figure', a.figure_id], ['Formula', a.formula_id]] as const).filter(([, id]) => id));
  if (!links.length) return <p className="muted">No structured source artifact is linked to this chunk.</p>;
  return <><h3>Source artifact relationships</h3><ul>{links.map(([label, id]) => <li key={id!} className="mono checksum">{label} artifact {id}</li>)}</ul></>;
}

type Source = { element: ParseElement; position: number; start_offset: number; end_offset: number; role: string };
function useData<T>(path: string, enabled = true) {
  const { token } = useSession();
  return useQuery({ queryKey: ['chunks', path], queryFn: () => api<T>(token, path), enabled });
}

const STATE_ICON: Record<ChunkState, IconName> = { blocking: 'failed', warning: 'conflict', clean: 'stage-skipped' };

function StateBadge({ state }: { state: ChunkState }) {
  return <span className={`chunk-state chunk-state-${state}`}>
    <Icon name={STATE_ICON[state]} small />{STATE_WORD[state]}
  </span>;
}

function FindingLine({ finding, chunk, run }: {
  finding: ChunkFinding | Finding; chunk: Chunk | null; run: ChunkRun;
}) {
  const blocking = isBlocking(finding.severity);
  const sized = measurement(finding, chunk, run);
  return <p className={`finding-line ${blocking ? 'finding-blocking' : 'finding-warning'}`}>
    <span className="finding-sev">{SEVERITY_WORD[finding.severity] ?? finding.severity}</span>
    <span className="mono">{finding.code}</span>
    {sized && <span className="finding-measure">{sized}</span>}
  </p>;
}

export function ChunkInspector() {
  const { runId } = useParams();
  return <><p className="eyebrow">CHUNK PROVENANCE</p><h1>Chunk inspector</h1><AccessGate><Inspector runId={runId!} /></AccessGate></>;
}

function Inspector({ runId }: { runId: string }) {
  // Where the reviewer is lives in the URL, so a link from "Review required" lands on the right
  // view with the right chunk open, and a reload keeps it.
  const [search, setSearch] = useSearchParams();
  const selected = search.get('chunk') ?? '';
  const status = readStatus(search.get('status'));
  const [offset, setOffset] = useState(0), [kind, setKind] = useState(''), [page, setPage] = useState('');
  const [parent, setParent] = useState(''), [question, setQuestion] = useState('');
  const run = useData<ChunkRun>(`/chunk-runs/${runId}`);
  const summary = useData<ChunkReviewSummary>(`/chunk-runs/${runId}/review-summary`);
  const needsReview = run.data?.validation_result === 'NEEDS_REVIEW' || run.data?.validation_result === 'FAIL';
  // A stopped dataset opens on its findings unless a chunk or view was asked for.
  const tab: Tab = readTab(search.get('tab')) ?? (selected ? 'chunks' : needsReview ? 'findings' : 'chunks');

  function update(changes: Record<string, string | null>) {
    const next = new URLSearchParams(search);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === '') next.delete(key); else next.set(key, value);
    }
    setSearch(next, { replace: true });
  }
  const select = (id: string) => update({ chunk: id || null });
  const show = (next: Tab) => { update({ tab: next }); setOffset(0); };

  const query = new URLSearchParams({ offset: String(offset), limit: '20' });
  if (kind) query.set('chunk_type', kind); if (page) query.set('page_number', page);
  if (parent) query.set('parent_id', parent); if (question) query.set('question_id', question);
  if (status !== 'all') query.set('status', status);
  const chunks = useData<Page<Chunk>>(`/chunk-runs/${runId}/chunks?${query}`, tab === 'chunks');
  const questions = useData<Page<Question>>(`/chunk-runs/${runId}/questions?offset=${offset}`, tab === 'questions');
  const findings = useData<Page<Finding>>(`/chunk-runs/${runId}/validation-findings?offset=${offset}&limit=100`, tab === 'findings');
  const lifecycle = useLifecycle(run.data?.document_id ?? '');
  if (run.isPending) return <p>Loading chunk run...</p>;
  if (run.isError) return <p role="alert">Chunk run unavailable or access denied.</p>;
  const value = run.data;
  const counts = summary.data;
  // Remediation applies only to the dataset the document is actually stopped on.
  const review = lifecycle.data?.view.review?.run_id === runId ? lifecycle.data.view : null;
  const current = lifecycle.data?.view ?? null;

  return <><Link to={`/documents/${value.document_id}#lifecycle`}>Back to document</Link>
    <RunHeader run={value} counts={counts} onReview={() => { update({ tab: 'findings' }); setOffset(0); }} />
    <Tabs tab={tab} counts={counts} onChange={show} />
    <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="inspector-panel">
      <p className="tab-help">{TAB_HELP[tab].help}</p>
      {selected && tab === 'chunks' && <Detail key={selected} chunkId={selected} run={value}
        select={select} review={review} current={current} toFindings={() => show('findings')} />}
      {tab === 'chunks' && <section className="panel" aria-label="Chunk list"><div className="actions">
        <StatusFilterBar status={status} counts={counts}
          onChange={next => { update({ status: next === 'all' ? null : next }); setOffset(0); }} />
        <label>Chunk type<select value={kind} onChange={e => { setKind(e.target.value); setOffset(0); }}><option value="">All types</option>{['TEXT_PARENT', 'TEXT_CHILD', 'LIST', 'TABLE', 'TABLE_PART', 'FORMULA', 'FIGURE_CONTEXT', 'QUESTION', 'QUESTION_EXPLANATION', 'OTHER_STRUCTURED'].map(t => <option key={t}>{t}</option>)}</select></label>
        <label>Source page<input type="number" min="1" value={page} onChange={e => { setPage(e.target.value); setOffset(0); }} /></label>
        {(parent || question) && <button className="secondary" onClick={() => { setParent(''); setQuestion(''); setOffset(0); }}>Clear relationship filter</button>}
      </div>
        {chunks.isPending ? <p>Loading chunks...</p> : chunks.isError ? <p role="alert">Chunks unavailable.</p> : <>
          {!chunks.data.items.length && <p>No chunks match this view.</p>}
          <ul className="chunk-list">{chunks.data.items.map(c => {
            const state = chunkState(c);
            const current = c.id === selected;
            return <li key={c.id} className={`chunk-card chunk-${state}${current ? ' chunk-selected' : ''}`}
              aria-current={current ? 'true' : undefined}>
              <div className="chunk-card-head">
                <h3>#{c.sequence_number} {c.chunk_type}</h3>
                <StateBadge state={state} />
              </div>
              <p className="muted">Pages {c.page_start ?? 'unlocated'}–{c.page_end ?? 'unlocated'} · {c.token_count} source tokens · {c.retrieval_token_count} retrieval tokens</p>
              {(c.findings ?? []).map((f, i) => <FindingLine key={i} finding={f} chunk={c} run={value} />)}
              <p className="chunk-preview">{c.normalized_text.slice(0, 320) || 'Visual source without text'}</p>
              <div className="actions"><button className="secondary" onClick={() => select(c.id)}>Inspect chunk {c.sequence_number}</button>
                {c.parent_chunk_id && <button className="secondary" onClick={() => select(c.parent_chunk_id!)}>Inspect parent</button>}
                {['TEXT_PARENT', 'QUESTION'].includes(c.chunk_type) && <button className="secondary" onClick={() => { setParent(c.id); setOffset(0); }}>Show children</button>}</div>
            </li>;
          })}</ul><Pagination offset={offset} total={chunks.data.total} onChange={setOffset} />
        </>}
      </section>}
      {tab === 'questions' && <section className="panel" aria-label="Question list">
        {questions.isPending ? <p>Loading questions...</p> : questions.isError ? <p role="alert">Questions unavailable.</p> : <>
          {!questions.data.items.length && <p>No question objects were extracted from this document.</p>}
          {questions.data.items.map(q => <article key={q.id} className="version-card"><h3>{q.question_number}. {q.question_text}</h3><p>{q.question_type} / {q.extraction_status}</p>
            <ol>{q.options.map(o => <li key={o.ordinal}>{o.label}. {o.text}</li>)}</ol>
            <p><strong>Explicit source answer:</strong> <span>{q.explicit_answer ?? 'Absent; no answer inferred'}</span></p>
            <p className="chunk-text">{q.explanation ?? 'No source explanation'}</p><p>Authority: {String(q.authority.authority_level)}</p>
            <button className="secondary" onClick={() => { setQuestion(q.id); setParent(''); show('chunks'); }}>Inspect question provenance</button>
          </article>)}<Pagination offset={offset} total={questions.data.total} onChange={setOffset} />
        </>}
      </section>}
      {tab === 'findings' && <FindingsView findings={findings.data} pending={findings.isPending}
        failed={findings.isError} run={value} review={review} current={current} page={setOffset}
        open={(chunkId, blocking) => {
          update({ tab: 'chunks', chunk: chunkId, status: blocking ? 'blocking' : 'warning' });
          setOffset(0);
        }} />}
    </div>
  </>;
}

function RunHeader({ run, counts, onReview }: {
  run: ChunkRun; counts: ChunkReviewSummary | undefined; onReview: () => void;
}) {
  const result = run.validation_result;
  const tone = result === 'PASS' ? 'success' : result === 'PASS_WITH_WARNINGS' ? 'success'
    : result === 'NEEDS_REVIEW' ? 'review' : result === 'FAIL' ? 'danger' : 'neutral';
  const title = result === 'PASS' ? 'Validated'
    : result === 'PASS_WITH_WARNINGS' ? 'Validated with warnings'
      : result === 'NEEDS_REVIEW' ? 'Stopped for review' : result === 'FAIL' ? 'Validation failed'
        : 'Validation pending';
  const icon: IconName = tone === 'success' ? 'verified' : tone === 'review' ? 'conflict'
    : tone === 'danger' ? 'failed' : 'info';
  return <section className={`panel run-header tone-${tone}`} aria-labelledby="run-title">
    <div className="run-header-head">
      <span className="lifecycle-mark" aria-hidden="true"><Icon name={icon} /></span>
      <div>
        <h2 id="run-title">{title}</h2>
        <p className="muted">{run.is_active ? 'This is the active passage set.' : 'This passage set is not active.'}
          {' '}Chunker {run.chunker_name} {run.chunker_version} · {run.tokenizer_name}</p>
      </div>
    </div>
    {result === 'NEEDS_REVIEW' && counts && <div className="run-review">
      <p>
        {counts.blocking_findings === 1 ? 'One blocking finding stops' : `${counts.blocking_findings} blocking findings stop`}
        {' '}these passages from being embedded and searched.
        {counts.warning_findings > 0 && ` ${counts.warning_findings} ${counts.warning_findings === 1 ? 'warning does' : 'warnings do'} not.`}
      </p>
      <button type="button" onClick={onReview}><Icon name="failed" small />Start with the blocking findings</button>
    </div>}
    {run.error_code && <p role="alert" className="error">{run.error_code}: {run.error_message}</p>}
    <details><summary>Policy, metrics and reproducibility</summary>
      <dl className="detail-grid"><div><dt>Policy</dt><dd>{run.configuration_version}</dd></div>
        <div><dt>Policy fingerprint</dt><dd className="mono checksum">{run.policy_fingerprint}</dd></div></dl>
      <pre className="chunk-text">{JSON.stringify({ metrics: run.metrics, input: run.input_fingerprint, tokenizer_version: run.tokenizer_version }, null, 2)}</pre>
    </details>
    <Link to={`/documents/${run.document_id}/versions/${run.document_version_id}/parse/${run.parse_run_id}`}>Open source parse</Link>
  </section>;
}

/** Three views with counts, and the one that needs attention says so in words and an icon. */
function Tabs({ tab, counts, onChange }: {
  tab: Tab; counts: ChunkReviewSummary | undefined; onChange: (tab: Tab) => void;
}) {
  const order: Tab[] = ['chunks', 'questions', 'findings'];
  const blockers = counts?.blocking_findings ?? 0;
  function key(event: KeyboardEvent<HTMLButtonElement>) {
    const at = order.indexOf(tab);
    const next = event.key === 'ArrowRight' ? order[(at + 1) % order.length]
      : event.key === 'ArrowLeft' ? order[(at + order.length - 1) % order.length]
        : event.key === 'Home' ? order[0] : event.key === 'End' ? order[order.length - 1] : null;
    if (!next) return;
    event.preventDefault();
    onChange(next);
    document.getElementById(`tab-${next}`)?.focus();
  }
  const count = (t: Tab) => (counts ? t === 'chunks' ? counts.chunks : t === 'questions' ? counts.questions : counts.findings : null);
  return <div className="inspector-tabs" role="tablist" aria-label="Inspector views">
    {order.map(t => {
      const attention = t === 'findings' && blockers > 0;
      const n = count(t);
      return <button key={t} id={`tab-${t}`} type="button" role="tab" aria-selected={tab === t}
        aria-controls={`panel-${t}`} tabIndex={tab === t ? 0 : -1} onKeyDown={key}
        className={`inspector-tab${attention ? ' tab-attention' : ''}`} onClick={() => onChange(t)}>
        <span>{TAB_HELP[t].label}</span>
        {n !== null && <span className="tab-count">{n}</span>}
        {attention && <span className="tab-flag">
          <Icon name="failed" small />{blockers} blocking{tab !== 'findings' ? ' · start here' : ''}
        </span>}
        {t === 'findings' && counts && counts.warning_findings > 0 && <span className="tab-sub">
          {counts.warning_findings} {counts.warning_findings === 1 ? 'warning' : 'warnings'}
        </span>}
      </button>;
    })}
  </div>;
}

function StatusFilterBar({ status, counts, onChange }: {
  status: StatusFilter; counts: ChunkReviewSummary | undefined; onChange: (status: StatusFilter) => void;
}) {
  const options: [StatusFilter, string, number | undefined][] = [
    ['all', 'All', counts?.chunks],
    ['blocking', 'Blocking', counts?.blocking_chunks],
    ['warning', 'Warnings', counts?.warning_chunks],
    ['clean', 'No findings', counts?.clean_chunks],
  ];
  return <fieldset className="status-filter">
    <legend>Show</legend>
    {options.map(([value, label, n]) => <label key={value} className={`status-option status-${value}${status === value || (value === 'all' && status === 'findings') ? ' is-on' : ''}`}>
      <input type="radio" name="chunk-status" value={value} checked={status === value}
        onChange={() => onChange(value)} />
      {label}{n !== undefined && <span className="tab-count">{n}</span>}
    </label>)}
  </fieldset>;
}

function FindingsView({ findings, pending, failed, run, review, current, open, page }: {
  findings: Page<Finding> | undefined; pending: boolean; failed: boolean; run: ChunkRun;
  review: Lifecycle | null; current: Lifecycle | null; open: (chunkId: string, blocking: boolean) => void;
  page: (offset: number) => void;
}) {
  if (pending) return <p>Loading findings...</p>;
  if (failed || !findings) return <p role="alert">Findings unavailable.</p>;
  if (!findings.items.length) return <section className="panel"><p>No validation findings recorded.</p></section>;
  const blocking = findings.items.filter(f => isBlocking(f.severity));
  const others = findings.items.filter(f => !isBlocking(f.severity));
  const grouped = new Map<string, Finding[]>();
  for (const f of others) grouped.set(`${f.severity}:${f.code}`, [...(grouped.get(`${f.severity}:${f.code}`) ?? []), f]);
  return <section className="panel" aria-label="Findings">
    {blocking.length > 0 && <>
      <h2 className="findings-heading findings-blocking"><Icon name="failed" />Blocking ({blocking.length})</h2>
      <p className="muted">These stop the passages from being embedded. Each must be resolved by rebuilding — none can be accepted as it is.</p>
      <ul className="finding-cards">{blocking.map(f => {
        const guidance = guidanceFor({ code: f.code, severity: f.severity as FindingSeverity, blocking: true, count: 1, message: f.message, samples: [] });
        return <li key={f.id} className="finding-card finding-card-blocking">
          <FindingLine finding={f} chunk={null} run={run} />
          <h3>{guidance?.title ?? f.message}</h3>
          {guidance && <p>{guidance.explanation}</p>}
          <p className="muted">{f.message}</p>
          {f.chunk_id && <button type="button" onClick={() => open(f.chunk_id!, true)}>
            <Icon name="inspect" small />Inspect affected chunk</button>}
        </li>;
      })}</ul>
      {review ? <div className="findings-remedies"><h3>Ways to resolve it</h3><RemediationActions lifecycle={review} /></div>
        : <Superseded run={run} current={current} />}
    </>}
    {grouped.size > 0 && <>
      <h2 className="findings-heading findings-warning"><Icon name="conflict" />Warnings ({others.length})</h2>
      <p className="muted">Recorded for review; they do not stop processing.</p>
      <ul className="finding-cards">{[...grouped.values()].map(group => {
        const f = group[0];
        const guidance = guidanceFor({ code: f.code, severity: f.severity as FindingSeverity, blocking: false, count: group.length, message: f.message, samples: [] });
        return <li key={`${f.severity}:${f.code}`} className="finding-card finding-card-warning">
          <FindingLine finding={f} chunk={null} run={run} />
          <h3>{guidance?.title ?? f.message}{group.length > 1 ? ` × ${group.length}` : ''}</h3>
          {guidance && <p>{guidance.explanation}</p>}
          {f.chunk_id && <button type="button" className="secondary" onClick={() => open(f.chunk_id!, false)}>
            Inspect {group.length > 1 ? 'the first affected chunk' : 'affected chunk'}</button>}
        </li>;
      })}</ul>
    </>}
    <Pagination offset={findings.offset} total={findings.total} onChange={page} limit={findings.limit} />
  </section>;
}

function Detail({ chunkId, run, select, review, current, toFindings }: {
  chunkId: string; run: ChunkRun; select: (id: string) => void; review: Lifecycle | null;
  current: Lifecycle | null; toFindings: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const detail = useData<Chunk>(`/chunks/${chunkId}`), sources = useData<Page<Source>>(`/chunks/${chunkId}/sources?offset=${offset}`);
  // Arriving here — from a link or a button — moves the reader to the chunk, not to the page top.
  useEffect(() => {
    if (!detail.data) return;
    heading.current?.scrollIntoView?.({ block: 'start' });
    heading.current?.focus({ preventScroll: true });
  }, [detail.data]);
  const state = detail.data ? chunkState(detail.data) : null;
  const blocking = (detail.data?.findings ?? []).filter(f => isBlocking(f.severity));
  const page = detail.data?.page_start;
  const parseLink = `/documents/${run.document_id}/versions/${run.document_version_id}/parse/${run.parse_run_id}${page ? `?page=${page}` : ''}`;
  return <section className={`panel chunk-detail${state ? ` chunk-${state}` : ''}`} aria-labelledby="detail-title">
    <div className="chunk-card-head">
      <h2 id="detail-title" ref={heading} tabIndex={-1}>Selected chunk{detail.data ? ` #${detail.data.sequence_number}` : ''}</h2>
      {state && <StateBadge state={state} />}
    </div>
    <div className="actions">
      {state && state !== 'clean' && <button type="button" className="secondary" onClick={toFindings}>Back to findings</button>}
      <button type="button" className="secondary" onClick={() => select('')}>Close detail</button>
    </div>
    {detail.isPending ? <p>Loading chunk...</p> : detail.isError ? <p role="alert">Chunk detail unavailable.</p> : <>
      {(detail.data.findings ?? []).map((f, i) => <div key={i} className={`finding-card ${isBlocking(f.severity) ? 'finding-card-blocking' : 'finding-card-warning'}`}>
        <FindingLine finding={f} chunk={detail.data} run={run} />
        <p>{guidanceFor({ code: f.code, severity: f.severity as FindingSeverity, blocking: isBlocking(f.severity), count: 1, message: f.message, samples: [] })?.explanation ?? f.message}</p>
      </div>)}
      {blocking.length > 0 && <DecisionGuide parseLink={parseLink} finding={blocking[0]} review={review} current={current} run={run} />}
      <h3>Source representation</h3><pre className="chunk-text">{detail.data.normalized_text || 'No source text'}</pre>
      <h3>Retrieval representation</h3><p className="muted">This is what is embedded and measured. It may include declared hierarchy context; added labels are not source evidence.</p><pre className="chunk-text">{detail.data.retrieval_text}</pre>
      <p className="mono checksum">SHA-256: {detail.data.chunk_hash}</p>
      <p>{detail.data.chunk_type} · pages {detail.data.page_start ?? 'unlocated'}–{detail.data.page_end ?? 'unlocated'} · {detail.data.token_count} source tokens · {detail.data.retrieval_token_count} retrieval tokens</p>
      {['TABLE', 'TABLE_PART'].includes(detail.data.chunk_type) && <TableView chunk={detail.data} />}
      <ArtifactLinks chunk={detail.data} />
      <details><summary>Structured artifact metadata</summary><pre className="chunk-text">{JSON.stringify(detail.data.chunk_metadata, null, 2)}</pre></details>
      {detail.data.next_sibling_ids?.map(id => <button key={id} className="secondary" onClick={() => select(id)}>Next sibling</button>)}
    </>}
    <h3>Ordered source mappings</h3>{sources.isPending ? <p>Loading sources...</p> : sources.isError ? <p role="alert">Source mappings unavailable.</p> : <>
      {sources.data.items.map(s => <article key={s.position} className="version-card"><p>{s.role} / page {s.element.page_number ?? 'unlocated'} / reading order {s.element.reading_order} / offsets {s.start_offset}–{s.end_offset}</p>
        <p className="mono checksum">Element {s.element.id}</p><pre className="chunk-text">{s.element.normalized_text}</pre>
        <p className="mono">Bounding box: {s.element.bbox ? JSON.stringify(s.element.bbox) : 'Not reported by parser'}</p>
        <Link to={`/documents/${run.document_id}/versions/${run.document_version_id}/parse/${run.parse_run_id}?page=${s.element.page_number}`}>Open source page</Link>
      </article>)}<Pagination offset={offset} total={sources.data.total} onChange={setOffset} /></>}
  </section>;
}

/** "What should I do?" for a blocking chunk: inspect first, then the rebuild that fits. */
function DecisionGuide({ parseLink, finding, review, current, run }: {
  parseLink: string; finding: ChunkFinding; review: Lifecycle | null; current: Lifecycle | null;
  run: ChunkRun;
}) {
  const guidance = guidanceFor({ code: finding.code, severity: finding.severity as FindingSeverity, blocking: true, count: 1, message: finding.message, samples: [] });
  const sourceFirst = guidance?.inspect === 'parse';
  return <aside className="decision-guide" aria-labelledby="guide-title">
    <h3 id="guide-title"><Icon name="info" small />What should I do?</h3>
    <ol>
      <li><strong>Inspect the source.</strong> <Link to={parseLink}>Open the source page</Link> and
        compare it with this passage. Is the extracted {finding.code.includes('TABLE') || finding.code === 'CHUNK_OVERSIZED' ? 'table or text' : 'text'} correct?</li>
      <li><strong>{sourceFirst ? 'If it is wrong:' : 'If it is correct:'}</strong>{' '}
        {sourceFirst ? 'Reparse, so the document is extracted again.' : 'Rechunk. The passages are rebuilt from the same extraction; nothing is read again.'}</li>
      <li><strong>{sourceFirst ? 'If it is correct:' : 'If it is wrong:'}</strong>{' '}
        {sourceFirst ? 'Rechunk.' : 'Reparse first — a new extraction, then new passages.'}</li>
      <li>Do not simply retry: unchanged code and input give the same result. Embedding and index
        actions stay unavailable until the passages are valid.</li>
    </ol>
    {review ? <RemediationActions lifecycle={review} compact /> : <Superseded run={run} current={current} />}
  </aside>;
}

/**
 * Said instead of offering actions when these passages are no longer what the document is
 * stopped on — most often because a curator has just started a rebuild from here.
 */
function Superseded({ run, current }: { run: ChunkRun; current: Lifecycle | null }) {
  const link = <Link to={`/documents/${run.document_id}#lifecycle`}>Follow it on the document page</Link>;
  if (current?.state === 'PROCESSING') {
    return <p role="status" className="review-repeat note-info"><Icon name="processing" small />
      <span>The document is being processed again. These passages are an earlier attempt and are kept
        for reference. {link}.</span></p>;
  }
  if (current?.state === 'READY') {
    return <p role="status" className="review-repeat note-info"><Icon name="verified" small />
      <span>The document has since been rebuilt and is ready for Ask. These passages are an earlier
        attempt and are kept for reference. {link}.</span></p>;
  }
  return <p className="muted">These passages are not what the document is currently stopped on, so
    no action is offered here. {link}.</p>;
}
