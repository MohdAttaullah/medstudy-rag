import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import type { Page } from '../../types/documents';
import type { ChunkRun } from '../../types/chunking';

/** Metrics are recorded by the worker; a run that failed before validation has none. */
function count(metrics: Record<string, unknown>, key: string): number | null {
  const value = metrics?.[key];
  return typeof value === 'number' ? value : null;
}

function summary(run: ChunkRun): string {
  const parts: string[] = [];
  for (const [key, label] of [['parents', 'parent'], ['children', 'child'], ['tables', 'table'],
    ['formulas', 'formula'], ['figures', 'figure'], ['questions', 'question']] as const) {
    const value = count(run.metrics, key);
    if (value !== null) parts.push(`${value} ${label}`);
  }
  return parts.length ? parts.join(' · ') : 'No chunk counts recorded for this run.';
}

export function ChunkSummaryPanel({ documentId, versionId }: { documentId: string; versionId: string }) {
  const { token } = useSession();
  const result = useQuery({ queryKey: ['chunk-runs', documentId, versionId],
    queryFn: () => api<Page<ChunkRun>>(token, `/documents/${documentId}/versions/${versionId}/chunk-runs`), refetchInterval: 5000 });
  return <section className="panel"><h3>Chunking</h3>
    <p className="muted">Validated chunks are staged for embedding. This version is not searchable.</p>
    {result.isPending ? <p>Loading chunk runs...</p> : result.isError ? <p role="alert">Chunk state unavailable.</p> : !result.data.items.length ?
      <p>No chunk runs yet. Chunking requires an active, validated parse.</p> : <>
      {result.data.items.map(run => <article key={run.id} className="version-card">
        <p>{run.status} / {run.validation_result ?? 'Validation pending'} / {run.is_active ? 'Active dataset' : 'Inactive dataset'}</p>
        <p>{run.chunker_name} {run.chunker_version} · {run.configuration_version} · {count(run.metrics, 'chunks') ?? 0} chunks</p>
        <p className="muted">{summary(run)}</p>
        <p className="muted">Tokenizer {run.tokenizer_name} (local measurement only; no embeddings exist).</p>
        {run.error_code && <p role="alert">{run.error_code}: {run.error_message}</p>}
        <Link to={`/chunk-runs/${run.id}`}>Open chunk inspector</Link>
      </article>)}
      {result.data.total > result.data.items.length && <p>Showing the latest {result.data.items.length} of {result.data.total} runs.</p>}
    </>}
  </section>;
}
