import type { ParseResult, ParseRunStatus, Severity } from '../../types/parsing';

/** Plain descriptions. No score, percentage or confidence is presented as parse accuracy. */
export const RESULT_LABEL: Record<ParseResult, string> = {
  PASS: 'Passed all checks',
  PASS_WITH_WARNINGS: 'Passed with warnings',
  NEEDS_REVIEW: 'Needs review before further processing',
  FAIL: 'Failed validation',
};

/**
 * Run status in words. `REVIEWED_ACCEPTED` is deliberately not phrased as success: the parse did
 * not pass, a person decided it was usable anyway, and the difference has to stay visible.
 */
export const RUN_STATUS_LABEL: Record<ParseRunStatus, string> = {
  RUNNING: 'Running',
  SUCCEEDED: 'Succeeded',
  REVIEWED_ACCEPTED: 'Accepted after review',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
};

export const SEVERITY_ORDER: Severity[] = ['CRITICAL', 'ERROR', 'WARNING', 'INFO'];

export function findingSummary(counts: Record<string, number>): string {
  const parts = SEVERITY_ORDER.filter(level => counts[level]).map(
    level => `${counts[level]} ${level.toLowerCase()}`,
  );
  return parts.length ? `Validation findings: ${parts.join(', ')}.` : 'No validation findings recorded.';
}

export const label = (value: string) =>
  value.toLowerCase().replaceAll('_', ' ').replace(/^./, character => character.toUpperCase());
