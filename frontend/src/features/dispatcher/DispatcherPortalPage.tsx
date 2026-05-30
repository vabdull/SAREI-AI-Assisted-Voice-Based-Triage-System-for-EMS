// Dispatcher Portal: the main call-handling screen. Captures live audio,
// streams it for real-time Arabic transcription + triage, shows the case
// summary (location, patient count, demographics, symptoms), and lets the
// dispatcher edit details, create manual cases, and dispatch to the
// ambulance/hospital. This is the largest, most feature-rich page.
import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Phone,
  PhoneOff,
  Mic,
  MicOff,
  AlertTriangle,
  CheckCircle,
  Clock,
  MapPin,
  User,
  FileText,
  Send,
  Ambulance,
  LogOut,
  Bell,
  Settings,
  RefreshCw,
  TrendingUp,
  Zap,
  Shield,
  ClipboardList,
  Pencil,
} from "lucide-react";
import { authApi, casesApi, dispatcherApi, inferenceApi } from "../../services/api";
import type { UserRead, CaseRead, AITriageAnalysis, AIHighlight } from "../../types/api";
import PortalSwitcher from "../../components/PortalSwitcher";
import {
  type DispatcherCaseSummary,
  type TriagePriorityValue,
  buildCaseSummaryFromAnalysis,
  createEmptyAiTriageAnalysis,
  getConfidenceTier,
  getEntryHighlightRanges,
  getKeywordLabels,
  sanitizeAiTriageAnalysis,
} from "./aiTriageEngine";
import { useTriageWebSocket, type TriageMatch } from "./useTriageWebSocket";
import ManualCaseModal, { type ManualCaseSubmission } from "./ManualCaseModal";
import EditCallInfoModal, { type EditCallInfoValues } from "./EditCallInfoModal";
/**
 * Validate, deduplicate and sort the canonical highlight set the
 * BACKEND produced in ``CaseLiveState.highlights``.
 *
 * Backend is the single source of truth for highlight offsets. This
 * helper does NOT re-search the transcript with ``indexOf`` — that is
 * exactly the substring-matching mistake we're moving away from. It
 * only:
 *
 *  1. drops items with missing / out-of-range offsets,
 *  2. drops items whose recorded span_text doesn't match the live
 *     transcript at the recorded offsets (defensive against stale
 *     payloads from before a transcript correction),
 *  3. drops negated and non-current items,
 *  4. drops overlapping items (longer / earlier wins),
 *  5. returns the rest sorted by ``start``.
 *
 * The result is what ``HighlightedText`` paints, so the dispatcher
 * sees exactly what the backend Arabic-tokenization said.
 */
function selectCanonicalHighlights(
  transcriptText: string,
  highlights: readonly AIHighlight[] | null | undefined,
): AIHighlight[] {
  if (!transcriptText || !Array.isArray(highlights) || highlights.length === 0)
    return [];
  const n = transcriptText.length;
  const cleaned: AIHighlight[] = [];
  for (const h of highlights) {
    if (!h || h.negated || h.current === false) continue;
    if (typeof h.start !== "number" || typeof h.end !== "number") continue;
    if (!Number.isFinite(h.start) || !Number.isFinite(h.end)) continue;
    if (h.start < 0 || h.end <= h.start || h.end > n) continue;
    // Defensive: if the backend's recorded span_text doesn't match
    // the current transcript at the recorded offsets, the payload is
    // stale (transcript was corrected after the highlight was emitted).
    // Drop it rather than render a misaligned slice.
    const slice = transcriptText.slice(h.start, h.end);
    if (h.span_text && slice !== h.span_text) continue;
    cleaned.push(h);
  }
  // Sort by start ascending; on ties prefer longer (more specific) span.
  cleaned.sort(
    (a, b) =>
      (a.start as number) - (b.start as number) ||
      ((b.end as number) - (b.start as number)) -
        ((a.end as number) - (a.start as number)),
  );
  // De-overlap: keep the first highlight at each non-overlapping range.
  const out: AIHighlight[] = [];
  let lastEnd = -1;
  for (const h of cleaned) {
    if ((h.start as number) < lastEnd) continue;
    out.push(h);
    lastEnd = h.end as number;
  }
  return out;
}

// Fallback patient-count guess used only when the backend hasn't yet
// produced a canonical count. A number word is counted ONLY when it sits
// next to an injury/person noun (مصاب/جريح/شخص...). This prevents age
// phrases like "عمري ثلاثة وعشرين" (I'm 23) from being misread as a
// patient count, matching the backend's stricter inference.
const _FAST_COUNT_NOUN = "(?:مصابين|مصابون|مصاب|جرحى|جريح|اشخاص|أشخاص|شخص|افراد|أفراد)";
const _FAST_SPELLED: Record<string, number> = {
  "شخصين": 2, "اثنين": 2, "اثنان": 2, "اتنين": 2,
  "ثلاث": 3, "ثلاثة": 3, "ثلاثه": 3,
  "اربع": 4, "أربع": 4, "اربعة": 4, "اربعه": 4,
  "خمس": 5, "خمسة": 5, "خمسه": 5,
};

function inferPatientsFast(transcriptText: string): number {
  const text = transcriptText || "";

  // Digit forms: "3 مصابين" / "مصابين 3" / "في 3 اشخاص".
  const digitMatch =
    text.match(new RegExp(`(\\d{1,3})\\s+${_FAST_COUNT_NOUN}`)) ??
    text.match(new RegExp(`${_FAST_COUNT_NOUN}\\s+(\\d{1,3})`));
  if (digitMatch) {
    const n = parseInt(digitMatch[1], 10);
    if (n >= 1 && n <= 99) return n;
  }

  // "شخصين" already means "two persons" on its own.
  if (text.includes("شخصين")) return 2;

  // Spelled number directly followed by a count noun (e.g. "ثلاث مصابين").
  const spelledMatch = text.match(
    new RegExp(`(${Object.keys(_FAST_SPELLED).join("|")})\\s+${_FAST_COUNT_NOUN}`),
  );
  if (spelledMatch) {
    const value = _FAST_SPELLED[spelledMatch[1]];
    if (value) return value;
  }

  return 1;
}

function buildFastKeywordLabels(matches: TriageMatch[]): string[] {
  if (!Array.isArray(matches)) return [];
  return Array.from(
    new Set(
      matches
        .filter((match) => match && !match.negated)
        .flatMap((match) => {
          const spans = Array.isArray(match.spans) ? match.spans : [];
          return spans.length > 0
            ? spans.map((span) => (span?.text ?? "").trim())
            : [(match.matched_keyword ?? "").trim()];
        })
        .filter(Boolean),
    ),
  );
}

function extractPreviewLocation(transcriptText: string): string | null {
  const text = transcriptText.trim();
  if (!text) return null;
  const patterns = [
    /(?:عند|جنب|بجنب|قدام|مقابل|قريب من|بالقرب من|ورا|وراء)\s+[^\s،,.]+(?:\s+[^\s،,.]+){0,5}/,
    /(?:في|بحي)\s+(?:حي\s+)?[^\s،,.]+(?:\s+[^\s،,.]+){0,4}/,
    /(?:شارع|طريق|حي|مخرج|جسر|كبري|دوار|اشاره|محطه|محطة|مسجد|مدرسه|مدرسة|مستشفى|ميدان)\s+[^\s،,.]+(?:\s+[^\s،,.]+){0,4}/,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match?.[0]) return match[0].trim();
  }
  return null;
}

/* ── Types ──────────────────────────────────────────────────────────── */
type CallStatus = "idle" | "incoming" | "active" | "ended";
type TriagePriority = TriagePriorityValue | null;
type AmbulanceStatus = "idle" | "assigned" | "en_route" | "on_scene" | "transporting";

type CaseSummaryState = DispatcherCaseSummary;

const EMPTY_CASE_SUMMARY: CaseSummaryState = {
  symptoms: [],
  location: "بانتظار تحديد الموقع",
  patients: 1,
  condition: "بانتظار التقييم",
  notes: "",
};

function cn(...classes: (string | undefined | null | false)[]) {
  return classes.filter(Boolean).join(" ");
}

/* ── Waveform Animation ─────────────────────────────────────────────── */
function Waveform({ active }: { active: boolean }) {
  const bars = Array.from({ length: 20 });
  return (
    <div className="flex items-end justify-center gap-[3px] h-10">
      {bars.map((_, i) => (
        <motion.div
          key={i}
          className="w-[3px] rounded-full"
          style={{ backgroundColor: active ? "#006C35" : "#d1d5db" }}
          animate={
            active
              ? { height: [8, Math.random() * 28 + 8, 8], transition: { duration: 0.5 + Math.random() * 0.5, repeat: Infinity, delay: i * 0.05 } }
              : { height: 6 }
          }
        />
      ))}
    </div>
  );
}

/* ── Triage Badge ───────────────────────────────────────────────────── */
function TriageBadge({ level, size = "sm" }: { level: TriagePriority; size?: "sm" | "lg" }) {
  if (!level) return null;
  const palette = {
    red:    { bg: "bg-red-50",    border: "border-red-200",   text: "text-red-700",   dot: "bg-red-500",    label: "حرجة — Red",    en: "CRITICAL" },
    yellow: { bg: "bg-amber-50",  border: "border-amber-200", text: "text-amber-700", dot: "bg-amber-500",  label: "متوسطة — Yellow", en: "MODERATE" },
    green:  { bg: "bg-emerald-50",border: "border-emerald-200",text: "text-emerald-700",dot: "bg-emerald-500",label: "بسيطة — Green", en: "LOW" },
  } as const;
  // Be defensive: unexpected values from the API (e.g. case.triage_priority
  // outside the {red,yellow,green} union) used to throw on `config.bg`.
  const config = palette[level as keyof typeof palette];
  if (!config) return null;
  if (size === "lg") {
    return (
      <div className={cn("rounded-2xl border-2 p-6 text-center", config.bg, config.border)}>
        <div className={cn("w-5 h-5 rounded-full mx-auto mb-3 shadow-lg", config.dot)} style={{ boxShadow: `0 0 16px 4px ${level === "red" ? "#ef4444" : level === "yellow" ? "#f59e0b" : "#10b981"}66` }} />
        <div className={cn("text-3xl font-bold", config.text)}>{config.en}</div>
        <div className={cn("text-sm mt-1 font-medium", config.text)}>{config.label}</div>
      </div>
    );
  }
  return (
    <span className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border", config.bg, config.border, config.text)}>
      <span className={cn("w-1.5 h-1.5 rounded-full", config.dot)} />
      {config.en}
    </span>
  );
}

