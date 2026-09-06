import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import type { EmbeddingSummary } from '../../types/embeddings';

const RESULT: Record<string, string> = {
  SUCCEEDED: 'Embeddings complete', RUNNING: 'Embedding in progress', PENDING: 'Embedding queued',
  FAILED: 'Embedding failed', CANCELLED: 'Embedding cancelled', NEEDS_REVIEW: 'Needs review',
};
const INDEX: Record<string, string> = {
  VERIFIED: 'Index verified', STAGING: 'Index staging', VERIFYING: 'Index verifying',
  FAILED: 'Index failed', CANCELLED: 'Index cancelled', SUPERSEDED: 'Index superseded',
};

function findingSummary(counts: Record<string, number>) {
  const entries = Object.entries(counts).filter(([, count]) => count > 0);
  if (!entries.length) return 'No index validation findings.';
  return 'Validation findings: ' + entries.map(([severity, count]) =>
    `${count} ${severity.toLowerCase()}`).join(', ') + '.';
}

export function EmbeddingSummaryPanel({ documentId, versionId }: { documentId: string; versionId: string }) {
  const { token } = useSession();
  const result = useQuery({
    queryKey: ['embedding', documentId, versionId],
    queryFn: () => api<EmbeddingSummary>(token, `/documents/${documentId}/versions/${versionId}/embedding`),
    refetchInterval: 5000,
  });
  return <section className="panel"><h3>Embedding and index</h3>
    <p className="muted">A verified index means the vectors were loaded and reconciled. It does not mean this document can be answered from: retrieval and answering are not implemented.</p>
    {result.isPending ? <p>Loading embedding state...</p>
      : result.isError ? <p role="alert">Embedding state unavailable.</p>
      : !result.data.embedding_run ? <p>This version has not been embedded yet. Embedding requires an active, validated chunk dataset.</p>
      : <>
        {(() => {
          const { embedding_run: run, embedding_version: version, index_run: index } = result.data;
          return <article className="version-card">
            <p>{RESULT[run.status] ?? run.status} / {run.is_active ? 'Active vectors' : 'Inactive vectors'}
              {index && <> / {INDEX[index.status] ?? index.status}</>}</p>
            {version && <p>{version.model_id} @ {version.model_revision.slice(0, 12)}</p>}
            {version && <p className="muted">{version.embedding_dimension}-dimensional · {version.pooling_strategy} pooling · {version.normalization === 'NONE' ? 'unnormalized' : version.normalization} · {version.distance_metric} similarity</p>}
            <p>{run.eligible_chunk_count} eligible · {run.embedded_chunk_count} embedded · {run.reused_chunk_count} reused · {run.failed_chunk_count} failed</p>
            {index && <p>{index.verified_point_count} of {index.expected_point_count} points verified · vector {index.vector_name}</p>}
            <p className="muted">{findingSummary(result.data.finding_counts)}</p>
            {Object.keys(result.data.chunk_types).length > 0 &&
              <p className="muted">Indexed chunk types: {Object.entries(result.data.chunk_types).map(([kind, count]) => `${kind} ${count}`).join(', ')}. Parent chunks are not indexed.</p>}
            {run.error_code && <p role="alert" className="error">{run.error_code}: {run.error_message}</p>}
            {index && <Link className="button-link" to={`/index-runs/${index.id}`}>Open index inspector</Link>}
          </article>;
        })()}
      </>}
  </section>;
}
