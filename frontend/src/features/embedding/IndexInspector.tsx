import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import { Pagination } from '../../components/DocumentWidgets';
import type { Page } from '../../types/documents';
import type { Chunk } from '../../types/chunking';
import type {
  ChunkEmbedding, EmbeddingRun, EmbeddingVersion, IndexFinding, IndexRun, IndexStatistics,
} from '../../types/embeddings';

function useData<T>(path: string, enabled = true) {
  const { token } = useSession();
  return useQuery({ queryKey: ['index', path], queryFn: () => api<T>(token, path), enabled });
}

export function IndexInspector() {
  const { runId } = useParams();
  return <><p className="eyebrow">VECTOR INDEX</p><h1>Index inspector</h1>
    <AccessGate><Inspector runId={runId!} /></AccessGate></>;
}

function Inspector({ runId }: { runId: string }) {
  const [offset, setOffset] = useState(0), [selected, setSelected] = useState('');
  const [tab, setTab] = useState<'points' | 'findings'>('points');
  const run = useData<IndexRun>(`/index-runs/${runId}`);
  const embeddingRunId = run.data?.embedding_run_id;
  const embedding = useData<EmbeddingRun>(`/embedding-runs/${embeddingRunId}`, !!embeddingRunId);
  const versions = useData<Page<EmbeddingVersion>>('/embedding-versions?limit=100');
  const statistics = useData<IndexStatistics>(`/index-runs/${runId}/statistics`);
  const points = useData<Page<ChunkEmbedding>>(
    `/embedding-runs/${embeddingRunId}/embeddings?offset=${offset}&limit=20`,
    !!embeddingRunId && tab === 'points');
  const findings = useData<Page<IndexFinding>>(
    `/embedding-runs/${embeddingRunId}/validation-findings?offset=${offset}`,
    !!embeddingRunId && tab === 'findings');

  if (run.isPending) return <p>Loading index run...</p>;
  if (run.isError) return <p role="alert">Index run unavailable or access denied.</p>;
  const value = run.data;
  const version = versions.data?.items.find(item => item.id === value.embedding_version_id);

  return <><Link to={`/documents/${embedding.data?.document_id ?? ''}`}>Back to document</Link>
    <section className="panel"><h2>{value.is_active ? 'Active vector index' : 'Inactive vector index'}</h2>
      <p>{value.status} · attempt {value.attempt + 1}</p>
      <p>This index is staged for retrieval. No query path, reranking or answering exists yet, so nothing here is retrievable evidence.</p>
      <dl className="detail-grid">
        <div><dt>Collection</dt><dd className="mono">{value.physical_collection}</dd></div>
        <div><dt>Vector name</dt><dd className="mono">{value.vector_name}</dd></div>
        <div><dt>Dimensions</dt><dd>{version ? version.embedding_dimension : '—'}</dd></div>
        <div><dt>Similarity</dt><dd>{version ? version.distance_metric : '—'}</dd></div>
        <div><dt>Model</dt><dd>{version ? `${version.model_id}` : '—'}</dd></div>
        <div><dt>Model revision</dt><dd className="mono checksum">{version ? version.model_revision : '—'}</dd></div>
        <div><dt>Pooling</dt><dd>{version ? version.pooling_strategy : '—'}</dd></div>
        <div><dt>Expected points</dt><dd>{value.expected_point_count}</dd></div>
        <div><dt>Indexed points</dt><dd>{value.indexed_point_count}</dd></div>
        <div><dt>Verified points</dt><dd>{value.verified_point_count}</dd></div>
        <div><dt>Upsert batches</dt><dd>{value.upsert_batches}</dd></div>
        <div><dt>Activated</dt><dd>{value.activated_at ?? 'Not activated'}</dd></div>
      </dl>
      {statistics.data && <p className="muted">
        Live index: {statistics.data.reachable
          ? `${statistics.data.live_points} point(s) present for this run; alias ${statistics.data.alias} points at ${statistics.data.alias_target ?? 'nothing'}.`
          : 'the vector index is not reachable from the API right now.'}
      </p>}
      {embedding.data && <p className="muted">
        {embedding.data.eligible_chunk_count} eligible chunks · {embedding.data.embedded_chunk_count} embedded · {embedding.data.reused_chunk_count} reused · {embedding.data.failed_chunk_count} failed.
        Parent chunks are excluded from first-stage retrieval by policy.
      </p>}
      {value.error_code && <p role="alert" className="error">{value.error_code}: {value.error_message}</p>}
    </section>

    <div className="actions" aria-label="Inspector views">
      {(['points', 'findings'] as const).map(item =>
        <button key={item} aria-pressed={tab === item} onClick={() => { setTab(item); setOffset(0); }}>{item}</button>)}
    </div>

    {tab === 'points' && <section className="panel"><h2>Indexed points</h2>
      <p className="muted">Vector metadata only. The dense arrays stay in the index; source text stays in the document store.</p>
      {points.isPending ? <p>Loading points...</p> : points.isError ? <p role="alert">Points unavailable.</p> : <>
        {!points.data.items.length && <p>No points recorded for this run.</p>}
        {points.data.items.map(item => <article key={item.id} className="version-card">
          <p>{item.dimension} dimensions · {item.token_count} input tokens · norm {item.vector_norm.toFixed(3)}{item.reused ? ' · reused' : ''}</p>
          <p className="mono checksum">Point {item.point_id}</p>
          <p className="mono checksum">Vector SHA-256: {item.vector_checksum}</p>
          <p className="mono checksum">Input SHA-256: {item.input_hash}</p>
          <button onClick={() => setSelected(item.chunk_id)}>Inspect source chunk</button>
        </article>)}
        <Pagination offset={offset} total={points.data.total} onChange={setOffset} />
      </>}
    </section>}

    {tab === 'findings' && <section className="panel"><h2>Index validation findings</h2>
      <p className="muted">Structural integrity of vectors and index reconciliation. Not a measure of retrieval relevance.</p>
      {findings.isPending ? <p>Loading findings...</p> : findings.isError ? <p role="alert">Findings unavailable.</p> : <>
        {!findings.data.items.length && <p>No validation findings recorded.</p>}
        {findings.data.items.map(item => <article key={item.id} className="version-card">
          <h3>{item.severity}: {item.code}</h3><p>{item.message}</p>
          <pre className="chunk-preview">{JSON.stringify(item.details, null, 2)}</pre>
        </article>)}
        <Pagination offset={offset} total={findings.data.total} onChange={setOffset} />
      </>}
    </section>}

    {selected && <SourceChunk key={selected} chunkId={selected} close={() => setSelected('')} />}
  </>;
}

/** A point resolves to its chunk, and the chunk resolves to the original source page. */
function SourceChunk({ chunkId, close }: { chunkId: string; close: () => void }) {
  const chunk = useData<Chunk>(`/chunks/${chunkId}`);
  return <section className="panel" aria-label="Source chunk"><h2>Source chunk</h2>
    <button onClick={close}>Close detail</button>
    {chunk.isPending ? <p>Loading chunk...</p> : chunk.isError ? <p role="alert">Chunk unavailable.</p> : <>
      <p>{chunk.data.chunk_type} · pages {chunk.data.page_start ?? 'unlocated'}–{chunk.data.page_end ?? 'unlocated'} · {chunk.data.token_count} source tokens</p>
      <h3>Embedded retrieval representation</h3>
      <pre className="chunk-text">{chunk.data.retrieval_text}</pre>
      <p className="mono checksum">Chunk SHA-256: {chunk.data.chunk_hash}</p>
      <Link to={`/chunk-runs/${chunk.data.chunk_run_id}`}>Open chunk inspector</Link>
    </>}
  </section>;
}
