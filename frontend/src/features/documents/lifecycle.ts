import type { IconName } from '../navigation/icons';
import type {
  Estimate, FindingGroup, LifecycleStage, LifecycleStageCode, LifecycleState, ReprocessAction,
  Review,
} from '../../types/lifecycle';

/**
 * How the document lifecycle reads to a person.
 *
 * Everything shown is derived from the server's lifecycle response; nothing here advances a
 * stage, counts toward a percentage or invents a completion time. This module only turns codes
 * into words and decides which words fit — kept apart from the components so the rules can be
 * tested on their own.
 */

export const STAGE_LABEL: Record<LifecycleStageCode, string> = {
  RECEIVED: 'Upload received',
  READING: 'Reading the document',
  PASSAGES: 'Preparing searchable passages',
  EMBEDDING: 'Creating search representations',
  SEMANTIC_INDEX: 'Building the semantic search index',
  KEYWORD_INDEX: 'Building the keyword search index',
  READY: 'Ready for Ask',
};

/** What the pipeline is doing inside a running stage, in plain words. */
export const ACTIVITY_LABEL: Record<string, string> = {
  UPLOADED: 'Checking the file',
  VALIDATING: 'Checking the file',
  QUEUED: 'Waiting for a worker',
  PARSING: 'Extracting text, tables and figures',
  NORMALIZING: 'Normalising the extracted text',
  ENRICHING: 'Linking captions and structure',
  READY_FOR_CHUNKING: 'Waiting for a worker',
  CHUNKING: 'Creating passages',
  VALIDATING_CHUNKS: 'Validating passages',
  READY_FOR_EMBEDDING: 'Waiting for a worker',
  EMBEDDING: 'Encoding passages',
  INDEXING: 'Writing vectors to the index',
  VERIFYING_INDEX: 'Verifying every vector',
  READY_FOR_RETRIEVAL: 'Waiting for a worker',
  SPARSE_INDEXING: 'Building the keyword index',
  VERIFYING_SPARSE_INDEX: 'Verifying the keyword index',
};

export type Tone = 'active' | 'success' | 'review' | 'danger' | 'neutral';

/** The headline for each overall state: a tone, an icon and a title — never colour alone. */
export const STATE: Record<LifecycleState, { tone: Tone; icon: IconName; title: string; short: string }> = {
  PROCESSING: { tone: 'active', icon: 'processing', title: 'Preparing document for Ask', short: 'Processing' },
  REVIEW_REQUIRED: { tone: 'review', icon: 'conflict', title: 'Paused for review', short: 'Review required' },
  FAILED: { tone: 'danger', icon: 'failed', title: 'Processing failed', short: 'Processing failed' },
  CANCELLED: { tone: 'neutral', icon: 'stage-skipped', title: 'Processing stopped', short: 'Stopped' },
  READY: { tone: 'success', icon: 'verified', title: 'Ready for Ask', short: 'Ready' },
  ARCHIVED: { tone: 'neutral', icon: 'archive', title: 'Archived', short: 'Archived' },
  DELETING: { tone: 'neutral', icon: 'processing', title: 'Deleting', short: 'Deleting' },
  DELETION_INCOMPLETE: {
    tone: 'danger', icon: 'failed', title: 'Deletion incomplete', short: 'Deletion incomplete',
  },
  NOT_STARTED: { tone: 'neutral', icon: 'stage-skipped', title: 'Not processed', short: 'Not processed' },
};

// ------------------------------------------------------------------------------------ time

/** 18s · 5m 06s · 1h 07m. Whole seconds; a stage clock does not need more precision. */
export function duration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * Milliseconds between `from` and the server's "now", advanced by local time since the response.
 *
 * Using the server's clock as the origin keeps a skewed browser clock from showing a negative or
 * inflated time. The local part only lets the display keep counting between polls; it never
 * decides anything about state.
 */
export function sinceOnServer(from: string | null, serverTime: string, receivedAt: number, now: number) {
  if (!from) return null;
  return Math.max(0, Date.parse(serverTime) - Date.parse(from) + Math.max(0, now - receivedAt));
}

function minutes(seconds: number) {
  return Math.max(1, Math.ceil(seconds / 60));
}

