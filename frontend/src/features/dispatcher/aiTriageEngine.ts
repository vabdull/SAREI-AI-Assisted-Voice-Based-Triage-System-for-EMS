// Client-side helpers that shape and sanitise the AI triage analysis for
// the dispatcher UI: defensive coercion of the server payload, patient-count
// inference from Arabic transcript text, and mapping transcript highlight
// offsets onto individual message bubbles.
import type {
  AIHighlight,
  AITriageAnalysis,
  AITriageAssessment,
  PatientLocation,
} from "../../types/api";

export type TriagePriorityValue = "red" | "yellow" | "green";

export interface DispatcherCaseSummary {
  symptoms: string[];
  location: string;
  patients: number;
  condition: string;
  notes: string;
}

export interface LocalHighlightRange {
  start: number;
  end: number;
  label: string;
}

const EMPTY_AI_ANALYSIS: AITriageAnalysis = {
  highlights: [],
  medical_entities: {
    symptoms: [],
    injuries: [],
    patient_state: {
      consciousness: "unknown",
      breathing: "unknown",
      bleeding: "unknown",
    },
    risk_factors: [],
    mechanism_of_injury: [],
    resolved_clues: [],
    timeline_clues: [],
  },
  triage: {
    level: "green",
    confidence: 0,
    reasoning: [],
    needs_confirmation: true,
  },
  patient_location: null,
  meta: {
    engine_version: "ai_v2",
    language: "ar",
    dialect_handling: true,
  },
};

function asNonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function clampConfidence(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
}

function sanitizePatientLocation(raw: unknown): PatientLocation | null {
  if (!raw || typeof raw !== "object") return null;
  const candidate = raw as Record<string, unknown>;
  const rawText = asNonEmptyString(candidate.raw_text);
  if (!rawText) return null;

  const compsRaw = (candidate.components ?? {}) as Record<string, unknown>;
  const spanRaw = candidate.source_span as Record<string, unknown> | null | undefined;
  const geocodeRaw = candidate.geocode as Record<string, unknown> | null | undefined;

  let sourceSpan: PatientLocation["source_span"] = null;
  if (
    spanRaw &&
    typeof spanRaw.start === "number" &&
    typeof spanRaw.end === "number" &&
    spanRaw.end > spanRaw.start
  ) {
    sourceSpan = { start: spanRaw.start, end: spanRaw.end };
  }

  let geocode: PatientLocation["geocode"] = null;
  if (geocodeRaw && typeof geocodeRaw === "object") {
    geocode = {
      lat: typeof geocodeRaw.lat === "number" ? geocodeRaw.lat : null,
      lng: typeof geocodeRaw.lng === "number" ? geocodeRaw.lng : null,
      confidence: clampConfidence(geocodeRaw.confidence),
      provider: asNonEmptyString(geocodeRaw.provider),
      match_type: asNonEmptyString(geocodeRaw.match_type),
    };
  }

  return {
    raw_text: rawText,
    source_span: sourceSpan,
    components: {
      street: asNonEmptyString(compsRaw.street),
      district: asNonEmptyString(compsRaw.district),
      city: asNonEmptyString(compsRaw.city),
      landmark: asNonEmptyString(compsRaw.landmark),
      governorate: asNonEmptyString(compsRaw.governorate),
    },
    geocode,
    confidence: clampConfidence(candidate.confidence),
    needs_confirmation:
      typeof candidate.needs_confirmation === "boolean"
        ? candidate.needs_confirmation
        : true,
  };
}

function sanitizeHighlights(raw: unknown): AIHighlight[] {
  if (!Array.isArray(raw)) return [];
  const highlights: AIHighlight[] = [];
  raw.forEach((item) => {
    if (!item || typeof item !== "object") return;
    const candidate = item as Record<string, unknown>;
    const spanText = asNonEmptyString(candidate.span_text);
    const label = asNonEmptyString(candidate.label);
    const canonicalLabel = asNonEmptyString(candidate.canonical_label) ?? label;
    if (!spanText || !label || !canonicalLabel) return;
    const start = typeof candidate.start === "number" ? candidate.start : null;
    const end = typeof candidate.end === "number" ? candidate.end : null;
    const severity =
      candidate.severity === "high" || candidate.severity === "medium" || candidate.severity === "low"
        ? candidate.severity
        : "medium";
    highlights.push({
      label,
      canonical_label: canonicalLabel,
      span_text: spanText,
      start,
      end,
      severity,
      negated: Boolean(candidate.negated),
      uncertain: Boolean(candidate.uncertain),
      current: candidate.current === false ? false : true,
    });
  });
  return highlights;
}

