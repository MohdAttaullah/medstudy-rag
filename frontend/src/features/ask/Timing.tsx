import { useSession } from '../library/Session';

/**
 * Where the time went, for someone who can act on it.
 *
 * The detailed waterfall is shown to a principal who already holds the retrieval diagnostics
 * permission — the same people who can open the Retrieval inspector. A reader without it sees the
 * total and nothing else: stage timings expose which internal services ran and how a request is
 * composed, which is operational detail rather than part of the answer.
 *
 * Nothing here is derived. Each row is a measurement the server reported, and the group total is
 * the sum of exactly those rows; the difference between the groups and Total is shown rather than
 * hidden, because unattributed time is the thing an operator most needs to see.
 */
const GROUPS: [string, string[]][] = [
  ['Retrieval', ['Resolving sources', 'Searching sources', 'Combining results', 'Loading sources']],
  ['Reranking', ['Reranking evidence']],
  ['Evidence', ['Expanding context', 'Assembling evidence', 'Checking evidence sufficiency']],
  ['Generation', ['Drafting from evidence', 'Redrafting from evidence']],
  ['Verification', ['Extracting claims', 'Verifying claims', 'Checking for conflict']],
];

export interface Stage { stage: string; duration_ms: number }

export function group(stages: Stage[]) {
  const byStage = new Map(stages.map(stage => [stage.stage, stage.duration_ms]));
  const total = byStage.get('Total') ?? 0;
  const rows = GROUPS.map(([label, members]) => ({
    label,
    duration: members.reduce((sum, name) => sum + (byStage.get(name) ?? 0), 0),
    members: members.filter(name => byStage.has(name))
      .map(name => ({ label: name, duration: byStage.get(name)! })),
  })).filter(row => row.members.length);
  const attributed = rows.reduce((sum, row) => sum + row.duration, 0);
  return { rows, total, unattributed: Math.max(total - attributed, 0) };
}

export function Timing({ stages }: { stages: Stage[] }) {
  const { identity } = useSession();
  const { rows, total, unattributed } = group(stages);
  const detailed = identity?.permissions.includes('retrieval:search') ?? false;

  if (!detailed) {
    return <p className="muted">Answered in {(total / 1000).toFixed(1)} s.</p>;
  }

  return <details><summary>Timing</summary>
    <table className="timing-waterfall">
      <caption>Measured server-side for this request.</caption>
      <thead><tr><th scope="col">Stage</th><th scope="col">Duration</th><th scope="col">Share</th></tr></thead>
      <tbody>
        {rows.map(row => <tr key={row.label}>
          <th scope="row">{row.label}
            <span className="mono">{row.members.map(member => member.label).join(' · ')}</span>
          </th>
          <td className="mono">{row.duration.toFixed(0)} ms</td>
          <td>
            {/* The bar is a second encoding of the number beside it, never the only one. */}
            <span className="bar" style={{ inlineSize: `${total ? (row.duration / total) * 100 : 0}%` }}
              aria-hidden="true" />
            <span className="mono">{total ? ((row.duration / total) * 100).toFixed(0) : '0'}%</span>
          </td>
        </tr>)}
        {unattributed > 0 && <tr>
          <th scope="row">Unattributed<span className="mono">persistence, serialisation, transport</span></th>
          <td className="mono">{unattributed.toFixed(0)} ms</td>
          <td><span className="mono">{total ? ((unattributed / total) * 100).toFixed(0) : '0'}%</span></td>
        </tr>}
      </tbody>
      <tfoot><tr><th scope="row">Total</th><td className="mono">{total.toFixed(0)} ms</td><td /></tr></tfoot>
    </table>
  </details>;
}
