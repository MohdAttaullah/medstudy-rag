import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { Pagination } from '../../components/DocumentWidgets';
import type { Page } from '../../types/documents';
import type { ParseElement } from '../../types/parsing';
import type { ChunkRun, Chunk, Question, Finding } from '../../types/chunking';

type Cell = { row: number; column: number; text?: string; row_span?: number; col_span?: number };
/** Structured table chunks keep their canonical cells; the grid is rendered, never re-flowed. */
function TableView({ chunk }: { chunk: Chunk }) {
  const meta = chunk.chunk_metadata as {
    caption?: string; headers?: string; part_number?: number; part_count?: number;
    row_indexes?: number[]; header_rows?: number[]; cells?: Cell[]; possible_continuation?: boolean;
  };
  const cells = meta.cells ?? [];
  if (!cells.length) return null;
  const rows = [...new Set(cells.map(c => c.row))].sort((a, b) => a - b);
  const columns = Math.max(...cells.map(c => c.column + (c.col_span ?? 1)));
  const headers = new Set(meta.header_rows ?? []);
  return <><h3>Source table</h3>
    {meta.caption && <p>{meta.caption}</p>}
    <p className="muted">Part {meta.part_number ?? 1} of {meta.part_count ?? 1} · source rows {(meta.row_indexes ?? []).join(', ') || 'header only'} · repeated header rows {[...headers].join(', ') || 'none declared'}</p>
    {meta.possible_continuation && <p className="muted">The parser flagged this table as a possible continuation. Parts were not merged.</p>}
    <div className="table-scroll"><table className="parse-table"><tbody>
      {rows.map(row => <tr key={row}>{Array.from({ length: columns }, (_, column) => {
        const cell = cells.find(c => c.row === row && c.column === column);
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
export function ChunkInspector() {
  const { runId } = useParams();
  return <><p className="eyebrow">CHUNK PROVENANCE</p><h1>Chunk inspector</h1><AccessGate><Inspector runId={runId!} /></AccessGate></>;
}
function Inspector({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0), [kind, setKind] = useState(''), [page, setPage] = useState('');
  const [selected, setSelected] = useState(''), [parent, setParent] = useState(''), [question, setQuestion] = useState('');
  const [warnings, setWarnings] = useState(false), [tab, setTab] = useState<'chunks' | 'questions' | 'findings'>('chunks');
  const run = useData<ChunkRun>(`/chunk-runs/${runId}`);
  const query = new URLSearchParams({ offset: String(offset), limit: '20' });
  if (kind) query.set('chunk_type', kind); if (page) query.set('page_number', page);
  if (parent) query.set('parent_id', parent); if (question) query.set('question_id', question);
  if (warnings) query.set('warnings', 'true');
  const chunks = useData<Page<Chunk>>(`/chunk-runs/${runId}/chunks?${query}`, tab === 'chunks');
  const questions = useData<Page<Question>>(`/chunk-runs/${runId}/questions?offset=${offset}`, tab === 'questions');
  const findings = useData<Page<Finding>>(`/chunk-runs/${runId}/validation-findings?offset=${offset}`, tab === 'findings');
  if (run.isPending) return <p>Loading chunk run...</p>;
  if (run.isError) return <p role="alert">Chunk run unavailable or access denied.</p>;
  const value = run.data;
  return <><Link to={`/documents/${value.document_id}`}>Back to document</Link>
    <section className="panel"><h2>{value.is_active ? 'Active chunk dataset' : 'Inactive chunk dataset'}</h2>
      <p>{value.status} / {value.validation_result ?? 'Validation pending'}</p>
      <p>Chunking stops at ready for embedding. No embeddings, index, or answers exist for this dataset.</p>
      <dl className="detail-grid"><div><dt>Chunker</dt><dd>{value.chunker_name} {value.chunker_version}</dd></div>
        <div><dt>Policy</dt><dd>{value.configuration_version}</dd></div><div><dt>Tokenizer</dt><dd>{value.tokenizer_name}</dd></div>
        <div><dt>Policy fingerprint</dt><dd className="mono checksum">{value.policy_fingerprint}</dd></div></dl>
      <details><summary>Run metrics and reproducibility</summary><pre className="chunk-text">{JSON.stringify({ metrics: value.metrics, input: value.input_fingerprint, tokenizer_version: value.tokenizer_version }, null, 2)}</pre></details>
      {value.error_code && <p role="alert">{value.error_code}: {value.error_message}</p>}
      <Link to={`/documents/${value.document_id}/versions/${value.document_version_id}/parse/${value.parse_run_id}`}>Open source parse</Link>
    </section>
    <div className="actions" aria-label="Inspector views">{(['chunks', 'questions', 'findings'] as const).map(item => <button key={item} aria-pressed={tab === item} onClick={() => { setTab(item); setOffset(0); }}>{item}</button>)}</div>
    {tab === 'chunks' && <section className="panel"><h2>Chunks</h2><div className="actions">
      <label>Chunk type<select value={kind} onChange={e => { setKind(e.target.value); setOffset(0); }}><option value="">All types</option>{['TEXT_PARENT', 'TEXT_CHILD', 'LIST', 'TABLE', 'TABLE_PART', 'FORMULA', 'FIGURE_CONTEXT', 'QUESTION', 'QUESTION_EXPLANATION', 'OTHER_STRUCTURED'].map(t => <option key={t}>{t}</option>)}</select></label>
      <label>Source page<input type="number" min="1" value={page} onChange={e => { setPage(e.target.value); setOffset(0); }} /></label>
      <label><input type="checkbox" checked={warnings} onChange={e => { setWarnings(e.target.checked); setOffset(0); }} />With findings</label>
      {(parent || question) && <button onClick={() => { setParent(''); setQuestion(''); setOffset(0); }}>Clear relationship filter</button>}
    </div>
      {chunks.isPending ? <p>Loading chunks...</p> : chunks.isError ? <p role="alert">Chunks unavailable.</p> : <>
        {!chunks.data.items.length && <p>No chunks match this view.</p>}
        {chunks.data.items.map(c => <article className="version-card" key={c.id}><h3>#{c.sequence_number} {c.chunk_type}</h3>
          <p>Pages {c.page_start ?? 'unlocated'}–{c.page_end ?? 'unlocated'} / {c.token_count} source tokens / {c.retrieval_token_count} retrieval tokens</p>
          <p className="chunk-preview">{c.normalized_text.slice(0, 320) || 'Visual source without text'}</p>
          <div className="actions"><button onClick={() => setSelected(c.id)}>Inspect chunk {c.sequence_number}</button>
            {c.parent_chunk_id && <button onClick={() => setSelected(c.parent_chunk_id!)}>Inspect parent</button>}
            {['TEXT_PARENT', 'QUESTION'].includes(c.chunk_type) && <button onClick={() => { setParent(c.id); setOffset(0); }}>Show children</button>}</div>
        </article>)}<Pagination offset={offset} total={chunks.data.total} onChange={setOffset} />
      </>}
    </section>}
    {tab === 'questions' && <section className="panel"><h2>Question source objects</h2><p>Source keys are assessment material; they are not automatically authoritative medical evidence.</p>
      {questions.isPending ? <p>Loading questions...</p> : questions.isError ? <p role="alert">Questions unavailable.</p> : <>
        {!questions.data.items.length && <p>No question objects were extracted.</p>}
        {questions.data.items.map(q => <article key={q.id} className="version-card"><h3>{q.question_number}. {q.question_text}</h3><p>{q.question_type} / {q.extraction_status}</p>
          <ol>{q.options.map(o => <li key={o.ordinal}>{o.label}. {o.text}</li>)}</ol>
          <p><strong>Explicit source answer:</strong> <span>{q.explicit_answer ?? 'Absent; no answer inferred'}</span></p>
          <p className="chunk-text">{q.explanation ?? 'No source explanation'}</p><p>Authority: {String(q.authority.authority_level)}</p>
          <button onClick={() => { setQuestion(q.id); setParent(''); setTab('chunks'); setOffset(0); }}>Inspect question provenance</button>
        </article>)}<Pagination offset={offset} total={questions.data.total} onChange={setOffset} />
      </>}
    </section>}
    {tab === 'findings' && <section className="panel"><h2>Validation findings</h2>{findings.isPending ? <p>Loading findings...</p> : findings.isError ? <p role="alert">Findings unavailable.</p> : <>
      {!findings.data.items.length && <p>No validation findings recorded.</p>}{findings.data.items.map(f => <article key={f.id}><h3>{f.severity}: {f.code}</h3><p>{f.message}</p>{f.chunk_id && <button onClick={() => setSelected(f.chunk_id!)}>Inspect affected chunk</button>}</article>)}
      <Pagination offset={offset} total={findings.data.total} onChange={setOffset} /></>}</section>}
    {selected && <Detail key={selected} chunkId={selected} run={value} select={setSelected} />}
  </>;
}
function Detail({ chunkId, run, select }: { chunkId: string; run: ChunkRun; select: (id: string) => void }) {
  const [offset, setOffset] = useState(0);
  const detail = useData<Chunk>(`/chunks/${chunkId}`), sources = useData<Page<Source>>(`/chunks/${chunkId}/sources?offset=${offset}`);
  return <section className="panel" aria-label="Selected chunk"><h2>Selected chunk</h2><button onClick={() => select('')}>Close detail</button>
    {detail.isPending ? <p>Loading chunk...</p> : detail.isError ? <p role="alert">Chunk detail unavailable.</p> : <>
      <h3>Source representation</h3><pre className="chunk-text">{detail.data.normalized_text || 'No source text'}</pre>
      <h3>Retrieval representation</h3><p className="muted">May include declared hierarchy context. Added labels are not source evidence.</p><pre className="chunk-text">{detail.data.retrieval_text}</pre>
      <p className="mono checksum">SHA-256: {detail.data.chunk_hash}</p>
      <p>{detail.data.chunk_type} · pages {detail.data.page_start ?? 'unlocated'}–{detail.data.page_end ?? 'unlocated'} · {detail.data.token_count} source tokens · {detail.data.retrieval_token_count} retrieval tokens</p>
      {['TABLE', 'TABLE_PART'].includes(detail.data.chunk_type) && <TableView chunk={detail.data} />}
      <ArtifactLinks chunk={detail.data} />
      <details><summary>Structured artifact metadata</summary><pre className="chunk-text">{JSON.stringify(detail.data.chunk_metadata, null, 2)}</pre></details>
      {detail.data.next_sibling_ids?.map(id => <button key={id} onClick={() => select(id)}>Next sibling</button>)}
    </>}
    <h3>Ordered source mappings</h3>{sources.isPending ? <p>Loading sources...</p> : sources.isError ? <p role="alert">Source mappings unavailable.</p> : <>
      {sources.data.items.map(s => <article key={s.position} className="version-card"><p>{s.role} / page {s.element.page_number ?? 'unlocated'} / reading order {s.element.reading_order} / offsets {s.start_offset}–{s.end_offset}</p>
        <p className="mono checksum">Element {s.element.id}</p><pre className="chunk-text">{s.element.normalized_text}</pre>
        <p>Bounding box: {s.element.bbox ? JSON.stringify(s.element.bbox) : 'Not reported by parser'}</p>
        <Link to={`/documents/${run.document_id}/versions/${run.document_version_id}/parse/${run.parse_run_id}?page=${s.element.page_number}`}>Open source page</Link>
      </article>)}<Pagination offset={offset} total={sources.data.total} onChange={setOffset} /></>}
  </section>;
}