// A number word counts as a patient count ONLY when an injury/person
// noun sits next to it. This stops age phrases such as
// "عمري ثلاثة وعشرين" (I'm 23) from being misread as "3 patients".
const PATIENT_COUNT_NOUN = "(?:مصابين|مصابون|مصاب|جرحى|جريح|اشخاص|أشخاص|شخص|افراد|أفراد)";
const SPELLED_PATIENT_COUNTS: Record<string, number> = {
  "شخصين": 2, "اثنين": 2, "اثنان": 2, "اتنين": 2,
  "ثلاث": 3, "ثلاثة": 3, "ثلاثه": 3,
  "اربع": 4, "أربع": 4, "اربعة": 4, "اربعه": 4,
  "خمس": 5, "خمسة": 5, "خمسه": 5,
};

function inferPatients(text: string) {
  const source = text || "";

  const digitMatch =
    source.match(new RegExp(`(\\d{1,3})\\s+${PATIENT_COUNT_NOUN}`)) ??
    source.match(new RegExp(`${PATIENT_COUNT_NOUN}\\s+(\\d{1,3})`));
  if (digitMatch) {
    const n = parseInt(digitMatch[1], 10);
    if (n >= 1 && n <= 99) return n;
  }

  if (source.includes("شخصين")) return 2;

  const spelledMatch = source.match(
    new RegExp(`(${Object.keys(SPELLED_PATIENT_COUNTS).join("|")})\\s+${PATIENT_COUNT_NOUN}`),
  );
  if (spelledMatch) {
    const value = SPELLED_PATIENT_COUNTS[spelledMatch[1]];
    if (value) return value;
  }

  return 1;
}

function dedupeStrings(values: string[]) {
  return values.filter((value, index, array) => array.indexOf(value) === index);
}

export function createEmptyAiTriageAnalysis(): AITriageAnalysis {
  return JSON.parse(JSON.stringify(EMPTY_AI_ANALYSIS)) as AITriageAnalysis;
}

export function sanitizeAiTriageAnalysis(raw: unknown): AITriageAnalysis {
  if (!raw || typeof raw !== "object") {
    return createEmptyAiTriageAnalysis();
  }

  const candidate = raw as Record<string, unknown>;
  const base = createEmptyAiTriageAnalysis();
  const triageRaw = (candidate.triage ?? {}) as Record<string, unknown>;
  const level =
    triageRaw.level === "red" || triageRaw.level === "yellow" || triageRaw.level === "green"
      ? triageRaw.level
      : "green";

  const triage: AITriageAssessment = {
    level,
    confidence: clampConfidence(triageRaw.confidence),
    reasoning: Array.isArray(triageRaw.reasoning)
      ? triageRaw.reasoning.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      : [],
    needs_confirmation:
      typeof triageRaw.needs_confirmation === "boolean"
        ? triageRaw.needs_confirmation
        : clampConfidence(triageRaw.confidence) < 0.75,
  };

  return {
    highlights: sanitizeHighlights(candidate.highlights),
    medical_entities: {
      symptoms: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.symptoms)
        ? ((candidate.medical_entities as Record<string, unknown>).symptoms as AITriageAnalysis["medical_entities"]["symptoms"])
        : base.medical_entities.symptoms,
      injuries: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.injuries)
        ? ((candidate.medical_entities as Record<string, unknown>).injuries as AITriageAnalysis["medical_entities"]["injuries"])
        : base.medical_entities.injuries,
      patient_state:
        typeof (candidate.medical_entities as Record<string, unknown> | undefined)?.patient_state === "object" &&
        (candidate.medical_entities as Record<string, unknown> | undefined)?.patient_state !== null
          ? {
              consciousness:
                asNonEmptyString(
                  ((candidate.medical_entities as Record<string, unknown>).patient_state as Record<string, unknown>).consciousness,
                ) ?? "unknown",
              breathing:
                asNonEmptyString(
                  ((candidate.medical_entities as Record<string, unknown>).patient_state as Record<string, unknown>).breathing,
                ) ?? "unknown",
              bleeding:
                asNonEmptyString(
                  ((candidate.medical_entities as Record<string, unknown>).patient_state as Record<string, unknown>).bleeding,
                ) ?? "unknown",
            }
          : base.medical_entities.patient_state,
      risk_factors: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.risk_factors)
        ? (((candidate.medical_entities as Record<string, unknown>).risk_factors as unknown[]).filter(
            (item): item is string => typeof item === "string" && item.trim().length > 0,
          ))
        : [],
      mechanism_of_injury: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.mechanism_of_injury)
        ? (((candidate.medical_entities as Record<string, unknown>).mechanism_of_injury as unknown[]).filter(
            (item): item is string => typeof item === "string" && item.trim().length > 0,
          ))
        : [],
      resolved_clues: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.resolved_clues)
        ? (((candidate.medical_entities as Record<string, unknown>).resolved_clues as unknown[]).filter(
            (item): item is string => typeof item === "string" && item.trim().length > 0,
          ))
        : [],
      timeline_clues: Array.isArray((candidate.medical_entities as Record<string, unknown> | undefined)?.timeline_clues)
        ? (((candidate.medical_entities as Record<string, unknown>).timeline_clues as unknown[]).filter(
            (item): item is string => typeof item === "string" && item.trim().length > 0,
          ))
        : [],
    },
    triage,
    patient_location: sanitizePatientLocation(candidate.patient_location),
    meta: {
      engine_version: asNonEmptyString((candidate.meta as Record<string, unknown> | undefined)?.engine_version) ?? "ai_v2",
      language: asNonEmptyString((candidate.meta as Record<string, unknown> | undefined)?.language) ?? "ar",
      dialect_handling:
        typeof (candidate.meta as Record<string, unknown> | undefined)?.dialect_handling === "boolean"
          ? Boolean((candidate.meta as Record<string, unknown>).dialect_handling)
          : true,
    },
  };
}

