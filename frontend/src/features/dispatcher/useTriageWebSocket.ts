/**
 * Real-time triage WebSocket client.
 *
 * Subscribes to /api/v1/triage/ws/{case_id} and surfaces:
 *   - fastResult: cumulative Layer-1+2 matches and ESI level (≤15ms after each
 *     ASR bubble). Drives instant keyword highlighting. The triage badge
 *     itself is owned by the LLM (enriched) layer, not this fast result.
 *   - latestInsight: the LLM-enriched analysis (1–3s after silence), kept for
 *     components that want the richer narrative.
 *
 * The hook gracefully re-connects with exponential backoff, and calls
 * ``reset`` on the server when the case changes.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { AITriageAnalysis, PatientLocation } from "../../types/api";

export type TriageLevel = "red" | "yellow" | "green";

// ── Canonical live_state payload (CaseLiveState mirror) ────────────────
// The backend now broadcasts a `live_state` message on every merge.
// Reading this is the long-term contract; legacy triage_update /
// triage_insight remain wired for fallback while the UI migrates.

export interface DisplayTriage {
  level: TriageLevel;
  esi: number;
  esi_label_ar: string;
  confidence: number;
  source: "fast" | "enriched" | "merged" | "none";
  reasoning: string[];
  needs_confirmation: boolean;
}

export interface HighlightPayload {
  label: string;
  canonical_label: string;
  span_text: string;
  start: number | null;
  end: number | null;
  severity: "high" | "medium" | "low";
  negated: boolean;
  uncertain: boolean;
  current: boolean;
  source: "fast" | "enriched";
}

export interface CaseLiveState {
  case_id: number;
  transcript_revision: number;
  transcript_text: string;
  transcript_status: "empty" | "preview" | "current" | "finalized";
  preview_transcript_text: string;
  display_triage: DisplayTriage;
  highlights: HighlightPayload[];
  keywords: string[];
  location: PatientLocation | null;
  location_revision: number;
  patient_count: number | null;
  patient_name: string | null;
  patient_age: number | null;
  patient_gender: string | null;
  reasoning: string[];
  chunk_count: number;
  fast_triage_revision: number;
  enriched_triage_revision: number;
  provisional: boolean;
}

export interface LiveStateEvent {
  type: "live_state";
  state: CaseLiveState;
  fast_matches: TriageMatch[];
  analysis?: AITriageAnalysis;
  patient_location?: PatientLocation | null;
  extraction_confidence?: number | null;
  live_transcript_text?: string | null;
}

export interface TriageEvidenceSpan {
  start: number;
  end: number;
  text: string;
}

export interface TriageMatch {
  concept_id: string;
  category: string;
  esi: number;
  weight: number;
  canonical_label_ar: string;
  matched_keyword: string;
  matched_dialect: string;
  fuzzy_score: number;
  is_fuzzy: boolean;
  negated: boolean;
  confidence: number;
  spans: TriageEvidenceSpan[];
  last_seen_at: number;
}

export interface TriageRiskModifier {
  modifier_id: string;
  note_ar: string;
  escalate: boolean;
  trigger: string;
  spans: TriageEvidenceSpan[];
}

export interface TriageFastResult {
  esi: number;
  esi_label_ar: string;
  level: TriageLevel;
  escalated: boolean;
  matches: TriageMatch[];
  modifiers: TriageRiskModifier[];
  processing_time_ms: number;
}

export interface TriageFastEvent {
  type: "triage_update";
  case_id: number;
  chunk_index: number;
  chunk_text: string;
  full_transcript: string;
  result: TriageFastResult;
  chunk_matches: TriageMatch[];
  provisional?: boolean;
  client_sent_at_ms?: number | null;
}

export interface TriageInsightEvent {
  type: "triage_insight";
  case_id: number;
  full_transcript: string;
  analysis: AITriageAnalysis;
  fast_result?: TriageFastResult | null;
  llm_latency_ms?: number | null;
  timed_out: boolean;
  /** Transcript revision the enrichment was produced for. */
  analyzed_revision?: number;
  transcript_revision?: number;
}

export interface TriageSnapshotEvent {
  type: "snapshot";
  case_id: number;
  // ``result`` and ``state`` are both optional from the server: a fresh
  // case has no fast_triage yet so ``result`` arrives as null.
  result: TriageFastResult | null;
  state?: CaseLiveState | null;
}

