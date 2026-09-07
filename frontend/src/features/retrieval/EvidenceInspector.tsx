import { Link } from 'react-router-dom';
import type { RerankedResponse } from '../../types/retrieval';

export function EvidenceInspector({ result, stage }: {result: RerankedResponse; stage: 'reranked' | 'evidence'}) {
  const evidence = result.evidence_set;
  if (stage === 'reranked') return <section className="panel"><h2>Reranked candidates</h2>
    <p>Reranker score — ranking diagnostic, not medical confidence.</p>
    {result.reranked.map(candidate => <article className="version-card" key={candidate.chunk_id}>
      <h3>#{candidate.reranked_rank} · {candidate.provenance.document_title}</h3>
      <p>Fused rank {candidate.fused_rank} → reranked rank {candidate.reranked_rank}</p>
      <p>Cross-encoder score {candidate.reranker_score.toFixed(5)} · {candidate.selected_anchor ? 'Selected anchor' : 'Candidate'}</p>
      <p>{candidate.provenance.chunk_type} · {candidate.provenance.source_type} · authority {candidate.provenance.authority_level} · page {candidate.provenance.page_start}</p>
      <pre className="chunk-preview">{candidate.preview}</pre>
      <Link to={`/chunk-runs/${candidate.provenance.chunk_run_id}?chunk=${candidate.chunk_id}`}>Inspect chunk</Link>
    </article>)}
  </section>;
  return <section className="panel"><h2>Evidence Set</h2>
    <p>Source candidates assembled for inspection. Evidence sufficiency has not been assessed.</p>
    <p>{evidence.total_tokens} tokens · {evidence.evidence_blocks.length} blocks · {evidence.duplicates_removed} duplicates removed</p>
    {evidence.requires_visual_evidence && <p className="notice">Visual source required. Inspect the original figure.</p>}
    {evidence.warnings.map(w => <p role="alert" key={w}>{w}</p>)}
    {!evidence.evidence_blocks.length && <p>No source block fits the configured evidence budget.</p>}
    {evidence.evidence_blocks.map(block => <article className="version-card" key={block.evidence_id}>
      <h3>{block.document_title} · {block.expansion_reason.replaceAll('_', ' ')}</h3>
      <p>{block.chunk_type} · {block.source_type} · authority {block.authority_level} · pages {block.pages.join(', ')} · {block.token_count} tokens</p>
      {block.source_type.includes('QUESTION') && <p>Assessment material: source keys are not automatically authoritative.</p>}
      <p>Hierarchy: {block.hierarchy.map(h => h.text).join(' › ') || 'No recorded hierarchy'}</p>
      {!!block.context_reasons?.length && <p>Source context: {block.context_reasons.map(r => r.replaceAll('_', ' ')).join(' · ')}</p>}
      <pre className="chunk-preview">{block.text}</pre>
      {!!block.source_spans?.length && <details><summary>Source offsets and boxes</summary><ul>{block.source_spans.map((span, index) => <li key={index} className="mono">{span.element_id}: {span.start}–{span.end}; page {span.page ?? 'unlocated'}; box {span.bbox.join(', ')}</li>)}</ul></details>}
      <p className="mono">Anchor: {block.anchor_chunk_id}</p>
      <p className="mono">Source elements: {block.source_element_ids.join(', ')}</p>
      <div className="actions">{block.source_chunk_ids.map((chunkId, index) => <Link key={chunkId} to={`/chunk-runs/${block.chunk_run_id}?chunk=${chunkId}`}>{block.source_chunk_ids.length > 1 ? `Inspect contributing chunk ${index + 1} of ${block.source_chunk_ids.length}` : 'Inspect chunk provenance'}</Link>)}
        <Link to={`/documents/${block.document_id}/versions/${block.document_version_id}/parse/${block.parse_run_id}?page=${block.pages[0] ?? 1}`}>Inspect original page</Link>
        <Link to={`/documents/${block.document_id}`}>Original document</Link></div>
      {block.artifacts.map(artifact => <p key={artifact.artifact_id}>{artifact.kind} source {artifact.artifact_id}
        {' · '}<Link to={`/documents/${block.document_id}/versions/${block.document_version_id}/parse/${block.parse_run_id}?page=${block.pages[0] ?? 1}#artifact-${artifact.artifact_id}`}>Inspect {artifact.kind.toLowerCase()} source</Link>
        {artifact.kind === 'TABLE' && <> · rows {artifact.row_indexes.join(', ')} · headers {artifact.header_rows.join(', ')}</>}
        {artifact.kind === 'FIGURE' && <> · {artifact.image_available ? 'Original image available in page inspector' : 'Original crop unavailable; inspect source page'}</>}
      </p>)}
    </article>)}
    <details><summary>M6 stage timings</summary><dl>{Object.entries(evidence.reranking_trace.durations_ms).map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{value.toFixed(2)} ms</dd></div>)}</dl></details>
  </section>;
}