/* ── Status Pill ────────────────────────────────────────────────────── */
function StatusPill({ status }: { status: AmbulanceStatus }) {
  const map: Record<AmbulanceStatus, { label: string; color: string }> = {
    idle:         { label: "Standby", color: "bg-gray-100 text-gray-600" },
    assigned:     { label: "Assigned", color: "bg-blue-100 text-blue-700" },
    en_route:     { label: "En Route", color: "bg-amber-100 text-amber-700" },
    on_scene:     { label: "On Scene", color: "bg-orange-100 text-orange-700" },
    transporting: { label: "Transporting", color: "bg-purple-100 text-purple-700" },
  };
  const { label, color } = map[status];
  return <span className={cn("px-3 py-1 rounded-full text-xs font-semibold", color)}>{label}</span>;
}

/* ── Transcript highlight ───────────────────────────────────────────── */
function HighlightedText({
  text,
  highlights,
}: {
  text: string;
  highlights: { start: number; end: number; label: string }[];
}) {
  if (!highlights.length) return <span>{text}</span>;
  const parts: Array<{ text: string; highlighted: boolean; key: string }> = [];
  let cursor = 0;

  highlights.forEach((highlight, index) => {
    const start = Math.max(cursor, highlight.start);
    const end = Math.min(text.length, highlight.end);
    if (start > cursor) {
      parts.push({ text: text.slice(cursor, start), highlighted: false, key: `plain-${index}-${cursor}` });
    }
    if (end > start) {
      parts.push({ text: text.slice(start, end), highlighted: true, key: `mark-${index}-${start}` });
      cursor = end;
    }
  });

  if (cursor < text.length) {
    parts.push({ text: text.slice(cursor), highlighted: false, key: `plain-tail-${cursor}` });
  }

  return (
    <>
      {parts.map((part) =>
        part.highlighted ? (
          <mark key={part.key} className="bg-amber-100 text-amber-800 rounded px-0.5 font-semibold">
            {part.text}
          </mark>
        ) : (
          <span key={part.key}>{part.text}</span>
        ),
      )}
    </>
  );
}

/* ── Panel Card ─────────────────────────────────────────────────────── */
function Panel({ title, icon, children, className, badge }: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  badge?: React.ReactNode;
}) {
  return (
    <div className={cn("bg-white rounded-2xl border border-[#e4e2db] shadow-sm overflow-hidden", className)}>
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#f0ede6] bg-[#faf9f6]">
        <div className="flex items-center gap-2.5">
          <span className="text-[#006C35]">{icon}</span>
          <h2 className="text-sm font-semibold text-gray-700 tracking-wide uppercase">{title}</h2>
        </div>
        {badge}
      </div>
      <div className="p-5">{children}</div>
    </div>
  );
}

type PendingSnapshot = {
  blob: Blob;
  mimeType?: string;
  segmentId: number;
};

type TranscriptEntry = {
  id: string;
  text: string;
  timestamp: Date;
  segmentId: number;
  globalStart: number;
  globalEnd: number;
};

/* ── Live capture constants ─────────────────────────────────────────── */
// Let the caller complete a natural sentence and cut mainly on actual silence.
// Keep only a long safety cap so a continuously-open recorder still flushes
// eventually if the caller never pauses.
const MAX_PHRASE_DURATION_MS = 20_000;
const SILENCE_DURATION_MS = 900;
const SILENCE_RMS_THRESHOLD = 0.018; // Audio energy threshold for "someone is speaking"
const AI_ANALYSIS_DEBOUNCE_MS = 50;
// After kicking off analysis, keep polling the cached result this often so
// triage/keywords appear the instant Ollama finishes in the background.
const AI_ANALYSIS_POLL_INTERVAL_MS = 150;
const PREVIEW_EMIT_BUFFER_SIZE = 2048;

