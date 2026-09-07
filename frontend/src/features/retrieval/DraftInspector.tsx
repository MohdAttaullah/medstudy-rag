import type { DraftResponse } from '../../types/retrieval';

/**
 * The M7 stages, deliberately kept visually distinct from one another.
 *
 * A reader must be able to tell an EvidenceSet from a sufficiency decision from a draft, because
 * only the first is source material, only the second is a policy outcome, and the third is an
 * unverified model output that no part of this page may present as an answer. There is no
 * confidence number here: M7 establishes no calibrated notion of medical certainty.
 */
export function DraftInspector({ result, stage }: {result: DraftResponse; stage: 'sufficiency' | 'draft'}) {
  const { sufficiency, draft, abstention } = result;
  if (stage === 'sufficiency') return <section className="panel"><h2>Sufficiency decision</h2>
    <p>Status <strong>{sufficiency.status}</strong> · question kind {sufficiency.question_kind.replaceAll('_', ' ').toLowerCase()}</p>
    <p>Evaluated from the structure of the evidence. No retrieval, fusion or reranker score takes part in this decision.</p>
    <h3>Reason codes</h3>
    <ul className="service-list">{sufficiency.reason_codes.map(code =>
      <li key={code}><span>{code.replaceAll('_', ' ').toLowerCase()}</span><span className="mono">{code}</span></li>)}</ul>
    {!!sufficiency.missing_requirements.length && <><h3>Missing requirements</h3>
      <p>{sufficiency.missing_requirements.join(', ')}</p></>}
    {!!sufficiency.conflicts.length && <><h3>Unresolved conflicts</h3>
      <p>Competing evidence is preserved. No source was chosen over another.</p>
      {sufficiency.conflicts.map((conflict, index) => <article className="version-card" key={index}>
        <h4>{conflict.kind.replaceAll('_', ' ')}</h4><p>{conflict.description}</p>
        <p className="mono">Evidence: {conflict.evidence_ids.join(', ')}</p>
      </article>)}</>}
    <h3>Evaluated signals</h3>
    <div className="table-scroll"><table>
      <thead><tr><th>Signal</th><th>Measured</th><th>Required</th><th>Satisfied</th></tr></thead>
      <tbody>{sufficiency.evaluated_signals.map(signal => <tr key={signal.name}>
        <td>{signal.name.replaceAll('_', ' ')}</td>
        <td className="mono">{JSON.stringify(signal.value)}</td>
        <td className="mono">{signal.required === null ? '—' : JSON.stringify(signal.required)}</td>
        <td>{signal.satisfied ? 'yes' : 'no'}</td></tr>)}</tbody></table></div>
    <p className="mono">Policy {sufficiency.policy_version} · {sufficiency.policy_fingerprint.slice(0, 12)}…</p>
  </section>;

  if (abstention) return <section className="panel"><h2>Abstained</h2>
    <p role="status">{abstention.message}</p>
    <p>Reason: {abstention.reason.replaceAll('_', ' ').toLowerCase()}</p>
    <ul className="service-list">{abstention.reason_codes.map(code =>
      <li key={code}><span className="mono">{code}</span></li>)}</ul>
    {!!abstention.conflicting_evidence_ids.length &&
      <p className="mono">Conflicting evidence: {abstention.conflicting_evidence_ids.join(', ')}</p>}
    <p>No answer was generated. Abstaining is an intended outcome, not a failure.</p>
  </section>;

  if (!draft) return <section className="panel"><h2>Grounded draft</h2>
    <p>No draft was produced for this question.</p></section>;

  return <section className="panel"><h2>Grounded draft — awaiting verification</h2>
    <p role="status" className="notice">
      This is an unverified model draft, not an answer. Its statements have not been checked against
      the evidence they cite. Claim verification is a later milestone.
    </p>
    <pre className="chunk-preview">{draft.answer}</pre>
    {draft.evidence_gap && <p>Reported evidence gap: {draft.evidence_gap}</p>}
    <h3>Statements and the evidence each is bound to</h3>
    {draft.claims.map((claim, index) => <article className="version-card" key={index}>
      <p>{claim.text}</p>
      <p className="mono">Cites: {claim.evidence_ids.join(', ')}</p>
    </article>)}
    <p>Cited {draft.cited_evidence_ids.length} of {draft.cited_evidence_ids.length + draft.uncited_evidence_ids.length} supplied evidence blocks.
      Every citation was checked to name a block this request supplied; that check does not establish that the block supports the statement.</p>
    <dl className="detail-grid">
      <div><dt>Provider</dt><dd>{draft.provider.provider}</dd></div>
      <div><dt>Model</dt><dd className="mono">{draft.provider.model_id}</dd></div>
      <div><dt>Temperature</dt><dd>{draft.provider.temperature ?? 'provider default'}</dd></div>
      <div><dt>Prompt</dt><dd className="mono">{draft.provider.prompt_version}</dd></div>
      <div><dt>Grounding policy</dt><dd className="mono">{draft.grounding_policy_version}</dd></div>
      <div><dt>Verification</dt><dd>{draft.verification_status.replaceAll('_', ' ').toLowerCase()}</dd></div>
    </dl>
  </section>;
}
