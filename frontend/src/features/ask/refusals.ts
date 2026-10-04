import type { IconName } from '../navigation/icons';

/**
 * Requests the question itself made impossible, as opposed to a service that broke.
 *
 * These arrive as outcome FAILED — nothing was searched, so it would be untrue to call them an
 * abstention about the evidence — but labelling them "Technical failure" told a reader the service
 * was down when it was not, and invited a retry that could only fail the same way. The server's
 * message already says the true cause; this is the label and heading that sit above it.
 *
 * The set mirrors `REASON_MESSAGES` in `backend/app/services/ask.py` and is closed on purpose. Any
 * other FAILED keeps the technical wording, because softening a real outage would be the worse
 * mistake.
 */
export interface Refusal { icon: IconName; label: string; title: string }

const QUESTION_REFUSALS: Record<string, Refusal> = {
  QUERY_TOO_LONG: {
    icon: 'rephrase', label: 'Question too long', title: 'Please ask a shorter question',
  },
  QUERY_EMPTY: {
    icon: 'rephrase', label: 'Nothing to search', title: 'Please rephrase the question',
  },
};

/** The refusal a FAILED turn really was, or null when it was a genuine failure. */
export function questionRefusal(outcome: string | null | undefined, reasons?: string[] | null) {
  if (outcome !== 'FAILED') return null;
  for (const code of reasons ?? []) {
    // Own keys only: `in` would also match inherited names such as "constructor".
    if (Object.prototype.hasOwnProperty.call(QUESTION_REFUSALS, code)) return QUESTION_REFUSALS[code];
  }
  return null;
}
