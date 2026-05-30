// Modal for creating a case manually (text entry) instead of from a live
// call, e.g. when a dispatcher logs a case without recorded audio. Runs
// the same AI triage analysis over the typed description.
import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  Sparkles,
  AlertTriangle,
  Loader2,
  User,
  MapPin,
  FileText,
  Activity,
  ClipboardList,
  Ambulance,
  Hospital,
} from "lucide-react";
import { inferenceApi } from "../../services/api";
import type { AITriageAnalysis, CaseCreate } from "../../types/api";
import { getKeywordLabels, type TriagePriorityValue } from "./aiTriageEngine";

/**
 * Payload the modal hands back to the Dispatcher portal on submit. The
 * parent owns the actual API call + state hydration so the manual case
 * flows through the SAME path as voice cases (create → active list →
 * dispatch). The modal stays a pure, self-contained form.
 */
export interface ManualCaseSubmission {
  create: CaseCreate;
  triage: TriagePriorityValue;
  confidence: number | null;
  summary: {
    location: string;
    patients: number;
    symptoms: string[];
    notes: string;
  };
  analysis: AITriageAnalysis | null;
}

interface ManualCaseModalProps {
  open: boolean;
  onClose: () => void;
  /**
   * Creates the case and dispatches it. ``includeHospital`` forwards
   * the case to the hospital portal in addition to the ambulance.
   * Should throw on failure.
   */
  onSubmit: (submission: ManualCaseSubmission, includeHospital: boolean) => Promise<void>;
}

const GENDER_OPTIONS = [
  { value: "", label: "اختر الجنس" },
  { value: "ذكر", label: "ذكر" },
  { value: "أنثى", label: "أنثى" },
  { value: "غير معروف", label: "غير معروف" },
];

const EMERGENCY_TYPES = [
  { value: "", label: "اختر نوع الطارئة" },
  { value: "حادث مروري", label: "حادث مروري" },
  { value: "إصابة / رضح", label: "إصابة / رضح" },
  { value: "نزيف", label: "نزيف" },
  { value: "صعوبة في التنفس", label: "صعوبة في التنفس" },
  { value: "ألم في الصدر / قلب", label: "ألم في الصدر / قلب" },
  { value: "فقدان وعي", label: "فقدان وعي" },
  { value: "حرق", label: "حرق" },
  { value: "سقوط من ارتفاع", label: "سقوط من ارتفاع" },
  { value: "تسمم", label: "تسمم" },
  { value: "ولادة / حمل", label: "ولادة / حمل" },
  { value: "سكتة دماغية", label: "سكتة دماغية" },
  { value: "أخرى", label: "أخرى" },
];

const CONSCIOUSNESS_OPTIONS = [
  { value: "", label: "غير محدد" },
  { value: "واعٍ", label: "واعٍ" },
  { value: "غير واعٍ", label: "غير واعٍ" },
  { value: "مشوش", label: "مشوش / غير متجاوب جزئياً" },
];

const BREATHING_OPTIONS = [
  { value: "", label: "غير محدد" },
  { value: "يتنفس طبيعي", label: "يتنفس بشكل طبيعي" },
  { value: "صعوبة في التنفس", label: "صعوبة في التنفس" },
  { value: "لا يتنفس", label: "لا يتنفس" },
];

const SEVERITY_OPTIONS: { value: TriagePriorityValue; label: string; tone: string }[] = [
  { value: "red", label: "حرجة (Red)", tone: "text-red-700" },
  { value: "yellow", label: "متوسطة (Yellow)", tone: "text-amber-700" },
  { value: "green", label: "منخفضة (Green)", tone: "text-emerald-700" },
];

const inputClass =
  "w-full rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-sm text-gray-800 " +
  "focus:border-[#006C35] focus:ring-2 focus:ring-[#006C35]/20 focus:outline-none transition";

function Label({ children, required }: { children: React.ReactNode; required?: boolean }) {
  return (
    <label className="block text-[12px] font-semibold text-gray-600 mb-1.5">
      {children}
      {required && <span className="text-red-500 mr-0.5"> *</span>}
    </label>
  );
}

function SectionTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[#006C35] font-bold text-sm mb-3 mt-1">
      {icon}
      {children}
    </div>
  );
}