/* ── Main Dashboard ─────────────────────────────────────────────────── */
export default function DispatcherPortalPage() {
  const navigate = useNavigate();
  const transcriptRef = useRef<HTMLDivElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const previewAsrWsRef = useRef<WebSocket | null>(null);
  const previewProcessorRef = useRef<ScriptProcessorNode | null>(null);
  const previewGainNodeRef = useRef<GainNode | null>(null);
  const recordedChunksRef = useRef<Blob[]>([]);
  const transcriptionQueueRef = useRef<Promise<void>>(Promise.resolve());
  const pendingSnapshotRef = useRef<PendingSnapshot | null>(null);
  const isTranscribingRef = useRef(false);
  const streamingActiveRef = useRef(false);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const activeCaseIdRef = useRef<number | null>(null);
  const notificationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const detectedSpeechRef = useRef(false);
  const liveTranscriptTextRef = useRef("");
  const segmentRestartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const preferredMimeTypeRef = useRef<string | undefined>(undefined);
  const segmentSequenceRef = useRef(0);
  const committedTranscriptTextRef = useRef("");
  const transcriptEntriesRef = useRef<TranscriptEntry[]>([]);
  const extractedLocationRef = useRef<string | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const monitorFrameRef = useRef<number | null>(null);
  const lastSpeechAtRef = useRef(0);
  const hasSpeechInSegmentRef = useRef(false);
  const segmentStartedAtRef = useRef(0);
  const analysisTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const analysisInFlightRef = useRef(false);
  const analysisQueuedRef = useRef(false);
  // Highest transcript_revision we have already applied to the UI.
  // Polling responses or websocket triage_insights with an older
  // ``analyzed_revision`` are dropped to prevent stale data from
  // overwriting newer WS-driven state.
  const lastAppliedRevisionRef = useRef<number>(0);
  const llmWarmupStartedRef = useRef(false);
  // Keep the most recent LLM-produced highlights (their span_text is the
  // source of truth). On every transcript change we re-anchor these to the
  // current text and then overlay the instant Arabic matcher so highlights
  // paint the moment the bubble appears.
  const llmHighlightsRef = useRef<AIHighlight[]>([]);
  const lastPreviewTranscriptRef = useRef("");

  const [user, setUser] = useState<UserRead | null>(null);
  const [cases, setCases] = useState<CaseRead[]>([]);

  /* call state */
  const [callStatus, setCallStatus] = useState<CallStatus>("idle");
  const [muted, setMuted] = useState(false);
  const [callSeconds, setCallSeconds] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [isRequestingMic, setIsRequestingMic] = useState(false);

  /* triage */
  const [triage, setTriage] = useState<TriagePriority>(null);
  const [confidence, setConfidence] = useState<number | null>(null);
  const [needsConfirmation, setNeedsConfirmation] = useState(false);
  const [activeCaseId, setActiveCaseId] = useState<number | null>(null);
  const [manualModalOpen, setManualModalOpen] = useState(false);
  const [editInfoOpen, setEditInfoOpen] = useState(false);
  // Dispatcher manual corrections to the live-call info. When set, these
  // WIN over the AI-extracted (canonical) values in the summary panel so
  // a later ASR chunk can't clobber a correction the dispatcher made.
  const [infoOverride, setInfoOverride] = useState<{
    location: string | null;
    patients: number | null;
    symptoms: string[] | null;
    name: string | null;
    age: number | null;
    gender: string | null;
  } | null>(null);

  /* transcript */
  const [liveTranscriptText, setLiveTranscriptText] = useState("");
  const [previewTranscriptText, setPreviewTranscriptText] = useState("");
  const [aiAnalysis, setAiAnalysis] = useState<AITriageAnalysis>(createEmptyAiTriageAnalysis());
  const [transcriptEntries, setTranscriptEntries] = useState<TranscriptEntry[]>([]);
  const [isTyping, setIsTyping] = useState(false);
  const confidenceTier = getConfidenceTier(confidence);

  // Transcript + ASR are stable again, so keep the instant fast-path enabled
  // for low-latency highlights and triage while the LLM analysis catches up.
  const FAST_PATH_ENABLED = true;

  /* fast-path triage (instant WS client) */
  const {
    fastResult,
    previewFastResult,
    latestInsight,
    connected: _triageConnected,
    sendReset: _sendTriageReset,
    sendPreviewChunk,
    liveState,
  } = useTriageWebSocket(activeCaseId, {
    enabled: FAST_PATH_ENABLED && callStatus === "active",
  });

  const previewKeywordLabels = buildFastKeywordLabels(previewFastResult.matches);
  const committedKeywordLabels = buildFastKeywordLabels(fastResult.matches);
  const previewLocation =
    previewTranscriptText.trim().length > 0
      ? extractPreviewLocation(previewTranscriptText)
      : null;

  // Highlighting is fast-layer only: we render exclusively the canonical,
  // backend-grounded highlights from ``CaseLiveState.highlights``. The
  // backend produces token-aware offsets using Arabic word boundaries, so
  // we only validate and render them — we never re-anchor via indexOf,
  // which historically caused false positives like ``سم`` inside ``اسمي``
  // (see ``selectCanonicalHighlights`` for the validation rules).
  //
  // The LLM ("enriched") layer still owns the triage level, confidence,
  // and reasoning, but not the highlighted words — keeping painting on the
  // deterministic matcher is more precise and avoids late pop-in.
  const liveStateHighlights = liveState?.highlights ?? null;
  const effectiveHighlights: AIHighlight[] = selectCanonicalHighlights(
    liveTranscriptText,
    liveStateHighlights,
  );

  // Preview bubble is intentionally NOT highlighted: the fast matcher
  // only grounds highlights against finalized (silence-closed) chunks,
  // and we no longer fall back to LLM highlights here. Words light up
  // the moment the preview text becomes a committed transcript entry.
  const previewHighlights: AIHighlight[] = [];
  const callerKeywords = Array.from(
    new Set([
      ...previewKeywordLabels,
      ...committedKeywordLabels,
      ...getKeywordLabels(aiAnalysis),
    ]),
  );
  // The triage BADGE (level + confidence + reasoning) is LLM-OWNED.
  // The fast deterministic layer is used ONLY for highlighting and no
  // longer sets the badge — so there is intentionally no fast-path
  // setTriage() here. The badge is filled exclusively by
  // ``applyAiAnalysis`` once the LLM responds.

  // Consume the WS-pushed enriched analysis (triage_insight). This is
  // the primary delivery channel for LLM output now; the /live-analysis
  // poller stays as a fallback. We guard against stale insights via
  // ``analyzed_revision`` so a late insight cannot roll the UI back.
  useEffect(() => {
    if (!FAST_PATH_ENABLED) return;
    if (callStatus !== "active") return;
    if (!latestInsight) return;
    if (latestInsight.timed_out) return;
    const insightRev = latestInsight.analyzed_revision ?? 0;
    if (insightRev > 0 && insightRev < lastAppliedRevisionRef.current) {
      return;
    }
    const transcript =
      latestInsight.full_transcript ||
      committedTranscriptTextRef.current ||
      liveTranscriptText;
    if (!transcript) return;
    applyAiAnalysis(latestInsight.analysis, transcript);
    if (insightRev > 0) {
      lastAppliedRevisionRef.current = Math.max(
        lastAppliedRevisionRef.current,
        insightRev,
      );
    }
  }, [
    FAST_PATH_ENABLED,
    callStatus,
    latestInsight,
    liveTranscriptText,
  ]);

  /* case summary */
  const [summary, setSummary] = useState<CaseSummaryState>(EMPTY_CASE_SUMMARY);
  const [editingNotes, setEditingNotes] = useState(false);
  const [notes, setNotes] = useState("");
  const committedFastTranscript =
    committedTranscriptTextRef.current || liveTranscriptText;
  // Reasoning is LLM-ONLY. The fast layer no longer contributes a
  // reasoning narrative — the badge text comes solely from the LLM's
  // analysis.
  const displayedReasoning = aiAnalysis.triage.reasoning;
  // Canonical backend state wins when present; the client-side helpers
  // remain as a safety net during the migration.
  const canonicalLocation = liveState?.location?.raw_text ?? null;
  const canonicalPatientCount = liveState?.patient_count ?? null;
  const canonicalKeywords = liveState?.keywords ?? [];
  // The case backing the live call (for prefilling the edit modal).
  const activeCase = cases.find((c) => c.id === activeCaseId) ?? null;

  const summaryLocation =
    infoOverride?.location ??
    canonicalLocation ??
    previewLocation ??
    extractedLocationRef.current ??
    aiAnalysis.patient_location?.raw_text ??
    summary.location;
  const summaryPatients =
    infoOverride?.patients ??
    canonicalPatientCount ??
    (committedFastTranscript.trim().length > 0
      ? inferPatientsFast(committedFastTranscript)
      : summary.patients);
  const summarySymptoms =
    infoOverride?.symptoms != null
      ? infoOverride.symptoms
      : canonicalKeywords.length > 0
        ? canonicalKeywords
        : committedKeywordLabels.length > 0
          ? committedKeywordLabels
          : callerKeywords.length > 0
            ? callerKeywords
            : // Manual cases have no live keyword stream; fall back to the
              // symptoms captured in the summary state.
              summary.symptoms;

  // Patient demographics: dispatcher edit (override) wins, then the live
  // extracted value, then whatever is persisted on the case record.
  const summaryPatientName =
    infoOverride?.name ?? liveState?.patient_name ?? activeCase?.patient_name ?? null;
  const summaryPatientAge =
    infoOverride?.age ?? liveState?.patient_age ?? activeCase?.patient_age ?? null;
  const summaryPatientGender =
    infoOverride?.gender ?? liveState?.patient_gender ?? activeCase?.patient_gender ?? null;

  /* ambulance */
  const [ambStatus, setAmbStatus] = useState<AmbulanceStatus>("idle");
  const [confirmed, setConfirmed] = useState(false);
  const [sendingCase, setSendingCase] = useState(false);

  /* notification */
  const [notif, setNotif] = useState<string | null>(null);

  const showNotif = (msg: string) => {
    if (notificationTimerRef.current) clearTimeout(notificationTimerRef.current);
    setNotif(msg);
    notificationTimerRef.current = setTimeout(() => setNotif(null), 3000);
  };

  function getAudioFileExtension(mimeType: string | undefined) {
    const mime = (mimeType || "").toLowerCase();
    if (mime.includes("webm")) return "webm";
    if (mime.includes("ogg")) return "ogg";
    if (mime.includes("mp4")) return "mp4";
    if (mime.includes("wav")) return "wav";
    return "webm";
  }

  function joinTranscriptParts(...parts: string[]) {
    return parts
      .map((part) => part.trim())
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function stopAudioMonitoring() {
    if (monitorFrameRef.current !== null) {
      cancelAnimationFrame(monitorFrameRef.current);
      monitorFrameRef.current = null;
    }
    sourceNodeRef.current?.disconnect();
    sourceNodeRef.current = null;
    analyserRef.current?.disconnect();
    analyserRef.current = null;
    previewProcessorRef.current?.disconnect();
    previewProcessorRef.current = null;
    previewGainNodeRef.current?.disconnect();
    previewGainNodeRef.current = null;
    if (audioContextRef.current) {
      void audioContextRef.current.close();
      audioContextRef.current = null;
    }
  }

  function stopPreviewStreaming() {
    previewProcessorRef.current?.disconnect();
    previewProcessorRef.current = null;
    previewGainNodeRef.current?.disconnect();
    previewGainNodeRef.current = null;
    if (previewAsrWsRef.current) {
      try {
        previewAsrWsRef.current.close();
      } catch {
        // swallow
      }
      previewAsrWsRef.current = null;
    }
    lastPreviewTranscriptRef.current = "";
    setPreviewTranscriptText("");
  }

  function stopCurrentPhraseRecorder() {
    clearSegmentRestartTimer();
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state !== "recording") {
      return;
    }
    try {
      recorder.stop();
    } catch (error) {
      console.error("[live-asr] phrase stop failed", error);
    }
  }

  function startAudioMonitoring(stream: MediaStream) {
    stopAudioMonitoring();

    const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) {
      return;
    }

    const context = new AudioContextCtor();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    audioContextRef.current = context;
    sourceNodeRef.current = source;
    analyserRef.current = analyser;

    const buffer = new Uint8Array(analyser.fftSize);

    const tick = () => {
      const activeRecorder = mediaRecorderRef.current;
      if (!streamingActiveRef.current || !activeRecorder || activeRecorder.state !== "recording") {
        monitorFrameRef.current = requestAnimationFrame(tick);
        return;
      }

      analyser.getByteTimeDomainData(buffer);
      let sumSquares = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const normalized = (buffer[i] - 128) / 128;
        sumSquares += normalized * normalized;
      }
      const rms = Math.sqrt(sumSquares / buffer.length);
      const now = Date.now();

      if (rms >= SILENCE_RMS_THRESHOLD) {
        lastSpeechAtRef.current = now;
        hasSpeechInSegmentRef.current = true;
      } else if (
        hasSpeechInSegmentRef.current &&
        now - lastSpeechAtRef.current >= SILENCE_DURATION_MS
      ) {
        hasSpeechInSegmentRef.current = false;
        stopCurrentPhraseRecorder();
      }

      if (
        hasSpeechInSegmentRef.current &&
        now - segmentStartedAtRef.current >= MAX_PHRASE_DURATION_MS
      ) {
        hasSpeechInSegmentRef.current = false;
        stopCurrentPhraseRecorder();
      }

      monitorFrameRef.current = requestAnimationFrame(tick);
    };

    monitorFrameRef.current = requestAnimationFrame(tick);
  }

  function startPreviewStreaming(caseId: number) {
    if (!FAST_PATH_ENABLED) return;
    const token = localStorage.getItem("token");
    const context = audioContextRef.current;
    const source = sourceNodeRef.current;
    if (!token || !context || !source) {
      return;
    }

    stopPreviewStreaming();

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const ws = new WebSocket(
      `${proto}//${host}/api/v1/realtime/ws/${caseId}?token=${encodeURIComponent(token)}`,
    );
    previewAsrWsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "start_stream", encoding: "float32_pcm" }));
      ws.send(
        JSON.stringify({
          type: "audio_config",
          encoding: "float32_pcm",
          sample_rate: context.sampleRate,
        }),
      );

      const processor = context.createScriptProcessor(PREVIEW_EMIT_BUFFER_SIZE, 1, 1);
      const silentGain = context.createGain();
      silentGain.gain.value = 0;
      processor.onaudioprocess = (event) => {
        if (!streamingActiveRef.current || ws.readyState !== WebSocket.OPEN || muted) {
          return;
        }
        const channel = event.inputBuffer.getChannelData(0);
        if (!channel || channel.length === 0) {
          return;
        }
        try {
          ws.send(channel.slice().buffer);
        } catch {
          // swallow
        }
      };
      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(context.destination);
      previewProcessorRef.current = processor;
      previewGainNodeRef.current = silentGain;
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as {
          type?: string;
          text?: string;
          detail?: string;
          chunk_index?: number;
        };
        if (message.type === "transcript") {
          const text = (message.text || "").trim();
          if (!text || text === lastPreviewTranscriptRef.current) {
            return;
          }
          lastPreviewTranscriptRef.current = text;
          setPreviewTranscriptText(text);
          sendPreviewChunk({
            text,
            previewTranscript: text,
            clientSentAtMs: Date.now(),
          });
        }
      } catch {
        // swallow
      }
    };

    ws.onclose = () => {
      previewAsrWsRef.current = null;
    };

    ws.onerror = () => {
      // Preview ASR socket errors are non-fatal; the finalized live-chunk
      // path remains the source of truth, so we intentionally ignore them.
    };
  }

  /* load user & cases */
  useEffect(() => {
    authApi.me().then(setUser).catch(() => navigate("/login", { replace: true }));
  }, [navigate]);

  // Hydrate the recent-cases panel from the backend on mount and on
  // every 30s tick. Without this, switching back to the dispatcher
  // portal (especially as admin via the portal switcher) leaves
  // ``cases`` as the empty initial state because the component
  // remounts and React-local memory is lost. The database is the
  // source of truth — fetch from there.
  const loadCases = useCallback(async () => {
    try {
      const rows = await casesApi.list();
      setCases(rows);
    } catch {
      /* ignore — keep last known list visible on transient errors */
    }
  }, []);

  useEffect(() => {
    void loadCases();
    const timer = setInterval(() => {
      void loadCases();
    }, 30_000);
    return () => clearInterval(timer);
  }, [loadCases]);

  useEffect(() => {
    if (llmWarmupStartedRef.current) {
      return;
    }
    llmWarmupStartedRef.current = true;
    // Warmup is a best-effort optimization; failures are non-fatal.
    void inferenceApi.liveWarmup().catch(() => {});
  }, []);

  /* call timer */
  useEffect(() => {
    if (callStatus === "active") {
      timerRef.current = setInterval(() => setCallSeconds((s) => s + 1), 1000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      if (callStatus === "idle") setCallSeconds(0);
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [callStatus]);

  /* auto scroll transcript */
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [liveTranscriptText, previewTranscriptText]);

  useEffect(() => {
    mediaStreamRef.current?.getAudioTracks().forEach((track) => {
      track.enabled = !muted;
    });
  }, [muted]);

  useEffect(() => {
    return () => {
      if (notificationTimerRef.current) clearTimeout(notificationTimerRef.current);
      if (segmentRestartTimerRef.current) clearTimeout(segmentRestartTimerRef.current);
      if (analysisTimerRef.current) clearTimeout(analysisTimerRef.current);
      stopPreviewStreaming();
      stopAudioMonitoring();
      streamingActiveRef.current = false;
      try {
        if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
          mediaRecorderRef.current.stop();
        }
      } catch {
        // Ignore recorder shutdown races during unmount.
      }
      mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  function formatTime(s: number) {
    const m = Math.floor(s / 60).toString().padStart(2, "0");
    const sec = (s % 60).toString().padStart(2, "0");
    return `${m}:${sec}`;
  }

  function clearSegmentRestartTimer() {
    if (segmentRestartTimerRef.current) {
      clearTimeout(segmentRestartTimerRef.current);
      segmentRestartTimerRef.current = null;
    }
  }

  function clearAnalysisTimer() {
    if (analysisTimerRef.current) {
      clearTimeout(analysisTimerRef.current);
      analysisTimerRef.current = null;
    }
  }

  function resetCallState() {
    activeCaseIdRef.current = null;
    setActiveCaseId(null);
    detectedSpeechRef.current = false;
    recordedChunksRef.current = [];
    pendingSnapshotRef.current = null;
    isTranscribingRef.current = false;
    transcriptionQueueRef.current = Promise.resolve();
    streamingActiveRef.current = false;
    liveTranscriptTextRef.current = "";
    segmentSequenceRef.current = 0;
    committedTranscriptTextRef.current = "";
    transcriptEntriesRef.current = [];
    extractedLocationRef.current = null;
    lastSpeechAtRef.current = 0;
    hasSpeechInSegmentRef.current = false;
    segmentStartedAtRef.current = 0;
    analysisInFlightRef.current = false;
    analysisQueuedRef.current = false;
    lastAppliedRevisionRef.current = 0;
    llmHighlightsRef.current = [];
    lastPreviewTranscriptRef.current = "";
    clearSegmentRestartTimer();
    clearAnalysisTimer();
    preferredMimeTypeRef.current = undefined;
    setLiveTranscriptText("");
    setPreviewTranscriptText("");
    setAiAnalysis(createEmptyAiTriageAnalysis());
    setTranscriptEntries([]);
    setTriage(null);
    setConfidence(null);
    setNeedsConfirmation(false);
    setAmbStatus("idle");
    setConfirmed(false);
    setIsTyping(false);
    setSummary(EMPTY_CASE_SUMMARY);
    setInfoOverride(null);
    setEditInfoOpen(false);
  }

  function applyAiAnalysis(nextAnalysis: AITriageAnalysis, transcriptText: string) {
    const safeAnalysis = sanitizeAiTriageAnalysis(nextAnalysis);

    // Keep the LLM's grounded spans so we can validate them against
    // the next transcript update without another LLM round-trip.
    llmHighlightsRef.current = safeAnalysis.highlights;

    // Validate the backend-grounded offsets against the current
    // transcript. NEVER re-search with indexOf — backend is the
    // single source of truth for highlight offsets.
    const currentTranscript =
      committedTranscriptTextRef.current || transcriptText;
    safeAnalysis.highlights = selectCanonicalHighlights(
      currentTranscript,
      safeAnalysis.highlights,
    );

    setAiAnalysis(safeAnalysis);
    setTriage(safeAnalysis.triage.level);
    setConfidence(safeAnalysis.triage.confidence);
    setNeedsConfirmation(safeAnalysis.triage.needs_confirmation);
    setSummary(buildCaseSummaryFromAnalysis(safeAnalysis, transcriptText));
    const nextLocation = safeAnalysis.patient_location?.raw_text ?? null;
    extractedLocationRef.current = nextLocation;
  }

  function applyTranscript(segmentText: string, segmentId: number) {
    const normalizedSegmentText = segmentText.trim();
    if (!normalizedSegmentText) {
      return;
    }
    const transcriptTimestamp = new Date();
    const nextEntry: TranscriptEntry = {
      id: `segment-${segmentId}`,
      text: normalizedSegmentText,
      timestamp: transcriptTimestamp,
      segmentId,
      globalStart: 0,
      globalEnd: 0,
    };
    const existingIndex = transcriptEntriesRef.current.findIndex((entry) => entry.segmentId === segmentId);
    const mergedEntries =
      existingIndex >= 0
        ? transcriptEntriesRef.current.map((entry, index) => (index === existingIndex ? nextEntry : entry))
        : [...transcriptEntriesRef.current, nextEntry];

    let cursor = 0;
    const nextEntries = mergedEntries.map((entry, index) => {
      const start = cursor;
      const end = start + entry.text.length;
      cursor = end + (index < mergedEntries.length - 1 ? 1 : 0);
      return {
        ...entry,
        globalStart: start,
        globalEnd: end,
      };
    });

    transcriptEntriesRef.current = nextEntries;
    setTranscriptEntries(nextEntries);

    const fullTranscript = joinTranscriptParts(...nextEntries.map((entry) => entry.text));
    committedTranscriptTextRef.current = fullTranscript;
    liveTranscriptTextRef.current = fullTranscript;
    detectedSpeechRef.current = true;
    lastPreviewTranscriptRef.current = "";
    setPreviewTranscriptText("");
    setLiveTranscriptText(fullTranscript);
    setIsTyping(false);

    // Validate the LLM's last backend-grounded spans against the new
    // transcript. The backend already produced correct offsets when
    // the analysis was emitted; if the transcript hasn't moved the
    // span beyond recognition, the validator keeps them as-is. If it
    // has, the highlight drops cleanly rather than re-anchoring via
    // indexOf (which is what would reintroduce false positives).
    const validated = selectCanonicalHighlights(
      fullTranscript,
      llmHighlightsRef.current,
    );
    setAiAnalysis((prev) => ({
      ...prev,
      highlights: validated,
    }));
  }

  async function requestAiAnalysis(caseId: number) {
    const latestTranscript = committedTranscriptTextRef.current.trim();
    if (!latestTranscript) {
      analysisQueuedRef.current = false;
      return;
    }

    if (analysisInFlightRef.current) {
      analysisQueuedRef.current = true;
      return;
    }

    analysisInFlightRef.current = true;
    analysisQueuedRef.current = false;

    try {
      const response = await inferenceApi.liveAnalysis(caseId);
      if (activeCaseIdRef.current !== caseId) {
        return;
      }

      const currentTranscript = committedTranscriptTextRef.current.trim();
      if (!currentTranscript) {
        return;
      }

      // Revision-aware staleness guard. The backend stamps every
      // analysis with the transcript_revision it was produced for; if
      // that's older than what we've already applied (e.g. a WS
      // triage_insight arrived first), drop the polling result so it
      // can't roll the UI backwards.
      const analyzedRevision = response.analyzed_revision ?? 0;
      const currentBackendRevision = response.transcript_revision ?? 0;

      // Drop a polling result that is older than what we've already
      // applied (a newer WS event may have arrived first) so the UI
      // never rolls backwards.
      const isStalePoll =
        analyzedRevision > 0 &&
        analyzedRevision < lastAppliedRevisionRef.current;
      if (!isStalePoll) {
        const analyzedTranscript =
          response.analyzed_transcript_text?.trim() ??
          response.live_transcript_text?.trim() ??
          "";
        if (analyzedTranscript) {
          applyAiAnalysis(response.analysis, analyzedTranscript);
          if (analyzedRevision > 0) {
            lastAppliedRevisionRef.current = Math.max(
              lastAppliedRevisionRef.current,
              analyzedRevision,
            );
          }
        }
      }

      // Keep polling whenever the backend's enriched analysis is still
      // behind the current transcript revision.
      const enrichmentBehind =
        response.analysis_in_progress === true ||
        (currentBackendRevision > 0 &&
          analyzedRevision < currentBackendRevision);
      if (enrichmentBehind) {
        analysisQueuedRef.current = true;
      }
    } catch (error) {
      console.error("[live-ai] analysis request failed", error);
      analysisQueuedRef.current = true;
    } finally {
      analysisInFlightRef.current = false;
      if (analysisQueuedRef.current && activeCaseIdRef.current === caseId) {
        clearAnalysisTimer();
        analysisTimerRef.current = setTimeout(() => {
          analysisTimerRef.current = null;
          void requestAiAnalysis(caseId);
        }, AI_ANALYSIS_POLL_INTERVAL_MS);
      }
    }
  }

  function scheduleAiAnalysis(caseId: number) {
    analysisQueuedRef.current = true;
    clearAnalysisTimer();
    analysisTimerRef.current = setTimeout(() => {
      analysisTimerRef.current = null;
      void requestAiAnalysis(caseId);
    }, AI_ANALYSIS_DEBOUNCE_MS);
  }

  function cleanupStreaming() {
    clearSegmentRestartTimer();
    clearAnalysisTimer();
    stopPreviewStreaming();
    stopAudioMonitoring();
    try {
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
        mediaRecorderRef.current.stop();
      }
    } catch {
      // Ignore recorder teardown races.
    }
    mediaRecorderRef.current = null;
    recordedChunksRef.current = [];
    pendingSnapshotRef.current = null;
    isTranscribingRef.current = false;
    streamingActiveRef.current = false;
    preferredMimeTypeRef.current = undefined;
    transcriptionQueueRef.current = Promise.resolve();
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    mediaStreamRef.current = null;
  }

  async function transcribeSnapshot(caseId: number, snapshot: PendingSnapshot) {
    const { blob, mimeType, segmentId } = snapshot;
    try {
      const extension = getAudioFileExtension(mimeType);
      const file = new File([blob], `live-snapshot-${Date.now()}.${extension}`, {
        type: mimeType || blob.type || "audio/webm",
      });

      const response = await inferenceApi.liveChunk(caseId, file);
      const segmentText = response.text.trim();

      const liveLocation = response.patient_location?.raw_text?.trim() ?? "";
      if (liveLocation) {
        extractedLocationRef.current = liveLocation;
      }

      if (segmentText) {
        applyTranscript(segmentText, segmentId);
        scheduleAiAnalysis(caseId);
      } else if (!detectedSpeechRef.current) {
        setIsTyping(false);
      }
    } catch (error) {
      console.error("[live-asr] transcription chunk failed", error);
      setIsTyping(false);
      showNotif(error instanceof Error ? error.message : "فشل النسخ الصوتي المباشر");
    }
  }

  function flushPendingTranscription() {
    if (isTranscribingRef.current) {
      return transcriptionQueueRef.current;
    }

    transcriptionQueueRef.current = (async () => {
      isTranscribingRef.current = true;
      try {
        while (pendingSnapshotRef.current) {
          const snapshot = pendingSnapshotRef.current;
          pendingSnapshotRef.current = null;
          const caseId = activeCaseIdRef.current;
          if (!caseId) {
            break;
          }
          await transcribeSnapshot(caseId, snapshot);
        }
      } finally {
        isTranscribingRef.current = false;
      }
    })();

    return transcriptionQueueRef.current;
  }

  function enqueueSegmentTranscription(blob: Blob, mimeType: string | undefined, segmentId: number) {
    if (!activeCaseIdRef.current) {
      return transcriptionQueueRef.current;
    }
    if (blob.size === 0) {
      return transcriptionQueueRef.current;
    }

    pendingSnapshotRef.current = { blob, mimeType, segmentId };
    setIsTyping(true);
    return flushPendingTranscription();
  }

  function finalizeStreaming() {
    void transcriptionQueueRef.current.finally(() => {
      window.setTimeout(() => cleanupStreaming(), 750);
    });
  }

  function getPreferredRecorderMimeType() {
    return [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4",
      "audio/ogg;codecs=opus",
      "audio/ogg",
    ].find((mimeType) => MediaRecorder.isTypeSupported(mimeType));
  }

  function startSegmentRecorder(stream: MediaStream, preferredMimeType?: string) {
    if (!streamingActiveRef.current) {
      return;
    }

    preferredMimeTypeRef.current = preferredMimeType;
    recordedChunksRef.current = [];
    clearSegmentRestartTimer();
    const segmentId = segmentSequenceRef.current + 1;
    segmentSequenceRef.current = segmentId;
    hasSpeechInSegmentRef.current = false;
    segmentStartedAtRef.current = Date.now();

    const recorder = preferredMimeType
      ? new MediaRecorder(stream, { mimeType: preferredMimeType })
      : new MediaRecorder(stream);
    mediaRecorderRef.current = recorder;

    recorder.ondataavailable = (event) => {
      if (event.data.size === 0) return;
      recordedChunksRef.current.push(event.data);
    };

    recorder.onstop = () => {
      const mimeType = recorder.mimeType || preferredMimeType || "audio/webm";
      const snapshot = new Blob(recordedChunksRef.current, { type: mimeType });
      recordedChunksRef.current = [];
      if (snapshot.size > 0) {
        void enqueueSegmentTranscription(snapshot, mimeType, segmentId);
      }
      if (streamingActiveRef.current) {
        startSegmentRecorder(stream, preferredMimeTypeRef.current);
      } else {
        finalizeStreaming();
      }
    };

    try {
      recorder.start();
    } catch (error) {
      console.error("[live-asr] recorder.start failed", error);
      return;
    }

    segmentRestartTimerRef.current = setTimeout(() => {
      if (mediaRecorderRef.current !== recorder) return;
      if (recorder.state !== "recording") return;
      stopCurrentPhraseRecorder();
    }, MAX_PHRASE_DURATION_MS);
  }

  async function startCall() {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      showNotif("المتصفح لا يدعم البث الصوتي المباشر");
      return;
    }

    setIsRequestingMic(true);
    resetCallState();
    setCallSeconds(0);
    setIsTyping(true);

    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      mediaStreamRef.current = stream;
      startAudioMonitoring(stream);

      try {
        const createdCase = await casesApi.create({
          patient_location: EMPTY_CASE_SUMMARY.location
            ? {
                raw_text: EMPTY_CASE_SUMMARY.location,
                source_span: null,
                components: {},
                geocode: null,
                confidence: 0,
                needs_confirmation: true,
              }
            : null,
          notes: notes || undefined,
        });
        activeCaseIdRef.current = createdCase.id;
        setActiveCaseId(createdCase.id);
        setCases((prev) => [createdCase, ...prev]);

        streamingActiveRef.current = true;
        const preferredMimeType = getPreferredRecorderMimeType();
        // Warmup is best-effort; a failure must not block starting the call.
        void inferenceApi.liveWarmup().catch(() => {});
        startPreviewStreaming(createdCase.id);
        startSegmentRecorder(stream, preferredMimeType);
        setCallStatus("active");
        showNotif("تم بدء المكالمة");
      } catch (error) {
        cleanupStreaming();
        showNotif(error instanceof Error ? error.message : "فشل بدء جلسة النسخ");
      }
    } catch (error) {
      cleanupStreaming();
      if (error instanceof DOMException) {
        if (error.name === "NotAllowedError") {
          showNotif("تم رفض إذن الميكروفون");
        } else if (error.name === "NotFoundError") {
          showNotif("لم يتم العثور على ميكروفون متصل");
        } else {
          showNotif("تعذر الوصول إلى الميكروفون");
        }
      } else {
        showNotif(error instanceof Error ? error.message : "تعذر الوصول إلى الميكروفون");
      }
    } finally {
      setIsRequestingMic(false);
    }
  }
  function endCall() {
    // Capture the case id BEFORE the refs below are nulled so we can
    // persist the buffered audio as one recording for this call.
    const endedCaseId = activeCaseIdRef.current ?? activeCaseId;
    setCallStatus("ended");
    setIsTyping(false);
    streamingActiveRef.current = false;
    // Immediate cleanup. Order matters: clear the in-flight markers
    // BEFORE timers so a poll-finally callback that fires between
    // ``clearAnalysisTimer`` and the ref nulling can't reschedule a
    // poll for a call we just ended.
    analysisQueuedRef.current = false;
    analysisInFlightRef.current = false;
    lastAppliedRevisionRef.current = 0;
    extractedLocationRef.current = null;
    lastPreviewTranscriptRef.current = "";
    clearAnalysisTimer();
    clearSegmentRestartTimer();
    stopPreviewStreaming();
    // Drop the active case marker so any late polling.finally branch
    // immediately fails its ``activeCaseIdRef.current === caseId``
    // guard and skips the reschedule.
    activeCaseIdRef.current = null;
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch (error) {
        console.error("[live-asr] recorder.stop failed", error);
        finalizeStreaming();
      }
    } else {
      finalizeStreaming();
    }
    showNotif("تم إنهاء المكالمة");

    // Persist the call's buffered audio as ONE recording. Wait for any
    // in-flight chunk uploads (which buffer audio server-side) to settle
    // first, then ask the backend to write the combined WAV. Best-effort:
    // never let a recording failure disrupt ending the call.
    if (endedCaseId != null) {
      void transcriptionQueueRef.current.finally(() => {
        window.setTimeout(() => {
          // Best-effort: a recording failure must not affect call teardown.
          void inferenceApi.finalizeRecording(endedCaseId).catch(() => {});
        }, 1200);
      });
    }
  }

  // ── Manual Case Entry ──────────────────────────────────────────
  // Create a dispatcher-typed case through the SAME endpoint voice
  // cases use (POST /cases/, source="manual"), then immediately
  // dispatch it to the ambulance (and optionally the hospital) via the
  // same dispatch endpoint AI cases use.
  //
  // IMPORTANT: this is a fire-and-forget action — it does NOT touch the
  // live call/summary panel state (triage / summary / activeCaseId).
  // A manual case is fully handled from the modal, so it must not
  // linger in the "calling area" afterwards like an in-progress call.
  async function handleCreateManualCase(
    submission: ManualCaseSubmission,
    includeHospital: boolean,
  ) {
    const created = await casesApi.create(submission.create);
    setCases((prev) => [created, ...prev]);

    // Close the modal up-front so a dispatch failure (e.g. no medic on
    // shift) can't make the dispatcher resubmit and create a DUPLICATE.
    setManualModalOpen(false);

    // Dispatch right away — this is what makes the manual case appear in
    // the medic/hospital portals, exactly like a dispatched AI case.
    const chiefComplaintPayload = submission.summary.symptoms
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
      .slice(0, 8)
      .join(" | ");

    try {
      const dispatched = await dispatcherApi.dispatchCase(created.id, {
        triage_priority: submission.triage,
        notes: submission.summary.notes || null,
        chief_complaint:
          chiefComplaintPayload || submission.create.chief_complaint || null,
        include_hospital: includeHospital,
      });

      setCases((prev) => prev.map((c) => (c.id === dispatched.id ? dispatched : c)));

      const medicName = dispatched.assigned_medic?.full_name;
      const hospitalName = dispatched.assigned_hospital?.full_name;
      const parts: string[] = [];
      if (medicName) parts.push(`المسعف: ${medicName}`);
      if (includeHospital && hospitalName) parts.push(`المستشفى: ${hospitalName}`);
      const detail = parts.length > 0 ? ` — ${parts.join(" | ")}` : "";
      const label = includeHospital
        ? "تم إنشاء الحالة وإرسالها إلى الإسعاف والمستشفى"
        : "تم إنشاء الحالة وإرسالها إلى الإسعاف";
      showNotif(`${label}${detail}`);
    } catch (error) {
      // Create succeeded; only dispatch failed. Don't rethrow (that
      // would reopen the modal and risk a duplicate).
      const message = error instanceof Error ? error.message : "فشل إرسال الحالة";
      showNotif(`تم إنشاء الحالة لكن تعذّر إرسالها: ${message}`);
    }
  }

  // Apply dispatcher edits to the live call: override the on-screen
  // summary (so corrections survive later ASR chunks) and persist the
  // editable fields to the case record so the medic/hospital portals
  // and the eventual dispatch carry them.
  async function handleSaveCallInfo(values: EditCallInfoValues) {
    const rawLocation = [
      values.location,
      values.landmark ? `(${values.landmark})` : "",
    ]
      .filter(Boolean)
      .join(" ")
      .trim();

    // 1) Local display override — wins over canonical/AI values.
    setInfoOverride({
      location: rawLocation || null,
      patients: values.patients,
      symptoms: values.symptoms,
      name: values.patientName || null,
      age: values.age,
      gender: values.gender || null,
    });
    setNotes(values.notes);

    // 2) Persist to the case record (skip if there's no backing case).
    const caseId = activeCaseIdRef.current ?? activeCaseId;
    if (caseId === null) {
      showNotif("تم تحديث المعلومات");
      return;
    }

    const chiefComplaint = values.symptoms
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
      .slice(0, 8)
      .join(" | ");

    await casesApi.update(caseId, {
      patient_name: values.patientName || undefined,
      patient_age: values.age ?? undefined,
      patient_gender: values.gender || undefined,
      patient_count: values.patients,
      notes: values.notes || undefined,
      chief_complaint: chiefComplaint || undefined,
      patient_location: rawLocation
        ? {
            raw_text: rawLocation,
            source_span: null,
            components: { landmark: values.landmark || null },
            geocode: null,
            confidence: 0,
            needs_confirmation: true,
          }
        : undefined,
    });

    // Reflect the persisted case in the local cases list.
    setCases((prev) =>
      prev.map((c) =>
        c.id === caseId
          ? {
              ...c,
              patient_name: values.patientName || c.patient_name,
              patient_age: values.age ?? c.patient_age,
              patient_gender: values.gender || c.patient_gender,
              patient_count: values.patients,
              notes: values.notes || c.notes,
            }
          : c,
      ),
    );
    showNotif("تم حفظ تعديلات الحالة");
  }

  // Shared dispatch handler for the two "Send to ..." buttons.
  //
  // Both buttons hit the same backend endpoint with different
  // ``include_hospital`` flags so the persistence path stays single-
  // source-of-truth. UI mode flag is only used for messaging.
  async function sendCase(includeHospital: boolean) {
    const caseId = activeCaseIdRef.current ?? activeCaseId;
    if (caseId === null) {
      showNotif("لا توجد حالة نشطة للإرسال");
      return;
    }
    if (sendingCase) {
      // Reentrancy guard. Backend is idempotent, but avoid the
      // double-toast/spinner flicker.
      return;
    }

    // Snapshot the highlighted keywords so portals see a populated
    // chief_complaint instead of a blank field. ``summarySymptoms`` is
    // the same list rendered in the "الأعراض المُكتشفة" panel.
    const chiefComplaintPayload = summarySymptoms
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
      .slice(0, 8)
      .join(" | ");

    setSendingCase(true);
    try {
      const dispatched = await dispatcherApi.dispatchCase(caseId, {
        triage_priority: triage,
        notes: notes || null,
        chief_complaint: chiefComplaintPayload || null,
        include_hospital: includeHospital,
      });

      setConfirmed(true);
      setAmbStatus("assigned");
      setSummary((prev) => ({ ...prev, notes }));
      setCases((prev) =>
        prev.map((c) => (c.id === dispatched.id ? dispatched : c)),
      );

      const medicName = dispatched.assigned_medic?.full_name;
      const hospitalName = dispatched.assigned_hospital?.full_name;
      const parts: string[] = [];
      if (medicName) parts.push(`المسعف: ${medicName}`);
      if (includeHospital && hospitalName) parts.push(`المستشفى: ${hospitalName}`);
      const detail = parts.length > 0 ? ` — ${parts.join(" | ")}` : "";
      const label = includeHospital
        ? "تم الإرسال إلى الإسعاف والمستشفى"
        : "تم الإرسال إلى الإسعاف";
      showNotif(`${label}${detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "فشل إرسال الحالة";
      showNotif(message);
    } finally {
      setSendingCase(false);
    }
  }

  function overrideTriage(level: TriagePriority) {
    setTriage(level);
    setNeedsConfirmation(false);
    showNotif("تم تعديل مستوى الأولوية يدوياً");
  }

  const logout = useCallback(() => {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    navigate("/login", { replace: true });
  }, [navigate]);

  /* ── Render ── */
  return (
    <div className="min-h-screen bg-[#f5f4f0] flex flex-col" dir="rtl">

      {/* ── Top Navigation Bar ─────────────────────────────────── */}
      <header className="bg-white border-b border-[#e4e2db] shadow-sm sticky top-0 z-50">
        <div className="max-w-[1600px] mx-auto px-6 h-20 flex items-center justify-between">

          {/* Logo */}
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Sare'i EMS" className="w-20 h-20 rounded-xl object-contain bg-white" />
            <div>
              <div className="text-base font-bold text-[#006C35] leading-tight">سارع</div>
              <div className="text-[10px] text-gray-400 leading-tight tracking-wider uppercase">Sare'i EMS</div>
            </div>
          </div>

          {/* Center — call status */}
          <div className="flex items-center gap-3">
            <AnimatePresence mode="wait">
              {callStatus === "active" && (
                <motion.div
                  key="active"
                  initial={{ opacity: 0, scale: 0.9 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.9 }}
                  className="flex items-center gap-2 bg-red-50 border border-red-200 rounded-full px-4 py-1.5"
                >
                  <span className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                  <span className="text-red-700 text-sm font-semibold">مكالمة نشطة</span>
                  <span className="text-red-600 text-sm font-mono">{formatTime(callSeconds)}</span>
                </motion.div>
              )}
              {callStatus === "idle" && (
                <motion.div key="idle" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-gray-400 text-sm">
                  لا توجد مكالمة نشطة
                </motion.div>
              )}
              {callStatus === "ended" && (
                <motion.div key="ended" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-2 bg-gray-100 border border-gray-200 rounded-full px-4 py-1.5">
                  <span className="text-gray-600 text-sm font-semibold">انتهت المكالمة</span>
                  <span className="text-gray-500 text-sm font-mono">{formatTime(callSeconds)}</span>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* Right — user & actions */}
          <div className="flex items-center gap-3">
            <button
              onClick={() => setManualModalOpen(true)}
              disabled={callStatus === "active"}
              title={callStatus === "active" ? "أنهِ المكالمة الحالية أولاً" : "إنشاء حالة يدوية"}
              className={cn(
                "flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-semibold transition-all",
                callStatus === "active"
                  ? "bg-gray-100 text-gray-400 cursor-not-allowed border border-gray-200"
                  : "text-white hover:opacity-90 active:scale-95",
              )}
              style={
                callStatus === "active"
                  ? {}
                  : { background: "linear-gradient(135deg, #006C35, #00883f)" }
              }
            >
              <ClipboardList className="w-4 h-4" />
              <span className="hidden sm:inline">حالة يدوية</span>
            </button>
            <PortalSwitcher user={user} />
            <span className="relative">
              <Bell className="w-5 h-5 text-gray-400 hover:text-[#006C35] cursor-pointer transition-colors" />
            </span>
            <Settings className="w-5 h-5 text-gray-400 hover:text-[#006C35] cursor-pointer transition-colors" />
            <div className="h-5 w-px bg-gray-200" />
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-sm font-bold" style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}>
                {user?.full_name?.[0]?.toUpperCase() ?? "U"}
              </div>
              <div className="text-right hidden sm:block">
                <div className="text-sm font-semibold text-gray-700 leading-tight">{user?.full_name ?? "..."}</div>
                <div className="text-[11px] text-gray-400 leading-tight">مشغّل طوارئ</div>
              </div>
            </div>
            <button onClick={logout} className="p-2 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      {/* ── Notification Toast ──────────────────────────────────── */}
      <AnimatePresence>
        {notif && (
          <motion.div
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="fixed top-20 left-1/2 -translate-x-1/2 z-50 bg-[#006C35] text-white px-6 py-2.5 rounded-full shadow-xl text-sm font-medium flex items-center gap-2"
          >
            <CheckCircle className="w-4 h-4" />
            {notif}
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── Manual Case Entry modal ────────────────────────────── */}
      <ManualCaseModal
        open={manualModalOpen}
        onClose={() => setManualModalOpen(false)}
        onSubmit={handleCreateManualCase}
      />

      {/* ── Edit live call info modal ──────────────────────────── */}
      <EditCallInfoModal
        open={editInfoOpen}
        onClose={() => setEditInfoOpen(false)}
        initial={{
          location: summaryLocation || "",
          landmark: "",
          patients: summaryPatients || 1,
          symptoms: summarySymptoms,
          patientName: summaryPatientName ?? "",
          age: summaryPatientAge != null ? String(summaryPatientAge) : "",
          gender: summaryPatientGender ?? "",
          notes,
        }}
        onSave={handleSaveCallInfo}
      />

      {/* ── Main grid ──────────────────────────────────────────── */}
      <main className="flex-1 max-w-[1600px] mx-auto w-full px-6 py-6">
        <div className="grid grid-cols-12 gap-5">

          {/* ── Column 1 (left): Call + Triage ─────────────────── */}
          <div className="col-span-12 lg:col-span-3 flex flex-col gap-5">

            {/* Live Call Panel */}
            <Panel
              title="لوحة المكالمة"
              icon={<Phone className="w-4 h-4" />}
              badge={
                callStatus === "active" ? (
                  <span className="flex items-center gap-1 text-xs font-semibold text-red-600 bg-red-50 px-2 py-0.5 rounded-full border border-red-200">
                    <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-pulse" /> LIVE
                  </span>
                ) : null
              }
            >
              {/* Waveform */}
              <div className="mb-5">
                <Waveform active={callStatus === "active" && !muted} />
              </div>

              {/* Timer */}
              <div className="text-center mb-5">
                <div className="font-mono text-4xl font-bold text-gray-800 tracking-tight">
                  {formatTime(callSeconds)}
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  {callStatus === "idle" ? "في الانتظار" : callStatus === "active" ? "مكالمة نشطة" : "انتهت المكالمة"}
                </div>
              </div>

              {/* Call controls */}
              <div className="flex gap-2 justify-center mb-4">
                {callStatus === "idle" && (
                  <div className="flex-1 flex flex-col gap-2">
                    <button
                      onClick={startCall}
                      disabled={isRequestingMic}
                      className="flex items-center justify-center gap-2 py-3 rounded-xl font-semibold text-white transition-all hover:opacity-90 active:scale-95"
                      style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
                    >
                      <Phone className="w-4 h-4" />
                      {isRequestingMic ? "جاري بدء المكالمة..." : "بدء مكالمة"}
                    </button>
                    <button
                      onClick={() => setManualModalOpen(true)}
                      className="flex items-center justify-center gap-2 py-2.5 rounded-xl font-semibold text-sm border border-[#006C35]/30 text-[#006C35] bg-[#006C35]/[0.04] hover:bg-[#006C35]/10 transition-all active:scale-95"
                    >
                      <ClipboardList className="w-4 h-4" />
                      إدخال حالة يدوية
                    </button>
                  </div>
                )}
                {callStatus === "active" && (
                  <>
                    <button
                      onClick={() => setMuted(!muted)}
                      className={cn(
                        "flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl font-semibold text-sm transition-all hover:opacity-90 active:scale-95",
                        muted ? "bg-red-100 text-red-700 border border-red-200" : "bg-gray-100 text-gray-700 border border-gray-200"
                      )}
                    >
                      {muted ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
                      {muted ? "كتم" : "مايكروفون"}
                    </button>
                    <button
                      onClick={endCall}
                      className="flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl font-semibold text-sm bg-red-500 text-white hover:bg-red-600 transition-all active:scale-95"
                    >
                      <PhoneOff className="w-4 h-4" />
                      إنهاء
                    </button>
                  </>
                )}
                {callStatus === "ended" && (
                  <button
                    onClick={() => {
                      setCallStatus("idle");
                      committedTranscriptTextRef.current = "";
                      liveTranscriptTextRef.current = "";
                      transcriptEntriesRef.current = [];
                      extractedLocationRef.current = null;
                      llmHighlightsRef.current = [];
                      lastPreviewTranscriptRef.current = "";
                      setLiveTranscriptText("");
                      setPreviewTranscriptText("");
                      setAiAnalysis(createEmptyAiTriageAnalysis());
                      setTranscriptEntries([]);
                      setTriage(null);
                      setConfidence(null);
                      setNeedsConfirmation(false);
                      setSummary(EMPTY_CASE_SUMMARY);
                    }}
                    className="flex-1 flex items-center justify-center gap-2 py-3 rounded-xl font-semibold text-sm bg-gray-100 text-gray-700 border border-gray-200 hover:bg-gray-200 transition-all active:scale-95"
                  >
                    <RefreshCw className="w-4 h-4" />
                    مكالمة جديدة
                  </button>
                )}
              </div>

              {/* Quick stats */}
              <div className="grid grid-cols-2 gap-2 mt-2">
                <div className="bg-[#f5f4f0] rounded-xl p-3 text-center border border-[#e4e2db]">
                    <div className="text-lg font-bold text-gray-800">{cases.length}</div>
                    <div className="text-[11px] text-gray-400">حالات هذه الجلسة</div>
                </div>
                <div className="bg-[#f5f4f0] rounded-xl p-3 text-center border border-[#e4e2db]">
                  <div className="text-lg font-bold text-[#006C35]">
                    {cases.filter((c) => c.status === "active").length}
                  </div>
                  <div className="text-[11px] text-gray-400">نشطة الآن</div>
                </div>
              </div>
            </Panel>

            {/* AI Triage Panel */}
            <Panel
              title="تصنيف الذكاء الاصطناعي"
              icon={<Zap className="w-4 h-4" />}
              badge={
                confidence !== null && confidenceTier ? (
                  <span className={cn(
                    "text-xs font-semibold px-2 py-0.5 rounded-full border",
                    confidenceTier === "high" ? "bg-emerald-50 text-emerald-700 border-emerald-200" :
                    confidenceTier === "medium" ? "bg-amber-50 text-amber-700 border-amber-200" :
                    "bg-gray-50 text-gray-600 border-gray-200"
                  )}>
                    {confidenceTier === "high" ? "ثقة عالية" : confidenceTier === "medium" ? "ثقة متوسطة" : "ثقة منخفضة"}
                  </span>
                ) : null
              }
            >
              <AnimatePresence mode="wait">
                {triage ? (
                  <motion.div key="triage-result" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="space-y-4">
                    <TriageBadge level={triage} size="lg" />

                    {/* Confidence bar */}
                    {confidence !== null && confidenceTier && (
                      <div>
                        <div className="flex justify-between text-xs text-gray-500 mb-1.5">
                          <span>مستوى الثقة</span>
                          <span className="font-semibold">{Math.round(confidence * 100)}%</span>
                        </div>
                        <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                          <motion.div
                            initial={{ width: 0 }}
                            animate={{ width: `${Math.round(confidence * 100)}%` }}
                            transition={{ duration: 0.8, ease: "easeOut" }}
                            className={cn("h-full rounded-full", confidenceTier === "high" ? "bg-emerald-500" : confidenceTier === "medium" ? "bg-amber-500" : "bg-gray-400")}
                          />
                        </div>
                      </div>
                    )}

                    {needsConfirmation && (
                      <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                        التحليل يحتاج تأكيداً من المرسل قبل الاعتماد النهائي.
                      </div>
                    )}

                    {displayedReasoning.length > 0 && (
                      <div>
                        <p className="text-xs text-gray-400 mb-2">أسباب التوصية:</p>
                        <div className="flex flex-wrap gap-2">
                          {displayedReasoning.map((reason) => (
                            <span key={reason} className="px-2.5 py-1 rounded-full bg-slate-50 text-slate-700 border border-slate-200 text-xs">
                              {reason}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Override triage */}
                    <div>
                      <p className="text-xs text-gray-400 mb-2">تعديل يدوي:</p>
                      <div className="flex gap-2">
                        {(["red", "yellow", "green"] as TriagePriority[]).map((lvl) => (
                          <button
                            key={lvl}
                            onClick={() => overrideTriage(lvl)}
                            className={cn(
                              "flex-1 py-1.5 rounded-lg text-xs font-semibold border transition-all hover:opacity-80",
                              lvl === "red" ? "bg-red-50 text-red-700 border-red-200" :
                              lvl === "yellow" ? "bg-amber-50 text-amber-700 border-amber-200" :
                              "bg-emerald-50 text-emerald-700 border-emerald-200",
                              triage === lvl ? "ring-2 ring-offset-1 ring-current" : ""
                            )}
                          >
                            {lvl === "red" ? "🔴 حرجة" : lvl === "yellow" ? "🟡 متوسطة" : "🟢 بسيطة"}
                          </button>
                        ))}
                      </div>
                    </div>
                  </motion.div>
                ) : (
                  <motion.div key="triage-waiting" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-center py-8">
                    <div className="w-14 h-14 rounded-2xl mx-auto mb-3 flex items-center justify-center" style={{ background: "#f0f8f4" }}>
                      <Shield className="w-7 h-7 text-[#006C35] opacity-40" />
                    </div>
                    <p className="text-sm text-gray-400">في انتظار بيانات المكالمة...</p>
                    <p className="text-xs text-gray-300 mt-1">سيبدأ التحليل تلقائياً</p>
                  </motion.div>
                )}
              </AnimatePresence>
            </Panel>
          </div>

          {/* ── Column 2 (center): Transcript ──────────────────── */}
          <div className="col-span-12 lg:col-span-5">
            <Panel
              title="النسخ الفوري للمكالمة"
              icon={<FileText className="w-4 h-4" />}
              className="h-full flex flex-col"
              badge={
                isTyping ? (
                  <span className="flex items-center gap-1 text-xs text-[#006C35] font-medium">
                    <span className="flex gap-0.5">
                      <span className="w-1 h-1 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-1 h-1 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-1 h-1 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </span>
                    جاري النسخ...
                  </span>
                ) : null
              }
            >
              {/* Keyword legend */}
              <div className="flex flex-wrap gap-1.5 mb-4 pb-4 border-b border-[#f0ede6]">
                <span className="text-xs text-gray-400 ml-1">كلمات مفتاحية:</span>
                {callerKeywords.length > 0 ? (
                  callerKeywords.map((kw) => (
                    <span key={kw} className="px-2 py-0.5 bg-amber-50 text-amber-700 border border-amber-200 rounded-full text-xs font-medium">
                      {kw}
                    </span>
                  ))
                ) : (
                  <span className="text-xs text-gray-300">لا توجد كلمات مفتاحية بعد</span>
                )}
              </div>

              {/* Transcript scroll area */}
              <div
                ref={transcriptRef}
                className="flex flex-col gap-3 overflow-y-auto flex-1"
                style={{ minHeight: 320, maxHeight: 460 }}
                dir="rtl"
              >
                <AnimatePresence>
                  {transcriptEntries.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-full text-center py-12">
                      <Mic className="w-10 h-10 text-gray-200 mb-3" />
                      <p className="text-sm text-gray-400">
                        {callStatus === "active" ? "جاري الاستماع للمتصل..." : "ابدأ المكالمة لعرض النسخ"}
                      </p>
                      <p className="text-xs text-gray-300 mt-1">
                        {callStatus === "active" ? "سيظهر النص هنا كل بضع ثوانٍ أثناء الكلام" : "سيظهر كلام المتصل هنا أثناء المكالمة"}
                      </p>
                    </div>
                  ) : (
                    transcriptEntries.map((entry) => (
                      <motion.div
                        key={entry.id}
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="flex gap-3 items-start"
                      >
                        <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 mt-0.5 bg-blue-100 text-blue-700">
                          م
                        </div>

                        <div className="max-w-[80%] rounded-2xl px-4 py-2.5 bg-[#f0f8f4] border border-[#d4eddf] rounded-tr-sm">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-[11px] font-semibold text-[#006C35]">المتصل</span>
                            <span className="text-[10px] text-gray-300">
                              {entry.timestamp.toLocaleTimeString("ar-SA", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                            </span>
                          </div>
                          <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap" dir="rtl">
                            <HighlightedText
                              text={entry.text}
                              highlights={getEntryHighlightRanges(
                                entry.text,
                                entry.globalStart,
                                entry.globalEnd,
                                effectiveHighlights,
                              )}
                            />
                          </p>
                        </div>
                      </motion.div>
                    ))
                  )}
                </AnimatePresence>

                {/* Typing indicator */}
                {previewTranscriptText.trim().length > 0 && (
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="flex gap-3 items-start"
                  >
                    <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center text-xs font-bold text-blue-700">
                      م
                    </div>
                    <div className="max-w-[80%] rounded-2xl px-4 py-2.5 bg-[#fdf8e8] border border-amber-200 rounded-tr-sm">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-[11px] font-semibold text-amber-700">المتصل</span>
                        <span className="text-[10px] text-amber-400">مباشر</span>
                      </div>
                      <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap" dir="rtl">
                        <HighlightedText
                          text={previewTranscriptText}
                          highlights={previewHighlights
                            .map((highlight) => ({
                              start: highlight.start ?? 0,
                              end: highlight.end ?? 0,
                              label: highlight.label,
                            }))
                            .filter((highlight) => highlight.end > highlight.start)}
                        />
                      </p>
                    </div>
                  </motion.div>
                )}

                {isTyping && (
                  <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex gap-3 items-center">
                    <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center text-xs font-bold text-blue-700">م</div>
                    <div className="bg-[#f0f8f4] border border-[#d4eddf] rounded-2xl rounded-tr-sm px-4 py-3 flex gap-1">
                      <span className="w-1.5 h-1.5 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-1.5 h-1.5 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-1.5 h-1.5 bg-[#006C35] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </div>
                  </motion.div>
                )}
              </div>
            </Panel>
          </div>

          {/* ── Column 3 (right): Summary + Action ─────────────── */}
          <div className="col-span-12 lg:col-span-4 flex flex-col gap-5">

            {/* Case Summary Panel */}
            <Panel
              title="ملخص الحالة"
              icon={<FileText className="w-4 h-4" />}
              badge={
                <div className="flex items-center gap-2">
                  {activeCaseId !== null && (
                    <button
                      onClick={() => setEditInfoOpen(true)}
                      title="تعديل معلومات الحالة"
                      className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold text-[#006C35] bg-[#006C35]/[0.06] border border-[#006C35]/20 hover:bg-[#006C35]/12 transition active:scale-95"
                    >
                      <Pencil className="w-3 h-3" />
                      تعديل
                    </button>
                  )}
                  {triage ? <TriageBadge level={triage} /> : null}
                </div>
              }
            >
              <AnimatePresence mode="wait">
                {triage || liveTranscriptText.length > 0 ? (
                  <motion.div key="summary-data" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-4">

                    {/* Location */}
                    <div className="flex items-start gap-3 p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
                      <MapPin className="w-4 h-4 text-[#006C35] mt-0.5 flex-shrink-0" />
                      <div>
                        <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide mb-0.5">الموقع</div>
                        <div className="text-sm text-gray-800">{summaryLocation}</div>
                      </div>
                    </div>

                    {/* Patients */}
                    <div className="flex items-start gap-3 p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
                      <User className="w-4 h-4 text-[#006C35] mt-0.5 flex-shrink-0" />
                      <div>
                        <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide mb-0.5">المصابون</div>
                        <div className="text-sm text-gray-800">{summaryPatients} مصاب</div>
                      </div>
                    </div>

                    {/* Patient demographics (name / age / gender) */}
                    <div className="flex items-start gap-3 p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
                      <User className="w-4 h-4 text-[#006C35] mt-0.5 flex-shrink-0" />
                      <div className="flex-1">
                        <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide mb-1.5">بيانات المريض</div>
                        <div className="grid grid-cols-3 gap-2">
                          <div>
                            <div className="text-[10px] text-gray-400 mb-0.5">الاسم</div>
                            <div className="text-sm text-gray-800 truncate">
                              {summaryPatientName || <span className="text-gray-300">—</span>}
                            </div>
                          </div>
                          <div>
                            <div className="text-[10px] text-gray-400 mb-0.5">العمر</div>
                            <div className="text-sm text-gray-800">
                              {summaryPatientAge != null ? `${summaryPatientAge} سنة` : <span className="text-gray-300">—</span>}
                            </div>
                          </div>
                          <div>
                            <div className="text-[10px] text-gray-400 mb-0.5">الجنس</div>
                            <div className="text-sm text-gray-800">
                              {summaryPatientGender || <span className="text-gray-300">—</span>}
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Symptoms */}
                    <div>
                      <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide mb-2">الأعراض المُكتشفة</div>
                      <div className="flex flex-wrap gap-2">
                        {summarySymptoms.map((s, i) => (
                          <span key={i} className="px-2.5 py-1 bg-red-50 text-red-700 border border-red-200 rounded-full text-xs font-medium flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            {s}
                          </span>
                        ))}
                      </div>
                    </div>

                    {/* Notes */}
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide">ملاحظات المشغّل</div>
                        <button
                          onClick={() => setEditingNotes(!editingNotes)}
                          className="text-xs text-[#006C35] hover:underline font-medium"
                        >
                          {editingNotes ? "حفظ" : "تعديل"}
                        </button>
                      </div>
                      {editingNotes ? (
                        <textarea
                          value={notes}
                          onChange={(e) => setNotes(e.target.value)}
                          placeholder="أضف ملاحظاتك هنا..."
                          className="w-full text-sm border border-[#e4e2db] rounded-xl p-3 focus:outline-none focus:ring-2 resize-none text-right bg-white"
                          dir="rtl"
                          rows={3}
                        />
                      ) : (
                        <p className="text-sm text-gray-500 bg-[#f5f4f0] rounded-xl p-3 border border-[#e4e2db] min-h-[64px]" dir="rtl">
                          {notes || "لا توجد ملاحظات..."}
                        </p>
                      )}
                    </div>
                  </motion.div>
                ) : (
                  <motion.div key="summary-empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-center py-8">
                    <TrendingUp className="w-10 h-10 text-gray-200 mx-auto mb-3" />
                    <p className="text-sm text-gray-400">سيتم ملء الملخص تلقائياً</p>
                    <p className="text-xs text-gray-300 mt-1">بعد بدء وتحليل المكالمة</p>
                  </motion.div>
                )}
              </AnimatePresence>
            </Panel>

            {/* Action Panel */}
            <Panel title="لوحة الإجراءات" icon={<Ambulance className="w-4 h-4" />}>
              {/* Ambulance status */}
              <div className="flex items-center justify-between mb-5 p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
                <div className="flex items-center gap-2">
                  <Ambulance className="w-4 h-4 text-[#006C35]" />
                  <span className="text-sm font-medium text-gray-700">حالة الإسعاف</span>
                </div>
                <StatusPill status={ambStatus} />
              </div>

              {/* Timeline */}
              {ambStatus !== "idle" && (
                <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} className="mb-5">
                  <div className="flex items-center gap-0 text-xs">
                    {(["assigned", "en_route", "on_scene", "transporting"] as AmbulanceStatus[]).map((step, i, arr) => {
                      const idx = ["assigned", "en_route", "on_scene", "transporting"].indexOf(ambStatus);
                      const stepIdx = i;
                      const done = stepIdx <= idx;
                      return (
                        <div key={step} className="flex items-center flex-1">
                          <div className={cn("w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0", done ? "bg-[#006C35] text-white" : "bg-gray-100 text-gray-400 border border-gray-200")}>
                            {i + 1}
                          </div>
                          {i < arr.length - 1 && <div className={cn("h-0.5 flex-1", done && stepIdx < idx ? "bg-[#006C35]" : "bg-gray-200")} />}
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex justify-between text-[10px] text-gray-400 mt-1.5 px-0.5">
                    <span>تعيين</span><span>في الطريق</span><span>في الموقع</span><span>نقل</span>
                  </div>
                </motion.div>
              )}

              {/* Action buttons — two-button design.
                  The medic transitions the case through the rest of
                  the lifecycle from the Ambulance Portal (en_route →
                  at_scene → transporting → at_hospital → closed). */}
              <div className="flex flex-col gap-2.5">
                {confirmed ? (
                  <div className="w-full flex items-center justify-center gap-2 py-3 rounded-xl font-semibold text-sm bg-emerald-50 text-emerald-700 border-2 border-emerald-200">
                    <CheckCircle className="w-4 h-4" />
                    تم إرسال الحالة ✓
                  </div>
                ) : (
                  <>
                    <button
                      onClick={() => sendCase(false)}
                      disabled={!triage || sendingCase}
                      className={cn(
                        "w-full flex items-center justify-center gap-2 py-3 rounded-xl font-semibold text-sm border transition-all",
                        triage && !sendingCase
                          ? "bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100 active:scale-95"
                          : "bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed"
                      )}
                    >
                      <Ambulance className="w-4 h-4" />
                      {sendingCase ? "جارٍ الإرسال..." : "إرسال إلى الإسعاف"}
                    </button>

                    <button
                      onClick={() => sendCase(true)}
                      disabled={!triage || sendingCase}
                      className={cn(
                        "w-full flex items-center justify-center gap-2 py-3 rounded-xl font-semibold text-sm transition-all",
                        triage && !sendingCase
                          ? "text-white hover:opacity-90 active:scale-95"
                          : "bg-gray-100 text-gray-400 cursor-not-allowed"
                      )}
                      style={
                        triage && !sendingCase
                          ? { background: "linear-gradient(135deg, #006C35, #00883f)" }
                          : {}
                      }
                    >
                      <Send className="w-4 h-4" />
                      {sendingCase ? "جارٍ الإرسال..." : "إرسال إلى الإسعاف والمستشفى"}
                    </button>
                  </>
                )}
              </div>
            </Panel>
          </div>
        </div>

        {/* ── Recent Cases Bar ──────────────────────────────────── */}
        {cases.length > 0 && (
          <div className="mt-5">
            <Panel title="حالات هذه الجلسة" icon={<Clock className="w-4 h-4" />}>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
                {cases.slice(0, 5).map((c) => (
                  <div
                    key={c.id}
                    className="p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db] hover:border-[#006C35] hover:bg-[#f0f8f4] transition-all cursor-pointer"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-bold text-gray-700">{c.incident_number}</span>
                      <TriageBadge level={c.triage_priority as TriagePriority} />
                    </div>
                    <div className="text-xs text-gray-500 truncate">{c.patient_name || "مجهول"}</div>
                    <div className="text-[11px] text-gray-400 mt-1">{c.status}</div>
                  </div>
                ))}
              </div>
            </Panel>
          </div>
        )}
      </main>

      {/* ── Footer ────────────────────────────────────────────────── */}
      <footer className="bg-white border-t border-[#e4e2db] py-3 px-6">
        <div className="max-w-[1600px] mx-auto flex items-center justify-between">
          <div className="text-[11px] text-gray-400">
            منصة سارع للطوارئ الطبية الموحدة
          </div>
          <div className="flex items-center gap-3 text-[11px] text-gray-400">
            <span>بوابة موجه البلاغات الطارئة</span>
          </div>
        </div>
      </footer>
    </div>
  );
}
