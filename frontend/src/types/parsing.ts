export type ParseRunStatus = 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
export type ParseResult = 'PASS' | 'PASS_WITH_WARNINGS' | 'NEEDS_REVIEW' | 'FAIL';
export type Severity = 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';
export type ElementType =
  | 'TITLE' | 'HEADING' | 'PARAGRAPH' | 'LIST' | 'LIST_ITEM' | 'TABLE' | 'FORMULA' | 'FIGURE'
  | 'CAPTION' | 'FOOTNOTE' | 'PAGE_HEADER' | 'PAGE_FOOTER' | 'SECTION' | 'CODE' | 'OTHER';

/** PDF points, TOPLEFT origin, relative to the owning page's width/height. */
export interface BoundingBox { x1: number; y1: number; x2: number; y2: number; origin: 'TOPLEFT' }

export interface ParseRun {
  id: string; document_version_id: string; attempt: number; parser_name: string;
  parser_provider: string; parser_version: string; configuration_version: string;
  configuration_fingerprint: string; status: ParseRunStatus; is_active: boolean;
  validation_result: ParseResult | null; ocr_mode: string; ocr_engine: string | null;
  tables_enabled: boolean; formulas_enabled: boolean; figures_enabled: boolean;
  previews_enabled: boolean; page_count: number | null; source_page_count: number | null;
  element_count: number | null; table_count: number | null; figure_count: number | null;
  formula_count: number | null; ocr_page_count: number | null; raw_artifact_bytes: number | null;
  duration_ms: number | null; started_at: string | null; completed_at: string | null;
  error_code: string | null; error_message: string | null; created_at: string;
  finding_counts: Record<string, number>;
}
export interface ParseSummary {
  document_version_id: string; ingestion_status: string; parse_run: ParseRun | null; parse_runs: number;
}
export interface ParsePage {
  id: string; parse_run_id: string; page_number: number; width: number; height: number;
  rotation: number; element_count: number; source_text_chars: number; ocr_used: boolean;
  ocr_evidence: string | null; has_preview: boolean; preview_media_type: string | null;
  extracted_text?: string;
}
export interface ParseElement {
  id: string; page_id: string | null; page_number: number | null; parent_element_id: string | null;
  element_type: ElementType; depth: number; ordinal: number; reading_order: number;
  raw_text: string | null; normalized_text: string | null; text_normalized: boolean;
  source_parser_ref: string | null; source_label: string | null; content_layer: string | null;
  structure_inferred: boolean; bbox: BoundingBox | null;
}
export interface TableCell {
  text: string; row: number; column: number; row_span: number; column_span: number;
  column_header: boolean; row_header: boolean;
}
export interface ParseTable {
  id: string; document_element_id: string; page_number: number | null; caption_text: string | null;
  caption_element_id: string | null; row_count: number; column_count: number;
  header_row_count: number; table_group_id: string | null; continuation_of_id: string | null;
  possible_continuation: boolean; continuation_evidence: string | null; malformed: boolean;
  bbox: BoundingBox | null; cells?: TableCell[]; markdown?: string | null; html?: string | null;
}
export interface ParseFigure {
  id: string; document_element_id: string; page_number: number | null; caption_text: string | null;
  caption_element_id: string | null; figure_kind: string | null; image_media_type: string | null;
  image_width: number | null; image_height: number | null; image_bytes: number | null;
  has_image: boolean; bbox: BoundingBox | null;
}
export interface ParseFormula {
  id: string; document_element_id: string; page_number: number | null;
  source_expression: string | null; normalized_expression: string | null; notation: string | null;
  preceding_element_id: string | null; following_element_id: string | null; bbox: BoundingBox | null;
}
export interface ParseFinding {
  id: string; page_number: number | null; document_element_id: string | null; scope: string;
  severity: Severity; code: string; message: string; details: Record<string, unknown>;
  created_at: string;
}