export default function ManualCaseModal({ open, onClose, onSubmit }: ManualCaseModalProps) {
  const [patientName, setPatientName] = useState("");
  const [age, setAge] = useState("");
  const [gender, setGender] = useState("");
  const [emergencyType, setEmergencyType] = useState("");
  const [symptoms, setSymptoms] = useState("");
  const [consciousness, setConsciousness] = useState("");
  const [breathing, setBreathing] = useState("");
  const [severity, setSeverity] = useState<TriagePriorityValue | "">("");
  const [patientCount, setPatientCount] = useState("1");
  const [address, setAddress] = useState("");
  const [landmark, setLandmark] = useState("");
  const [notes, setNotes] = useState("");

  const [errors, setErrors] = useState<Record<string, string>>({});
  // Tracks which dispatch button is in flight (drives its spinner) and
  // disables both while a request runs.
  const [submittingMode, setSubmittingMode] = useState<"ambulance" | "both" | null>(null);
  const submitting = submittingMode !== null;
  const [formError, setFormError] = useState<string | null>(null);

  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiKeywords, setAiKeywords] = useState<string[]>([]);
  const [aiReasoning, setAiReasoning] = useState<string[]>([]);
  const [aiConfidence, setAiConfidence] = useState<number | null>(null);
  const [aiAnalysis, setAiAnalysis] = useState<AITriageAnalysis | null>(null);

  function resetForm() {
    setPatientName("");
    setAge("");
    setGender("");
    setEmergencyType("");
    setSymptoms("");
    setConsciousness("");
    setBreathing("");
    setSeverity("");
    setPatientCount("1");
    setAddress("");
    setLandmark("");
    setNotes("");
    setErrors({});
    setFormError(null);
    setAiLoading(false);
    setAiError(null);
    setAiKeywords([]);
    setAiReasoning([]);
    setAiConfidence(null);
    setAiAnalysis(null);
  }

  function handleClose() {
    if (submitting) return;
    resetForm();
    onClose();
  }

  function validate(): Record<string, string> {
    const e: Record<string, string> = {};
    if (!emergencyType) e.emergencyType = "نوع الطارئة مطلوب";
    if (!symptoms.trim()) e.symptoms = "وصف الأعراض مطلوب";
    if (!severity) e.severity = "مستوى الخطورة مطلوب";
    if (!address.trim()) e.address = "الموقع مطلوب";

    const count = Number(patientCount);
    if (!Number.isInteger(count) || count < 1) {
      e.patientCount = "عدد المصابين يجب أن يكون 1 على الأقل";
    }
    if (age.trim()) {
      const ageNum = Number(age);
      if (!Number.isInteger(ageNum) || ageNum < 0 || ageNum > 130) {
        e.age = "العمر غير صالح";
      }
    }
    return e;
  }

  async function handleGenerateSuggestion() {
    const text = [emergencyType, symptoms].filter(Boolean).join(". ").trim();
    if (text.length < 3) {
      setAiError("اكتب وصف الأعراض أولاً للحصول على اقتراح");
      return;
    }
    setAiLoading(true);
    setAiError(null);
    try {
      const { analysis } = await inferenceApi.analyzeText(text);
      setAiAnalysis(analysis);
      // Suggested severity = LLM triage level.
      if (analysis.triage?.level) {
        setSeverity(analysis.triage.level);
      }
      setAiConfidence(
        typeof analysis.triage?.confidence === "number" ? analysis.triage.confidence : null,
      );
      setAiReasoning(Array.isArray(analysis.triage?.reasoning) ? analysis.triage.reasoning : []);
      // Critical keywords from grounded highlights.
      setAiKeywords(getKeywordLabels(analysis).slice(0, 12));
      // Suggested emergency type from mechanism of injury, if empty.
      const mechanism = analysis.medical_entities?.mechanism_of_injury ?? [];
      if (!emergencyType && mechanism.length > 0) {
        setEmergencyType("أخرى");
      }
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "تعذّر توليد اقتراح الذكاء الاصطناعي");
    } finally {
      setAiLoading(false);
    }
  }

  async function handleSubmit(includeHospital: boolean) {
    if (submitting) return;
    const validationErrors = validate();
    setErrors(validationErrors);
    if (Object.keys(validationErrors).length > 0) {
      setFormError("يرجى تعبئة الحقول المطلوبة");
      return;
    }
    setFormError(null);
    setSubmittingMode(includeHospital ? "both" : "ambulance");

    const count = Number(patientCount);
    const ageNum = age.trim() ? Number(age) : undefined;

    // Compose a readable chief_complaint so the medic/hospital portals
    // render the manual case exactly like an AI case.
    const clinicalBits = [
      emergencyType,
      symptoms.trim(),
      consciousness ? `الوعي: ${consciousness}` : "",
      breathing ? `التنفس: ${breathing}` : "",
    ].filter(Boolean);
    const chiefComplaint = clinicalBits.join(" | ");

    const rawLocation = [address.trim(), landmark.trim() ? `(${landmark.trim()})` : ""]
      .filter(Boolean)
      .join(" ");

    const create: CaseCreate = {
      source: "manual",
      patient_name: patientName.trim() || undefined,
      patient_age: ageNum,
      patient_gender: gender || undefined,
      patient_count: count,
      chief_complaint: chiefComplaint || undefined,
      triage_priority: severity || undefined,
      notes: notes.trim() || undefined,
      patient_location: address.trim()
        ? {
            raw_text: rawLocation,
            source_span: null,
            components: { landmark: landmark.trim() || null },
            geocode: null,
            confidence: 0,
            needs_confirmation: true,
          }
        : null,
      manual_details: {
        emergency_type: emergencyType,
        symptoms: symptoms.trim(),
        consciousness: consciousness || null,
        breathing: breathing || null,
        severity,
        ai_confidence: aiConfidence,
      },
    };

    const summarySymptoms = aiKeywords.length > 0 ? aiKeywords : [emergencyType].filter(Boolean);

    try {
      await onSubmit(
        {
          create,
          triage: severity as TriagePriorityValue,
          confidence: aiConfidence,
          summary: {
            location: rawLocation,
            patients: count,
            symptoms: summarySymptoms,
            notes: notes.trim(),
          },
          analysis: aiAnalysis,
        },
        includeHospital,
      );
      resetForm();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "فشل إرسال الحالة");
    } finally {
      setSubmittingMode(null);
    }
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 backdrop-blur-sm p-4 sm:p-6"
          onMouseDown={handleClose}
          dir="rtl"
        >
          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.98 }}
            transition={{ type: "spring", damping: 26, stiffness: 280 }}
            className="relative w-full max-w-2xl bg-[#faf9f6] rounded-2xl shadow-2xl my-4"
            onMouseDown={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="sticky top-0 z-10 flex items-center justify-between px-6 py-4 bg-white rounded-t-2xl border-b border-gray-100">
              <div className="flex items-center gap-2.5">
                <div
                  className="w-9 h-9 rounded-xl flex items-center justify-center text-white"
                  style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
                >
                  <ClipboardList className="w-4 h-4" />
                </div>
                <div>
                  <div className="text-base font-bold text-gray-800 leading-tight">إدخال حالة يدوية</div>
                  <div className="text-[11px] text-gray-400 leading-tight">Manual Case Entry</div>
                </div>
              </div>
              <button
                onClick={handleClose}
                disabled={submitting}
                className="p-2 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition disabled:opacity-50"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-5 space-y-5">
              {/* Patient information */}
              <section>
                <SectionTitle icon={<User className="w-4 h-4" />}>معلومات المريض</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <Label>اسم المريض</Label>
                    <input
                      className={inputClass}
                      value={patientName}
                      onChange={(e) => setPatientName(e.target.value)}
                      placeholder="اختياري"
                    />
                  </div>
                  <div>
                    <Label>العمر</Label>
                    <input
                      className={inputClass}
                      value={age}
                      onChange={(e) => setAge(e.target.value)}
                      inputMode="numeric"
                      placeholder="مثال: 45"
                    />
                    {errors.age && <p className="text-[11px] text-red-500 mt-1">{errors.age}</p>}
                  </div>
                  <div>
                    <Label>الجنس</Label>
                    <select className={inputClass} value={gender} onChange={(e) => setGender(e.target.value)}>
                      {GENDER_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </section>

              {/* Emergency information */}
              <section>
                <SectionTitle icon={<Activity className="w-4 h-4" />}>معلومات الطوارئ</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <Label required>نوع الطارئة</Label>
                    <select
                      className={inputClass}
                      value={emergencyType}
                      onChange={(e) => setEmergencyType(e.target.value)}
                    >
                      {EMERGENCY_TYPES.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    {errors.emergencyType && (
                      <p className="text-[11px] text-red-500 mt-1">{errors.emergencyType}</p>
                    )}
                  </div>
                  <div>
                    <Label required>عدد المصابين</Label>
                    <input
                      className={inputClass}
                      value={patientCount}
                      onChange={(e) => setPatientCount(e.target.value)}
                      inputMode="numeric"
                    />
                    {errors.patientCount && (
                      <p className="text-[11px] text-red-500 mt-1">{errors.patientCount}</p>
                    )}
                  </div>
                </div>

                <div className="mt-3">
                  <Label required>الأعراض / الوصف</Label>
                  <textarea
                    className={`${inputClass} min-h-[80px] resize-y`}
                    value={symptoms}
                    onChange={(e) => setSymptoms(e.target.value)}
                    placeholder="صف حالة المريض والأعراض كما وردت..."
                  />
                  {errors.symptoms && <p className="text-[11px] text-red-500 mt-1">{errors.symptoms}</p>}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-3">
                  <div>
                    <Label>الوعي</Label>
                    <select
                      className={inputClass}
                      value={consciousness}
                      onChange={(e) => setConsciousness(e.target.value)}
                    >
                      {CONSCIOUSNESS_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <Label>التنفس</Label>
                    <select className={inputClass} value={breathing} onChange={(e) => setBreathing(e.target.value)}>
                      {BREATHING_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <Label required>مستوى الخطورة</Label>
                    <select
                      className={inputClass}
                      value={severity}
                      onChange={(e) => setSeverity(e.target.value as TriagePriorityValue | "")}
                    >
                      <option value="">اختر المستوى</option>
                      {SEVERITY_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    {errors.severity && <p className="text-[11px] text-red-500 mt-1">{errors.severity}</p>}
                  </div>
                </div>

                {/* AI Suggestion */}
                <div className="mt-4 rounded-xl border border-[#006C35]/20 bg-[#006C35]/[0.04] p-3.5">
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-2 text-sm font-semibold text-[#006C35]">
                      <Sparkles className="w-4 h-4" />
                      اقتراح الذكاء الاصطناعي
                    </div>
                    <button
                      type="button"
                      onClick={handleGenerateSuggestion}
                      disabled={aiLoading}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white transition hover:opacity-90 active:scale-95 disabled:opacity-60"
                      style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
                    >
                      {aiLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                      {aiLoading ? "جارٍ التحليل..." : "توليد اقتراح"}
                    </button>
                  </div>
                  {aiError && <p className="text-[11px] text-red-500 mt-2">{aiError}</p>}
                  {(aiKeywords.length > 0 || aiReasoning.length > 0) && (
                    <div className="mt-3 space-y-2">
                      {aiConfidence !== null && (
                        <div className="text-[11px] text-gray-500">
                          مستوى الثقة: <span className="font-semibold">{Math.round(aiConfidence * 100)}%</span>
                        </div>
                      )}
                      {aiKeywords.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {aiKeywords.map((k, i) => (
                            <span
                              key={i}
                              className="px-2 py-0.5 bg-red-50 text-red-700 border border-red-200 rounded-full text-[11px] font-medium flex items-center gap-1"
                            >
                              <AlertTriangle className="w-3 h-3" />
                              {k}
                            </span>
                          ))}
                        </div>
                      )}
                      {aiReasoning.length > 0 && (
                        <ul className="list-disc pr-4 space-y-0.5">
                          {aiReasoning.slice(0, 4).map((r, i) => (
                            <li key={i} className="text-[12px] text-gray-600">
                              {r}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
              </section>

              {/* Location information */}
              <section>
                <SectionTitle icon={<MapPin className="w-4 h-4" />}>معلومات الموقع</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <Label required>العنوان / الموقع</Label>
                    <input
                      className={inputClass}
                      value={address}
                      onChange={(e) => setAddress(e.target.value)}
                      placeholder="الحي، الشارع، المدينة"
                    />
                    {errors.address && <p className="text-[11px] text-red-500 mt-1">{errors.address}</p>}
                  </div>
                  <div>
                    <Label>معلم قريب</Label>
                    <input
                      className={inputClass}
                      value={landmark}
                      onChange={(e) => setLandmark(e.target.value)}
                      placeholder="اختياري"
                    />
                  </div>
                </div>
              </section>

              {/* Dispatcher notes */}
              <section>
                <SectionTitle icon={<FileText className="w-4 h-4" />}>ملاحظات المشغّل</SectionTitle>
                <textarea
                  className={`${inputClass} min-h-[64px] resize-y`}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="ملاحظات إضافية..."
                />
              </section>

              {formError && (
                <div className="rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm px-3 py-2 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" />
                  {formError}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="sticky bottom-0 flex flex-col sm:flex-row items-stretch sm:items-center sm:justify-end gap-2.5 px-6 py-4 bg-white rounded-b-2xl border-t border-gray-100">
              <button
                onClick={handleClose}
                disabled={submitting}
                className="px-4 py-2.5 rounded-xl text-sm font-semibold text-gray-600 border border-gray-200 hover:bg-gray-50 transition disabled:opacity-50"
              >
                إلغاء
              </button>
              <button
                onClick={() => handleSubmit(false)}
                disabled={submitting}
                className="flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 transition active:scale-95 disabled:opacity-60"
              >
                {submittingMode === "ambulance" ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Ambulance className="w-4 h-4" />
                )}
                {submittingMode === "ambulance" ? "جارٍ الإرسال..." : "إرسال إلى الإسعاف"}
              </button>
              <button
                onClick={() => handleSubmit(true)}
                disabled={submitting}
                className="flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white transition hover:opacity-90 active:scale-95 disabled:opacity-60"
                style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
              >
                {submittingMode === "both" ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Hospital className="w-4 h-4" />
                )}
                {submittingMode === "both" ? "جارٍ الإرسال..." : "إرسال إلى الإسعاف والمستشفى"}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