/** The estimate line, or the honest reason there is none. Null means say nothing. */
export function estimateText(estimate: Estimate): string | null {
  if (estimate.available && estimate.high_seconds !== null && estimate.low_seconds !== null) {
    if (estimate.high_seconds < 60) return 'Estimated: under a minute left in this stage.';
    const low = minutes(estimate.low_seconds);
    const high = minutes(estimate.high_seconds);
    const range = low === high ? `about ${high} min` : `about ${low}–${high} min`;
    return `Estimated: ${range} left in this stage, based on ${estimate.samples} similar documents on this server.`;
  }
  switch (estimate.reason) {
    case 'INSUFFICIENT_HISTORY':
      return `Time remaining is not available yet: too few similar documents have finished on this server to estimate it (${estimate.samples} of ${estimate.needed}).`;
    case 'LONGER_THAN_USUAL':
      return 'This is taking longer than similar documents usually do.';
    case 'NOT_ESTIMATED_FOR_STAGE':
      return 'Completion time depends on the document’s size and structure.';
    default:
      return null;
  }
}

// ----------------------------------------------------------------------------------- facts

const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
const number = (value: unknown) => (typeof value === 'number' ? value : null);

/** One line of real counts for a stage, or null when the stage has produced nothing yet. */
export function factLine(stage: LifecycleStage): string | null {
  const f = stage.facts;
  const parts: string[] = [];
  switch (stage.code) {
    case 'RECEIVED': {
      const pages = number(f.pages);
      const bytes = number(f.file_size_bytes);
      if (pages !== null) parts.push(plural(pages, 'page'));
      // A small file in megabytes reads as "0.0 MB", as though it were empty.
      if (bytes !== null) parts.push(bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`);
      break;
    }
    case 'READING': {
      const pages = number(f.pages);
      const ocr = number(f.ocr_pages);
      if (pages !== null) parts.push(plural(pages, 'page'));
      if (ocr) parts.push(`${ocr} read with OCR`);
      if (number(f.tables)) parts.push(plural(number(f.tables)!, 'table'));
      if (number(f.figures)) parts.push(plural(number(f.figures)!, 'figure'));
      break;
    }
    case 'PASSAGES': {
      const passages = number(f.passages);
      if (passages !== null) parts.push(plural(passages, 'passage'));
      if (number(f.blocking)) parts.push(plural(number(f.blocking)!, 'blocking issue'));
      if (number(f.warnings)) parts.push(plural(number(f.warnings)!, 'warning'));
      break;
    }
    case 'EMBEDDING': {
      const done = number(f.embedded);
      const total = number(f.eligible);
      if (done !== null && total !== null) parts.push(`${done} of ${total} passages encoded`);
      break;
    }
    case 'SEMANTIC_INDEX': {
      const done = number(f.verified_vectors);
      const total = number(f.expected_vectors);
      if (done !== null && total !== null) parts.push(`${done} of ${total} vectors verified`);
      break;
    }
    case 'KEYWORD_INDEX': {
      const done = number(f.verified_passages);
      const total = number(f.expected_passages);
      if (done !== null && total !== null) parts.push(`${done} of ${total} passages indexed`);
      if (number(f.terms)) parts.push(plural(number(f.terms)!, 'term'));
      break;
    }
    default:
      break;
  }
  return parts.length ? parts.join(' · ') : null;
}

// --------------------------------------------------------------------------------- review

export interface Guidance {
  /** What happened, in plain words. The validator's own message is always shown beside it. */
  title: string;
  explanation: string;
  /** What to look at first. */
  inspect: 'chunk' | 'parse' | null;
  /** Reprocessing paths that can actually fix this finding, most likely first. */
  remedies: ReprocessAction[];
}

/**
 * Finding → guidance, keyed on code *and* severity.
 *
 * The validator uses one code at two severities — `CHUNK_OVERSIZED` is a blocking ERROR when a
 * passage cannot be embedded whole and a WARNING when an indivisible unit merely exceeds its
 * target — so a code-only table would give one of them the wrong advice. A finding with no entry
 * gets no invented advice: the validator's message and the inspector link are shown instead.
 *
 * Accepting a finding is never offered for a chunk dataset. There is no acceptance path for one,
 * and a passage too large to embed would be truncated, which the embedding contract forbids.
 */
export const GUIDANCE: Record<string, Guidance> = {
  'CHUNK_OVERSIZED:ERROR': {
    title: 'A passage is too large to search',
    explanation:
      'One searchable passage is longer than the search model can read in full. It would be cut ' +
      'off, so processing stopped before embedding rather than index part of it silently.',
    inspect: 'chunk',
    remedies: ['rechunk', 'reparse'],
  },
  'CHUNK_ATOMIC_LIMIT:ERROR': {
    title: 'An indivisible unit is too large',
    explanation:
      'A table, formula or question that cannot be split exceeds the review limit. Check whether ' +
      'the source was extracted as one unit by mistake.',
    inspect: 'chunk',
    remedies: ['reparse', 'rechunk'],
  },
  'CHUNK_TABLE_HEADERS_MISSING:ERROR': {
    title: 'A table lost its header row',
    explanation:
      'Part of a table has no header context, so its values would be searchable without their ' +
      'meaning. This usually starts in extraction.',
    inspect: 'parse',
    remedies: ['reparse'],
  },
  'CHUNK_TINY_RATIO:ERROR': {
    title: 'Too many fragments',
    explanation:
      'Many passages are fragments too small to stand on their own, which usually means the text ' +
      'was split apart during extraction.',
    inspect: 'parse',
    remedies: ['reparse'],
  },
  'CHUNK_OVERSIZED_RATIO:ERROR': {
    title: 'Too many oversized passages',
    explanation: 'An unusual share of passages exceed their target size.',
    inspect: 'chunk',
    remedies: ['rechunk', 'reparse'],
  },
  'CHUNK_FIGURE_NO_TEXT:WARNING': {
    title: 'Figure without text',
    explanation:
      'The source gives this figure no caption or text, so search cannot find it. Its image is ' +
      'kept and can still be shown as a source figure. Nothing needs to be done.',
    inspect: null,
    remedies: [],
  },
  'CHUNK_OVERSIZED:WARNING': {
    title: 'Longer than the target size',
    explanation: 'An indivisible unit is longer than the target but still fits the search model.',
    inspect: null,
    remedies: [],
  },
  'CHUNK_TINY:WARNING': {
    title: 'A very short passage',
    explanation: 'A structurally separate passage is shorter than the target. It is still indexed.',
    inspect: null,
    remedies: [],
  },
};

export function guidanceFor(group: FindingGroup): Guidance | null {
  return GUIDANCE[`${group.code}:${group.severity}`] ?? null;
}

/** Plain explanation of each reprocessing action, shown beside its button. */
export const ACTION_COPY: Record<ReprocessAction, { label: string; explanation: string }> = {
  retry: { label: 'Retry', explanation: 'Run the stage that failed again.' },
  reparse: {
    label: 'Reparse',
    explanation: 'Extract the document again from the original file, then create new passages.',
  },
  rechunk: {
    label: 'Rechunk',
    explanation: 'Create a new set of passages from the existing extraction.',
  },
  reembed: {
    label: 'Re-embed',
    explanation: 'Rebuild the search representations and both indexes from the current passages.',
  },
  'reindex-sparse': {
    label: 'Rebuild keyword index',
    explanation: 'Rebuild only the keyword index from the current passages.',
  },
  cancel: { label: 'Cancel processing', explanation: 'Stop processing this document.' },
};

/** Endpoint for each action, relative to /ingestion/jobs/{job}. */
export const ACTION_PATH: Record<ReprocessAction, string> = {
  retry: 'retry', reparse: 'reparse', rechunk: 'rechunk', reembed: 'reembed',
  'reindex-sparse': 'reindex-sparse', cancel: 'cancel',
};

/**
 * Remedies to offer for a review, in guidance order, keeping only those the server says are
 * available. Inspection always comes first in the interface; these are the second step.
 *
 * Nothing is reordered to look more hopeful. When an earlier attempt with the same code and
 * settings stopped on the same findings, the interface says so beside these actions instead —
 * repeating a deterministic step reproduces its result and spends a retry.
 */
export function remedies(review: Review, available: Set<ReprocessAction>): ReprocessAction[] {
  const wanted: ReprocessAction[] = [];
  for (const group of review.groups.filter(item => item.blocking)) {
    for (const action of guidanceFor(group)?.remedies ?? []) {
      if (!wanted.includes(action)) wanted.push(action);
    }
  }
  if (!wanted.length && review.stage === 'PARSE') wanted.push('reparse');
  return wanted.filter(action => available.has(action));
}

/** The warning shown beside remedies when the same attempt has already failed the same way. */
export function repeatWarning(review: Review): string | null {
  const times = review.identical_earlier_attempts;
  if (!times) return null;
  return (
    `This stage has already stopped on the same ${review.blocking_count === 1 ? 'issue' : 'issues'} ` +
    `${times === 1 ? 'once' : `${times} times`} with unchanged settings. Repeating it will most ` +
    `likely give the same result and uses one of the ${review.retries_left} remaining ` +
    `${review.retries_left === 1 ? 'retry' : 'retries'}. Inspect the issue first; if the source ` +
    'was extracted correctly, it needs a change to the chunking settings or code.'
  );
}