type TriageMessage =
  | TriageFastEvent
  | TriageInsightEvent
  | TriageSnapshotEvent
  | LiveStateEvent
  | { type: "triage_reset"; case_id: number }
  | { type: "pong" }
  | { type: "error"; detail: string };

const EMPTY_FAST_RESULT: TriageFastResult = {
  esi: 5,
  esi_label_ar: "غير طارئ",
  level: "green",
  escalated: false,
  matches: [],
  modifiers: [],
  processing_time_ms: 0,
};

/**
 * Coerce any websocket payload into a fully-formed TriageFastResult. The
 * backend snapshot path can legitimately send `result: null` when no fast
 * triage has run yet for a brand-new case; dereferencing `.matches` on that
 * null is what historically blanked the dispatcher page on Start Call.
 */
function coerceFastResult(value: unknown): TriageFastResult {
  if (!value || typeof value !== "object") return EMPTY_FAST_RESULT;
  const r = value as Partial<TriageFastResult>;
  return {
    esi: typeof r.esi === "number" ? r.esi : EMPTY_FAST_RESULT.esi,
    esi_label_ar:
      typeof r.esi_label_ar === "string"
        ? r.esi_label_ar
        : EMPTY_FAST_RESULT.esi_label_ar,
    level: r.level ?? EMPTY_FAST_RESULT.level,
    escalated: r.escalated ?? false,
    matches: Array.isArray(r.matches) ? r.matches : [],
    modifiers: Array.isArray(r.modifiers) ? r.modifiers : [],
    processing_time_ms:
      typeof r.processing_time_ms === "number" ? r.processing_time_ms : 0,
  };
}

function coerceMatches(value: unknown): TriageMatch[] {
  return Array.isArray(value) ? (value as TriageMatch[]) : [];
}

const EMPTY_DISPLAY_TRIAGE: DisplayTriage = {
  level: "green",
  esi: 5,
  esi_label_ar: "غير طارئ",
  confidence: 0,
  source: "none",
  reasoning: [],
  needs_confirmation: true,
};

/**
 * Coerce a backend live_state.state payload, tolerating partial /
 * legacy / null inputs. Always returns a fully-formed CaseLiveState so
 * UI components never have to do optional-chain gymnastics.
 */
function coerceLiveState(value: unknown): CaseLiveState | null {
  if (!value || typeof value !== "object") return null;
  const s = value as Partial<CaseLiveState>;
  const dt = (s.display_triage ?? {}) as Partial<DisplayTriage>;
  return {
    case_id: typeof s.case_id === "number" ? s.case_id : 0,
    transcript_revision:
      typeof s.transcript_revision === "number" ? s.transcript_revision : 0,
    transcript_text: typeof s.transcript_text === "string" ? s.transcript_text : "",
    transcript_status: s.transcript_status ?? "empty",
    preview_transcript_text:
      typeof s.preview_transcript_text === "string" ? s.preview_transcript_text : "",
    display_triage: {
      level: dt.level ?? EMPTY_DISPLAY_TRIAGE.level,
      esi: typeof dt.esi === "number" ? dt.esi : EMPTY_DISPLAY_TRIAGE.esi,
      esi_label_ar:
        typeof dt.esi_label_ar === "string"
          ? dt.esi_label_ar
          : EMPTY_DISPLAY_TRIAGE.esi_label_ar,
      confidence:
        typeof dt.confidence === "number"
          ? Math.max(0, Math.min(1, dt.confidence))
          : 0,
      source: dt.source ?? "none",
      reasoning: Array.isArray(dt.reasoning) ? dt.reasoning : [],
      needs_confirmation:
        typeof dt.needs_confirmation === "boolean" ? dt.needs_confirmation : true,
    },
    highlights: Array.isArray(s.highlights) ? s.highlights : [],
    keywords: Array.isArray(s.keywords) ? s.keywords : [],
    location: (s.location as PatientLocation | null) ?? null,
    location_revision:
      typeof s.location_revision === "number" ? s.location_revision : 0,
    patient_count:
      typeof s.patient_count === "number" ? s.patient_count : null,
    patient_name: typeof s.patient_name === "string" ? s.patient_name : null,
    patient_age: typeof s.patient_age === "number" ? s.patient_age : null,
    patient_gender:
      typeof s.patient_gender === "string" ? s.patient_gender : null,
    reasoning: Array.isArray(s.reasoning) ? s.reasoning : [],
    chunk_count: typeof s.chunk_count === "number" ? s.chunk_count : 0,
    fast_triage_revision:
      typeof s.fast_triage_revision === "number" ? s.fast_triage_revision : 0,
    enriched_triage_revision:
      typeof s.enriched_triage_revision === "number"
        ? s.enriched_triage_revision
        : 0,
    provisional: Boolean(s.provisional),
  };
}

