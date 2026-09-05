import type { ParseResult, Severity } from '../../types/parsing';

/** Plain descriptions. No score, percentage or confidence is presented as parse accuracy. */
export const RESULT_LABEL: Record<ParseResult, string> = {
  PASS: 'Passed all checks',
  PASS_WITH_WARNINGS: 'Passed with warnings',
  NEEDS_REVIEW: 'Needs review before further processing',
  FAIL: 'Failed validation',
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
