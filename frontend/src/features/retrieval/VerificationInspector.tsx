import type { AnswerResponse } from '../../types/retrieval';

const VERDICT_LABEL: Record<string, string> = {
  SUPPORTED: 'supported',
  UNSUPPORTED: 'not supported',
  CONTRADICTED: 'contradicted',
  INSUFFICIENT_EVIDENCE: 'evidence insufficient',
  UNVERIFIABLE: 'unverifiable',
};

/**
 * The M8 stages, kept visibly distinct from the M7 draft above them.
 *
 * A reader must be able to tell an unverified draft from a verified answer at a glance, because
 * only one of them has had every material claim checked against its evidence. Failed claims are
 * shown rather than hidden: an abstention that did not say what failed would be unauditable. There
 * is no confidence number anywhere here — M8 establishes no calibrated medical certainty.
 */
export function VerificationInspector({ result, stage }: {result: AnswerResponse; stage: 'claims' | 'answer'}) {
  const { verification, verified_answer: answer, verification_abstention: abstention } = result;
  if (!verification) return <section className="panel"><h2>Claim verification</h2>
    <p>No draft reached verification, so no claim was checked.</p></section>;

  if (stage === 'claims') return <section className="panel"><h2>Claim verification</h2>
    <p>Outcome <strong>{verification.outcome}</strong> · {verification.supported_claims} of {verification.material_claims} material claims supported
      {verification.repair_count ? ` · ${verification.repair_count} repair attempt` : ' · no repair needed'}</p>
    <p>Claims are taken from the answer text itself, not only from what the generator declared.
      Citations, numbers, units and negations are checked without a model; a model is asked only after those pass.</p>
    {verification.verifier && <p className="mono">
      Verifier: {verification.verifier.provider}/{verification.verifier.model_id}
      {verification.verifier.independent_of_generator
        ? ' · independent of the generator'
        : ' · SAME MODEL AS THE GENERATOR — a shared blind spot can pass both stages'}</p>}
    <div className="table-scroll"><table>
      <thead><tr><th>Claim</th><th>Type</th><th>Verdict</th><th>Reason</th><th>Evidence</th></tr></thead>
      <tbody>{verification.verifications.map(claim => <tr key={claim.claim_id}>
        <td>{claim.claim_text}</td>
        <td>{claim.claim_type.replaceAll('_', ' ').toLowerCase()}</td>
        <td>{VERDICT_LABEL[claim.verdict] ?? claim.verdict}</td>
        <td className="mono">{claim.reason_codes.join(', ')}</td>
        <td className="mono">{(claim.supporting_evidence_ids.length ? claim.supporting_evidence_ids : claim.contradicting_evidence_ids).join(', ') || '—'}</td>
      </tr>)}</tbody></table></div>
    {!!verification.contradictions.length && <><h3>Unresolved contradictions</h3>
      <p>Competing evidence is preserved. No source was chosen over another, and rank did not break the tie.</p>
      {verification.contradictions.map((finding, index) => <article className="version-card" key={index}>
        <h4>{finding.kind.replaceAll('_', ' ')}</h4><p>{finding.description}</p>
        <p className="mono">Evidence: {finding.evidence_ids.join(', ')}</p>
      </article>)}</>}
    <details><summary>M8 stage timings</summary><dl>{Object.entries(verification.durations_ms).map(([stageName, value]) =>
      <div key={stageName}><dt>{stageName.replace('_ms', '').replaceAll('_', ' ')}</dt><dd>{value.toFixed(2)} ms</dd></div>)}</dl></details>
    <p className="mono">Extraction {verification.claim_extraction_fingerprint.slice(0, 12)}… · policy {verification.final_policy_fingerprint.slice(0, 12)}…</p>
  </section>;

  if (abstention) return <section className="panel"><h2>Abstained — no answer released</h2>
    <p role="status">{abstention.message}</p>
    <p>Reason: {abstention.reason.replaceAll('_', ' ').toLowerCase()}</p>
    <ul className="service-list">{abstention.reason_codes.map(code =>
      <li key={code}><span className="mono">{code}</span></li>)}</ul>
    {!!abstention.contradicting_evidence_ids.length &&
      <p className="mono">Contradicting evidence: {abstention.contradicting_evidence_ids.join(', ')}</p>}
    <p>Abstaining is an intended outcome, not a failure. The retrieved evidence remains available above.</p>
  </section>;

  if (!answer) return <section className="panel"><h2>Verified answer</h2>
    <p>No verified answer was produced for this question.</p></section>;

  return <section className="panel"><h2>Verified answer</h2>
    <p role="status" className="notice">
      Every material claim below was checked against the evidence it cites. This is a synthetic
      engineering check over an indexed corpus, not clinical validation, and not medical advice.
    </p>
    <pre className="chunk-preview">{answer.answer}</pre>
    <h3>Checked claims</h3>
    {answer.claims.map(claim => <article className="version-card" key={claim.claim_id}>
      <p>{claim.claim_text}</p>
      <p className="mono">{VERDICT_LABEL[claim.verdict] ?? claim.verdict} · cites {claim.supporting_evidence_ids.join(', ') || '—'}</p>
    </article>)}
    <dl className="detail-grid">
      <div><dt>Generator</dt><dd className="mono">{answer.generator.provider}/{answer.generator.model_id}</dd></div>
      <div><dt>Verifier</dt><dd className="mono">{answer.verifier.provider}/{answer.verifier.model_id}</dd></div>
      <div><dt>Verifier independence</dt><dd>{answer.verifier.independent_of_generator ? 'independent of the generator' : 'same as the generator'}</dd></div>
      <div><dt>Repairs</dt><dd>{answer.repair_count}</dd></div>
      <div><dt>Status</dt><dd>{answer.verification_status.toLowerCase()}</dd></div>
    </dl>
  </section>;
}
