export type SourceType = 'GUIDELINE' | 'REFERENCE_BOOK' | 'TEXTBOOK' | 'COURSE_MATERIAL' | 'QUESTION_BANK' | 'QUESTION_PAPER' | 'ANSWER_KEY' | 'OTHER';
export type Authority = 'UNREVIEWED' | 'ASSESSMENT' | 'REFERENCE' | 'HIGH';
export interface Metadata { title: string; source_type: SourceType; authority_level: Authority; description?: string | null; specialty?: string | null; subject?: string | null; publisher?: string | null }
export interface Version {
  id: string; document_id: string; version_number: number; edition: string | null;
  publication_year: number | null; original_filename: string; normalized_filename: string;
  file_size_bytes: number; sha256: string; ingestion_status: string; searchable: boolean;
  created_by_user_id: string; created_at: string; archived_at: string | null;
}
export interface Document extends Metadata { id: string; created_by_user_id: string; created_at: string; archived_at: string | null; latest_version: Version | null }
export interface StageEvent { id: string; sequence: number; to_status: string; service_identity: string; retry_number: number; error_detail: string | null; created_at: string }
export interface Job {
  id: string; document_version_id: string; document_id: string; document_title: string;
  version_number: number; status: string; current_stage: string; correlation_id: string;
  created_at: string; started_at: string | null; queue_received_at: string | null;
  retry_count: number; max_retries: number; last_error_code: string | null;
  last_error_message: string | null; events: StageEvent[]; configuration_version: string;
}
export interface Page<T> { items: T[]; total: number; offset: number; limit: number }
export interface UploadResult { document_id: string; version_id: string; job_id: string; status: string; replayed: boolean }
export interface Identity { user_id: string; display_name: string; role: string; permissions: string[]; auth_mode: string }
export interface UploadLimits { max_upload_bytes: number; max_upload_mib: number; allowed_mime_types: string[]; files_per_request: number }
