// Shared TypeScript types describing the backend API's request/response
// shapes. These mirror the Pydantic schemas in backend/app/schemas so the
// frontend and backend stay in sync.

export interface UserRead {
  id: number;
  username: string;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface TranscriptSegmentRead {
  id: number;
  case_id: number;
  speaker: string;
  text: string;
  timestamp: number;
  is_ai_generated: boolean;
  confidence: number | null;
  created_at: string;
}

export interface CallRecordingRead {
  id: number;
  case_id: number;
  original_filename: string;
  status: string;
  duration_seconds: number | null;
  created_at: string;
}

export interface LocationSourceSpan {
  start: number;
  end: number;
}

export interface LocationComponents {
  street?: string | null;
  district?: string | null;
  city?: string | null;
  landmark?: string | null;
  governorate?: string | null;
}

export interface LocationGeocode {
  lat?: number | null;
  lng?: number | null;
  confidence: number;
  provider?: string | null;
  match_type?: string | null;
}

export interface PatientLocation {
  raw_text: string;
  source_span?: LocationSourceSpan | null;
  components: LocationComponents;
  geocode?: LocationGeocode | null;
  confidence: number;
  needs_confirmation: boolean;
}

export interface CaseRead {
  id: number;
  incident_number: string;
  status: string;
  source: string;
  triage_priority: string | null;
  manual_details: Record<string, unknown> | null;
  patient_name: string | null;
  patient_age: number | null;
  patient_gender: string | null;
  patient_count: number | null;
  chief_complaint: string | null;
  patient_location: PatientLocation | null;
  notes: string | null;
  dispatcher: UserRead | null;
  assigned_medic: UserRead | null;
  assigned_hospital: UserRead | null;
  transcript_segments: TranscriptSegmentRead[];
  recordings: CallRecordingRead[];
  medic_completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CaseCreate {
  patient_name?: string;
  patient_age?: number;
  patient_gender?: string;
  chief_complaint?: string;
  patient_location?: PatientLocation | null;
  notes?: string;
  source?: "voice" | "manual";
  patient_count?: number;
  triage_priority?: string;
  manual_details?: Record<string, unknown> | null;
}

export interface CaseUpdate {
  patient_name?: string;
  patient_age?: number;
  patient_gender?: string;
  patient_count?: number;
  chief_complaint?: string;
  patient_location?: PatientLocation | null;
  notes?: string;
  status?: string;
  triage_priority?: string;
  assigned_medic_id?: number;
  assigned_hospital_id?: number;
}

export interface TranscriptSegmentCreate {
  speaker: string;
  text: string;
  is_ai_generated?: boolean;
  confidence?: number;
}

export interface BatchTranscriptionItem {
  audio_path: string;
  text: string;
  preprocessing: Record<string, unknown>;
  recording_id?: number | null;
  audio_label?: string | null;
}

export interface BatchTranscriptionResponse {
  results: BatchTranscriptionItem[];
  live_transcript_text?: string | null;
}

export interface AIHighlight {
  label: string;
  canonical_label: string;
  span_text: string;
  start?: number | null;
  end?: number | null;
  severity: "high" | "medium" | "low";
  negated: boolean;
  uncertain: boolean;
  current: boolean;
}

export interface AIMedicalEntity {
  canonical_label: string;
  spoken_text: string;
  severity: "high" | "medium" | "low";
  negated: boolean;
  uncertain: boolean;
  current: boolean;
  speaker: string;
}

export interface AIPatientState {
  consciousness: string;
  breathing: string;
  bleeding: string;
}

export interface AIMedicalEntities {
  symptoms: AIMedicalEntity[];
  injuries: AIMedicalEntity[];
  patient_state: AIPatientState;
  risk_factors: string[];
  mechanism_of_injury: string[];
  resolved_clues: string[];
  timeline_clues: string[];
}

export interface AITriageAssessment {
  level: "red" | "yellow" | "green";
  confidence: number;
  reasoning: string[];
  needs_confirmation: boolean;
}

export interface AITriageMeta {
  engine_version: string;
  language: string;
  dialect_handling: boolean;
}

export interface AITriageAnalysis {
  highlights: AIHighlight[];
  medical_entities: AIMedicalEntities;
  triage: AITriageAssessment;
  patient_location: PatientLocation | null;
  meta: AITriageMeta;
}

export interface LiveChunkResponse {
  text: string;
  live_transcript_text?: string | null;
  patient_location?: PatientLocation | null;
  extraction_confidence?: number | null;
  analysis: AITriageAnalysis;
  preprocessing: Record<string, unknown>;
}

export interface LiveAnalysisResponse {
  analysis: AITriageAnalysis;
  live_transcript_text?: string | null;
  /** Authoritative transcript revision at the time of the response. */
  transcript_revision?: number;
  /**
   * The revision the bundled ``analysis`` was produced against. If this
   * is older than the last revision the client already applied, the
   * client MUST discard ``analysis`` (otherwise stale polling can
   * overwrite newer WS state).
   */
  analyzed_revision?: number;
  analyzed_transcript_text?: string | null;
  analysis_in_progress?: boolean;
}

export interface LiveWarmupResponse {
  queued: boolean;
}

export interface FinalizeRecordingResponse {
  saved: boolean;
  recording_id?: number | null;
  file_size_bytes?: number | null;
  duration_seconds?: number | null;
}

export interface AnalyzeTextResponse {
  analysis: AITriageAnalysis;
}

export interface TriageSuggestion {
  priority: string;
  reasoning: string;
  confidence: number;
  recommended_actions: string[];
  matched_rules: string[];
}

export interface AuditLogRead {
  id: number;
  user_id: number | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  created_at: string;
}

export interface RealtimeEvent {
  type: string;
  data: Record<string, unknown>;
}

export interface UserCreate {
  username: string;
  email: string;
  full_name: string;
  password: string;
  role: string;
}

export interface AdminUserCreate {
  username: string;
  email: string;
  full_name: string;
  password: string;
  role: string;
  is_active?: boolean;
}

export interface AdminUserUpdate {
  email?: string;
  full_name?: string;
  role?: string;
  is_active?: boolean;
  password?: string;
}

export interface UserLogin {
  username: string;
  password: string;
}
