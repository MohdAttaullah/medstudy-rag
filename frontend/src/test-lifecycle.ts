import type { Lifecycle, LifecycleStageCode, LifecycleStageState } from './types/lifecycle';

/** A lifecycle response for tests, shaped exactly as `GET /documents/{id}/lifecycle` returns it. */

const CODES: LifecycleStageCode[] = [
  'RECEIVED', 'READING', 'PASSAGES', 'EMBEDDING', 'SEMANTIC_INDEX', 'KEYWORD_INDEX', 'READY',
];

export function stages(states: Partial<Record<LifecycleStageCode, LifecycleStageState>>) {
  return CODES.map(code => ({
    code, state: states[code] ?? 'PENDING', started_at: null, completed_at: null, duration_ms: null,
    facts: {},
  }));
}

export function lifecycleView(overrides: Partial<Lifecycle> = {}): Lifecycle {
  return {
    document_id: 'doc-1', version_id: 'version-1', version_number: 1, job_id: 'job-1',
    state: 'READY', status: 'RETRIEVAL_READY', current_stage: 'READY', activity: null, terminal: true,
    run_started_at: '2026-09-05T00:00:00Z', finished_at: '2026-09-05T00:05:00Z',
    current_stage_started_at: null, heartbeat_at: null, server_time: '2026-09-05T00:10:00Z',
    stages: stages(Object.fromEntries(CODES.map(code => [code, 'COMPLETED']))),
    estimate: {
      available: false, stage: null, low_seconds: null, high_seconds: null, samples: 0, needed: 8,
      reason: 'NOT_RUNNING',
    },
    review: null, failure: null, actions: [],
    ...overrides,
  };
}