interface UseTriageWebSocketResult {
  connected: boolean;
  fastResult: TriageFastResult;
  previewFastResult: TriageFastResult;
  chunkMatches: TriageMatch[];
  previewChunkMatches: TriageMatch[];
  previewTranscript: string;
  latestInsight: TriageInsightEvent | null;
  /**
   * Canonical backend live state — populated whenever the server emits
   * a `live_state` payload or a snapshot containing `state`. Prefer
   * reading from this over the legacy triage_update fast result; the
   * legacy slice is kept for backwards compatibility during the
   * migration.
   */
  liveState: CaseLiveState | null;
  sendReset: () => void;
  sendPreviewChunk: (payload: {
    text: string;
    previewTranscript?: string;
    clientSentAtMs?: number;
  }) => void;
  reconnect: () => void;
}

export function useTriageWebSocket(
  caseId: number | null,
  options: { enabled?: boolean } = {},
): UseTriageWebSocketResult {
  const { enabled = true } = options;
  const [connected, setConnected] = useState(false);
  const [fastResult, setFastResult] =
    useState<TriageFastResult>(EMPTY_FAST_RESULT);
  const [previewFastResult, setPreviewFastResult] =
    useState<TriageFastResult>(EMPTY_FAST_RESULT);
  const [chunkMatches, setChunkMatches] = useState<TriageMatch[]>([]);
  const [previewChunkMatches, setPreviewChunkMatches] = useState<TriageMatch[]>([]);
  const [previewTranscript, setPreviewTranscript] = useState("");
  const [latestInsight, setLatestInsight] =
    useState<TriageInsightEvent | null>(null);
  const [liveState, setLiveState] = useState<CaseLiveState | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const manualCloseRef = useRef(false);

  const clearTimers = () => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (heartbeatTimerRef.current) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
  };

  const connect = useCallback(() => {
    if (!enabled || caseId == null) return;
    const token = localStorage.getItem("token");
    if (!token) return;

    manualCloseRef.current = false;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${proto}//${host}/api/v1/triage/ws/${caseId}?token=${encodeURIComponent(
      token,
    )}`;

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      console.error("[triage-ws] construct failed", err);
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectAttemptRef.current = 0;
      setConnected(true);
      heartbeatTimerRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "ping" }));
          } catch {
            // swallow
          }
        }
      }, 20_000);
    };

    ws.onmessage = (event) => {
      let message: TriageMessage;
      try {
        message = JSON.parse(event.data);
      } catch {
        // Ignore malformed frames; the next valid message will refresh state.
        return;
      }

      switch (message.type) {
        case "snapshot": {
          // The snapshot may include both the legacy `result` and the
          // canonical `state`. Hydrate both so reconnects fully restore
          // the dispatcher UI.
          setFastResult(coerceFastResult(message.result));
          setPreviewFastResult(EMPTY_FAST_RESULT);
          setPreviewChunkMatches([]);
          setPreviewTranscript("");
          const snap = coerceLiveState((message as TriageSnapshotEvent).state);
          if (snap !== null) {
            setLiveState((prev) => {
              if (prev && prev.transcript_revision > snap.transcript_revision) {
                return prev;
              }
              return snap;
            });
          }
          break;
        }
        case "live_state": {
          const next = coerceLiveState(message.state);
          if (next !== null) {
            setLiveState((prev) => {
              // Drop strictly-older revisions; equal revision is fine
              // (lets a re-broadcast refresh transient fields like
              // last_*_ts_ms or display_triage when only the merge
              // changed).
              if (prev && prev.transcript_revision > next.transcript_revision) {
                return prev;
              }
              return next;
            });
          }
          // Mirror fast_matches into the legacy slice so existing UI
          // components keep working without listening to live_state
          // directly. This is a transition shim — once the dispatcher
          // reads from liveState exclusively this can go away.
          if (Array.isArray(message.fast_matches)) {
            setChunkMatches(coerceMatches(message.fast_matches));
          }
          break;
        }
        case "triage_update":
          if (message.provisional) {
            setPreviewFastResult(coerceFastResult(message.result));
            setPreviewChunkMatches(coerceMatches(message.chunk_matches));
            setPreviewTranscript(message.full_transcript ?? message.chunk_text ?? "");
          } else {
            setFastResult(coerceFastResult(message.result));
            setChunkMatches(coerceMatches(message.chunk_matches));
            setPreviewFastResult(EMPTY_FAST_RESULT);
            setPreviewChunkMatches([]);
            setPreviewTranscript("");
          }
          break;
        case "triage_insight":
          setLatestInsight(message);
          if (message.fast_result) {
            setFastResult(coerceFastResult(message.fast_result));
          }
          break;
        case "triage_reset":
          setFastResult(EMPTY_FAST_RESULT);
          setPreviewFastResult(EMPTY_FAST_RESULT);
          setChunkMatches([]);
          setPreviewChunkMatches([]);
          setPreviewTranscript("");
          setLatestInsight(null);
          setLiveState(null);
          break;
        case "pong":
          break;
        case "error":
          // Server-reported pipeline errors are non-fatal for the UI.
          break;
        default:
          break;
      }
    };

    ws.onclose = (ev) => {
      setConnected(false);
      clearTimers();
      wsRef.current = null;
      if (!manualCloseRef.current) {
        // 4401 = auth failure -> don't spin. Other codes -> backoff reconnect.
        if (ev.code !== 4401 && ev.code !== 4403 && ev.code !== 4404) {
          scheduleReconnect();
        }
      }
    };

    ws.onerror = () => {
      // onclose will handle the retry; just keep the error out of the logs.
    };
  }, [caseId, enabled]);

  const scheduleReconnect = useCallback(() => {
    if (manualCloseRef.current) return;
    reconnectAttemptRef.current += 1;
    const delay = Math.min(1000 * 2 ** reconnectAttemptRef.current, 15_000);
    reconnectTimerRef.current = setTimeout(connect, delay);
  }, [connect]);

  const disconnect = useCallback(() => {
    manualCloseRef.current = true;
    clearTimers();
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        // swallow
      }
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  const sendReset = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "reset" }));
      } catch {
        // swallow
      }
    }
    setFastResult(EMPTY_FAST_RESULT);
    setPreviewFastResult(EMPTY_FAST_RESULT);
    setChunkMatches([]);
    setPreviewChunkMatches([]);
    setPreviewTranscript("");
    setLatestInsight(null);
    setLiveState(null);
  }, []);

  const sendPreviewChunk = useCallback(
    (payload: { text: string; previewTranscript?: string; clientSentAtMs?: number }) => {
      const ws = wsRef.current;
      const text = payload.text.trim();
      if (!text || !ws || ws.readyState !== WebSocket.OPEN) {
        return;
      }
      try {
        ws.send(
          JSON.stringify({
            type: "chunk",
            text,
            provisional: true,
            preview_transcript: payload.previewTranscript ?? text,
            client_sent_at_ms: payload.clientSentAtMs ?? Date.now(),
          }),
        );
      } catch {
        // swallow
      }
    },
    [],
  );

  const reconnect = useCallback(() => {
    disconnect();
    setTimeout(connect, 200);
  }, [connect, disconnect]);

  useEffect(() => {
    if (!enabled || caseId == null) {
      disconnect();
      setFastResult(EMPTY_FAST_RESULT);
      setPreviewFastResult(EMPTY_FAST_RESULT);
      setChunkMatches([]);
      setPreviewChunkMatches([]);
      setPreviewTranscript("");
      setLatestInsight(null);
      setLiveState(null);
      return;
    }
    connect();
    return () => {
      disconnect();
    };
  }, [caseId, enabled, connect, disconnect]);

  return {
    connected,
    fastResult,
    previewFastResult,
    chunkMatches,
    previewChunkMatches,
    previewTranscript,
    latestInsight,
    liveState,
    sendReset,
    sendPreviewChunk,
    reconnect,
  };
}
