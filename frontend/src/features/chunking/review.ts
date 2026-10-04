import type { Chunk, ChunkFinding, ChunkRun, Finding } from '../../types/chunking';

/**
 * How chunk validation reads in the inspector. Kept apart from the component so the rules — which
 * state a chunk is in, what a finding measured, what each view contains — can be tested alone.
 */

export type ChunkState = 'blocking' | 'warning' | 'clean';
export type StatusFilter = 'all' | 'findings' | 'blocking' | 'warning' | 'clean';
export type Tab = 'chunks' | 'questions' | 'findings';

/** CRITICAL and ERROR stop a dataset from becoming active; the server uses the same split. */
export const BLOCKING = new Set(['CRITICAL', 'ERROR']);

export function isBlocking(severity: string) {
  return BLOCKING.has(severity);
}

/** A chunk is in the state of its worst finding. No findings is "clean", nothing more. */
export function chunkState(chunk: Pick<Chunk, 'findings'>): ChunkState {
  const findings = chunk.findings ?? [];
  if (findings.some(f => isBlocking(f.severity))) return 'blocking';
  return findings.length ? 'warning' : 'clean';
}

export const SEVERITY_WORD: Record<string, string> = {
  CRITICAL: 'Blocking', ERROR: 'Blocking', WARNING: 'Warning', INFO: 'Note',
};

export const STATE_WORD: Record<ChunkState, string> = {
  blocking: 'Blocking', warning: 'Warning', clean: 'No findings',
};

/** The largest an embedded chunk body may be under the run's own policy. */
export function retrievalBudget(run: Pick<ChunkRun, 'config_snapshot'>): number | null {
  const policy = run.config_snapshot ?? {};
  const values = ['table_max_tokens', 'child_target_tokens', 'explanation_max_tokens']
    .map(key => policy[key])
    .filter((value): value is number => typeof value === 'number');
  return values.length ? Math.max(...values) : null;
}

/**
 * "396 / 384 retrieval tokens" for a size finding. The finding's own record wins; findings from
 * before that was recorded fall back to the chunk's stored count and the run's policy, which is
 * exactly what the validator compared.
 */
export function measurement(
  finding: Pick<ChunkFinding | Finding, 'code' | 'severity' | 'details'>,
  chunk: Pick<Chunk, 'retrieval_token_count' | 'token_count'> | null,
  run: Pick<ChunkRun, 'config_snapshot'>,
): string | null {
  if (finding.code !== 'CHUNK_OVERSIZED') return null;
  const details = (finding.details ?? {}) as Record<string, unknown>;
  const measured = typeof details.measured_tokens === 'number' ? details.measured_tokens
    : chunk ? (isBlocking(finding.severity) ? chunk.retrieval_token_count : chunk.token_count) : null;
  const limit = typeof details.limit_tokens === 'number' ? details.limit_tokens
    : isBlocking(finding.severity) ? retrievalBudget(run) : null;
  if (measured === null || limit === null) return null;
  const unit = isBlocking(finding.severity) ? 'retrieval tokens' : 'source tokens';
  return `${measured} / ${limit} ${unit}`;
}

/** What each view holds, from what the code actually puts there. */
export const TAB_HELP: Record<Tab, { label: string; help: string }> = {
  chunks: {
    label: 'Chunks',
    help: 'The passages built from the parsed document. Child passages — text, lists, tables, '
      + 'figures, formulas and questions — are embedded and searched for Ask. Parent passages are '
      + 'never embedded; they give a found passage its surrounding context.',
  },
  questions: {
    label: 'Questions',
    help: 'Exam questions extracted from question-bank, question-paper and answer-key documents, '
      + 'with their options and the answer only where the source states it — none is ever '
      + 'inferred. Empty for textbooks and other sources. Assessment material, not authoritative '
      + 'medical evidence.',
  },
  findings: {
    label: 'Findings',
    help: 'Results of validating this passage set. Blocking findings stop it from being embedded '
      + 'and searched; warnings are recorded but do not stop it.',
  },
};

const STATUSES: StatusFilter[] = ['all', 'findings', 'blocking', 'warning', 'clean'];
const TABS: Tab[] = ['chunks', 'questions', 'findings'];

export function readTab(value: string | null): Tab | null {
  return TABS.includes(value as Tab) ? value as Tab : null;
}

export function readStatus(value: string | null): StatusFilter {
  return STATUSES.includes(value as StatusFilter) ? value as StatusFilter : 'all';
}
