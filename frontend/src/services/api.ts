// Central API client. Every backend call goes through the shared
// ``request`` helper, which attaches the JWT auth header and normalizes
// error handling. Endpoints are grouped by domain (authApi, casesApi,
// dispatcherApi, inferenceApi, adminApi) so callers import one object.
import type {
  TokenResponse,
  UserRead,
  UserCreate,
  AdminUserCreate,
  AdminUserUpdate,
  UserLogin,
  BatchTranscriptionResponse,
  LiveChunkResponse,
  LiveAnalysisResponse,
  LiveWarmupResponse,
  FinalizeRecordingResponse,
  AnalyzeTextResponse,
  CaseRead,
  CaseCreate,
  CaseUpdate,
  TranscriptSegmentCreate,
  TranscriptSegmentRead,
  CallRecordingRead,
  AuditLogRead,
} from "../types/api";

// Use a relative API base in development so Vite can proxy requests
// to the backend and the browser never sees a cross-origin call.
const API_BASE = "/api/v1";

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem("token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
      ...options.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    let message = `Request failed (${res.status})`;
    if (body?.detail) {
      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body.detail)) {
        message = body.detail.map((e: { msg?: string }) => e.msg ?? JSON.stringify(e)).join("; ");
      } else {
        message = JSON.stringify(body.detail);
      }
    }
    throw new Error(message);
  }

  return res.json() as Promise<T>;
}

/* ── Auth ───────────────────────────────────────────────────────────── */
export const authApi = {
  register(data: UserCreate) {
    return request<UserRead>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  login(data: UserLogin) {
    return request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  me() {
    return request<UserRead>("/auth/me");
  },
};

/* ── Cases ──────────────────────────────────────────────────────────── */
export const casesApi = {
  list(params?: Record<string, string>) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<CaseRead[]>(`/cases/${qs}`);
  },

  create(data: CaseCreate) {
    return request<CaseRead>("/cases/", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  get(id: number | string) {
    return request<CaseRead>(`/cases/${id}`);
  },

  update(id: number | string, data: CaseUpdate) {
    return request<CaseRead>(`/cases/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  addTranscript(id: number | string, data: TranscriptSegmentCreate) {
    return request<TranscriptSegmentRead>(`/cases/${id}/transcript`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  getRecordings(id: number | string) {
    return request<CallRecordingRead[]>(`/cases/${id}/recordings`);
  },
};

/* ── Dispatcher ─────────────────────────────────────────────────────── */
export const dispatcherApi = {
  async uploadRecording(caseId: number | string, file: File) {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API_BASE}/dispatcher/upload-recording?case_id=${caseId}`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Upload failed (${res.status})`);
    }
    return res.json();
  },

  dispatchCase(
    caseId: number | string,
    data: {
      triage_priority?: string | null;
      notes?: string | null;
      chief_complaint?: string | null;
      include_hospital?: boolean;
    },
  ) {
    return request<CaseRead>(`/dispatcher/cases/${caseId}/dispatch`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
};

/* ── Inference / ASR ─────────────────────────────────────────────────── */
export const inferenceApi = {
  async batchTranscribe(caseId: number | string, files: File[]) {
    const form = new FormData();
    form.append("case_id", String(caseId));
    for (const file of files) {
      form.append("files", file);
    }

    const res = await fetch(`${API_BASE}/inference/batch-transcribe`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: form,
    });

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Batch transcription failed (${res.status})`);
    }

    return res.json() as Promise<BatchTranscriptionResponse>;
  },

  async liveChunk(caseId: number | string, file: File) {
    const form = new FormData();
    form.append("case_id", String(caseId));
    form.append("file", file);

    const res = await fetch(`${API_BASE}/inference/live-chunk`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: form,
    });

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Live chunk failed (${res.status})`);
    }

    return res.json() as Promise<LiveChunkResponse>;
  },

  async liveAnalysis(caseId: number | string) {
    const form = new FormData();
    form.append("case_id", String(caseId));

    const res = await fetch(`${API_BASE}/inference/live-analysis`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: form,
    });

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Live analysis failed (${res.status})`);
    }

    return res.json() as Promise<LiveAnalysisResponse>;
  },

  async liveWarmup() {
    const res = await fetch(`${API_BASE}/inference/live-warmup`, {
      method: "POST",
      headers: getAuthHeaders(),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Live warmup failed (${res.status})`);
    }

    return res.json() as Promise<LiveWarmupResponse>;
  },

  // Persist the live call's buffered audio as a single recording.
  // Called when a call ends. Best-effort: failures should not block
  // the UI from ending the call.
  async finalizeRecording(caseId: number | string) {
    const form = new FormData();
    form.append("case_id", String(caseId));

    const res = await fetch(`${API_BASE}/inference/finalize-recording`, {
      method: "POST",
      headers: getAuthHeaders(),
      body: form,
    });

    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `Finalize recording failed (${res.status})`);
    }

    return res.json() as Promise<FinalizeRecordingResponse>;
  },

  // Free-text AI triage analysis for Manual Case Entry's "Generate AI
  // Suggestion". Reuses the same engine as the live pipeline.
  analyzeText(text: string) {
    return request<AnalyzeTextResponse>("/inference/analyze-text", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  },
};

/* ── Ambulance (Medic) ─────────────────────────────────────────────── */
export const ambulanceApi = {
  myCases() {
    return request<CaseRead[]>("/ambulance/my-cases");
  },

  updateStatus(caseId: number | string, status: string) {
    return request<CaseRead>(`/ambulance/cases/${caseId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  },

  sendToHospital(caseId: number | string) {
    return request<CaseRead>(`/ambulance/cases/${caseId}/send-to-hospital`, {
      method: "POST",
    });
  },

  completeCase(caseId: number | string) {
    return request<CaseRead>(`/ambulance/cases/${caseId}/complete`, {
      method: "POST",
    });
  },
};

/* ── Hospital ──────────────────────────────────────────────────────── */
export const hospitalApi = {
  incoming() {
    return request<CaseRead[]>("/hospital/incoming");
  },

  getCase(caseId: number | string) {
    return request<CaseRead>(`/hospital/cases/${caseId}`);
  },

  completeCase(caseId: number | string) {
    return request<CaseRead>(`/hospital/cases/${caseId}/complete`, {
      method: "POST",
    });
  },
};

/* ── Admin ──────────────────────────────────────────────────────────── */
export const adminApi = {
  getAuditLogs(params?: Record<string, string>) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return request<AuditLogRead[]>(`/admin/audit-logs${qs}`);
  },

  getUsers() {
    return request<UserRead[]>("/admin/users");
  },

  createUser(data: AdminUserCreate) {
    return request<UserRead>("/admin/users", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  updateUser(id: number, data: AdminUserUpdate) {
    return request<UserRead>(`/admin/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  async deleteUser(id: number): Promise<void> {
    // DELETE returns 204 No Content — the shared `request` helper always
    // parses JSON, so handle this one directly to tolerate empty bodies.
    const res = await fetch(`${API_BASE}/admin/users/${id}`, {
      method: "DELETE",
      headers: getAuthHeaders(),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const detail =
        typeof body?.detail === "string"
          ? body.detail
          : `Request failed (${res.status})`;
      throw new Error(detail);
    }
  },
};