export function getConfidenceTier(confidence: number | null): "high" | "medium" | "low" | null {
  if (confidence === null) return null;
  if (confidence >= 0.8) return "high";
  if (confidence >= 0.55) return "medium";
  return "low";
}

export function buildCaseSummaryFromAnalysis(
  analysis: AITriageAnalysis,
  transcriptText: string,
): DispatcherCaseSummary {
  const symptoms = dedupeStrings([
    ...analysis.medical_entities.symptoms
      .filter((item) => !item.negated && item.current)
      .map((item) => item.canonical_label),
    ...analysis.medical_entities.injuries
      .filter((item) => !item.negated && item.current)
      .map((item) => item.canonical_label),
    ...analysis.medical_entities.mechanism_of_injury,
  ]).slice(0, 6);

  const condition =
    analysis.triage.level === "red"
      ? `حرجة - ${analysis.triage.reasoning[0] ?? "تحتاج تدخلاً سريعاً"}`
      : analysis.triage.level === "yellow"
        ? `تحت التقييم - ${analysis.triage.reasoning[0] ?? "تحتاج تأكيداً من المرسل"}`
        : symptoms.length > 0
          ? `مبدئياً مستقرة - ${symptoms.slice(0, 2).join(" / ")}`
          : "بانتظار التقييم";

  return {
    symptoms: symptoms.length > 0 ? symptoms : ["بانتظار معلومات إضافية"],
    location: analysis.patient_location?.raw_text ?? "بانتظار تحديد الموقع",
    patients: inferPatients(transcriptText),
    condition,
    notes: "",
  };
}

// Strip rogue latin-only labels the LLM sometimes leaks through (e.g. "exact")
// while still allowing Arabic or mixed Arabic/digit labels (like "ألم 1").
function hasArabic(value: string): boolean {
  return /[\u0600-\u06FF]/.test(value);
}

export function getKeywordLabels(analysis: AITriageAnalysis) {
  const highlightLabels = analysis.highlights
    .filter((item) => !item.negated && item.current)
    .map((item) =>
      hasArabic(item.span_text)
        ? item.span_text
        : hasArabic(item.canonical_label)
          ? item.canonical_label
          : hasArabic(item.label)
            ? item.label
            : "",
    )
    .filter((label): label is string => Boolean(label));

  const mechanismLabels = analysis.medical_entities.mechanism_of_injury.filter(
    (label) => typeof label === "string" && hasArabic(label),
  );

  return dedupeStrings([...highlightLabels, ...mechanismLabels]);
}

export function getEntryHighlightRanges(
  entryText: string,
  globalStart: number,
  globalEnd: number,
  highlights: AIHighlight[],
): LocalHighlightRange[] {
  if (!entryText.trim()) return [];
  return highlights
    .filter((item) => !item.negated && item.current)
    .filter((item) => typeof item.start === "number" && typeof item.end === "number")
    .filter((item) => (item.start as number) < globalEnd && (item.end as number) > globalStart)
    .map((item) => ({
      start: Math.max(0, (item.start as number) - globalStart),
      end: Math.min(entryText.length, (item.end as number) - globalStart),
      label: item.canonical_label,
    }))
    .filter((item) => item.end > item.start)
    .sort((a, b) => a.start - b.start);
}
