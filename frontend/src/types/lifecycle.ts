/** Mirrors `backend/app/schemas/lifecycle.py`. Read entirely from persisted server state. */

export type LifecycleState =
  | 'PROCESSING' | 'REVIEW_REQUIRED' | 'FAILED' | 'CANCELLED' | 'READY' | 'ARCHIVED'
  | 'DELETING' | 'DELETION_INCOMPLETE' | 'NOT_STARTED';

export type LifecycleStageCode =
  | 'RECEIVED' | 'READING' | 'PASSAGES' | 'EMBEDDING' | 'SEMANTIC_INDEX' | 'KEYWORD_INDEX' | 'READY';

export type LifecycleStageState =
  | 'COMPLETED' | 'RUNNING' | 'PENDING' | 'BLOCKED' | 'FAILED' | 'CANCELLED';

export type FindingSeverity = 'CRITICAL' | 'ERROR' | 'WARNING' | 'INFO';

export type ReprocessAction = 'retry' | 'reparse' | 'rechunk' | 'reembed' | 'reindex-sparse' | 'cancel';

export interface LifecycleStage {
  code: LifecycleStageCode;
  state: LifecycleStageState;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  facts: Record<string, number | string | null>;
}

export interface Estimate {
  available: boolean;
  stage: LifecycleStageCode | null;
  low_seconds: number | null;
  high_seconds: number | null;
  samples: number;
  needed: number;
  reason: 'INSUFFICIENT_HISTORY' | 'LONGER_THAN_USUAL' | 'NOT_ESTIMATED_FOR_STAGE' | 'NOT_RUNNING' | null;
}

export interface FindingGroup {
  code: string;
  severity: FindingSeverity;
  blocking: boolean;
  count: number;
  message: string;
  samples: { page: number | null; chunk_id: string | null }[];
}

export interface Review {
  stage: 'PARSE' | 'CHUNK' | 'OTHER';
  run_id: string | null;
  blocking_count: number;
  warning_count: number;
  info_count: number;
  groups: FindingGroup[];
  identical_earlier_attempts: number;
  /** The chunker that built the run under review, and the one installed now. */
  reviewed_chunker_version?: string | null;
  current_chunker_version?: string | null;
  retries_left: number;
  max_retries: number;
}

export interface ActionAvailability {
  action: ReprocessAction;
  available: boolean;
  reason: 'NO_RETRIES_LEFT' | 'NOT_APPLICABLE' | null;
}

export interface Lifecycle {
  document_id: string;
  version_id: string | null;
  version_number: number | null;
  job_id: string | null;
  state: LifecycleState;
  status: string | null;
  current_stage: LifecycleStageCode | null;
  activity: string | null;
  terminal: boolean;
  run_started_at: string | null;
  finished_at: string | null;
  current_stage_started_at: string | null;
  heartbeat_at: string | null;
  server_time: string;
  stages: LifecycleStage[];
  estimate: Estimate;
  review: Review | null;
  failure: { code: string | null; message: string | null } | null;
  actions: ActionAvailability[];
}

export interface LifecycleSummary {
  state: LifecycleState;
  current_stage: LifecycleStageCode | null;
  activity: string | null;
  run_started_at: string | null;
  finished_at: string | null;
  server_time: string;
  blocking_count: number;
  warning_count: number;
}

export interface DeletionPreview {
  document_id: string;
  versions: number;
  pages: number;
  chunks: number;
  vectors: number;
  citing_answers: number;
  blocked_reason: 'PROCESSING' | null;
}
