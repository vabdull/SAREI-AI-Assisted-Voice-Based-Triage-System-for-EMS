// Modal letting the dispatcher manually edit a live call's details
// (location, patient demographics, symptoms, notes). These edits act as
// overrides that take precedence over the AI-extracted values.
import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Loader2, User, MapPin, FileText, Activity, Save, AlertTriangle } from "lucide-react";

export interface EditCallInfoValues {
  location: string;
  landmark: string;
  patients: number;
  symptoms: string[];
  patientName: string;
  age: number | null;
  gender: string;
  notes: string;
}

export interface EditCallInfoInitial {
  location: string;
  landmark: string;
  patients: number;
  symptoms: string[];
  patientName: string;
  age: string;
  gender: string;
  notes: string;
}

interface EditCallInfoModalProps {
  open: boolean;
  onClose: () => void;
  initial: EditCallInfoInitial;
  /** Persists the edits + updates the live display. Throws on failure. */
  onSave: (values: EditCallInfoValues) => Promise<void>;
}

const GENDER_OPTIONS = [
  { value: "", label: "غير محدد" },
  { value: "ذكر", label: "ذكر" },
  { value: "أنثى", label: "أنثى" },
  { value: "غير معروف", label: "غير معروف" },
];

const inputClass =
  "w-full rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-sm text-gray-800 " +
  "focus:border-[#006C35] focus:ring-2 focus:ring-[#006C35]/20 focus:outline-none transition";

function Label({ children }: { children: React.ReactNode }) {
  return <label className="block text-[12px] font-semibold text-gray-600 mb-1.5">{children}</label>;
}

function SectionTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[#006C35] font-bold text-sm mb-3 mt-1">
      {icon}
      {children}
    </div>
  );
}

export default function EditCallInfoModal({ open, onClose, initial, onSave }: EditCallInfoModalProps) {
  const [location, setLocation] = useState("");
  const [landmark, setLandmark] = useState("");
  const [patientCount, setPatientCount] = useState("1");
  const [symptomsText, setSymptomsText] = useState("");
  const [patientName, setPatientName] = useState("");
  const [age, setAge] = useState("");
  const [gender, setGender] = useState("");
  const [notes, setNotes] = useState("");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form from the live values every time the modal opens.
  useEffect(() => {
    if (!open) return;
    setLocation(initial.location ?? "");
    setLandmark(initial.landmark ?? "");
    setPatientCount(String(initial.patients ?? 1));
    setSymptomsText((initial.symptoms ?? []).join("\n"));
    setPatientName(initial.patientName ?? "");
    setAge(initial.age ?? "");
    setGender(initial.gender ?? "");
    setNotes(initial.notes ?? "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function handleClose() {
    if (saving) return;
    onClose();
  }

  async function handleSave() {
    if (saving) return;
    const count = Number(patientCount);
    if (!Number.isInteger(count) || count < 1) {
      setError("عدد المصابين يجب أن يكون 1 على الأقل");
      return;
    }
    let ageNum: number | null = null;
    if (age.trim()) {
      const n = Number(age);
      if (!Number.isInteger(n) || n < 0 || n > 130) {
        setError("العمر غير صالح");
        return;
      }
      ageNum = n;
    }
    setError(null);
    setSaving(true);

    const symptoms = symptomsText
      .split(/\r?\n|،|,/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    try {
      await onSave({
        location: location.trim(),
        landmark: landmark.trim(),
        patients: count,
        symptoms,
        patientName: patientName.trim(),
        age: ageNum,
        gender,
        notes: notes.trim(),
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "فشل حفظ التعديلات");
    } finally {
      setSaving(false);
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
            className="relative w-full max-w-xl bg-[#faf9f6] rounded-2xl shadow-2xl my-4"
            onMouseDown={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="sticky top-0 z-10 flex items-center justify-between px-6 py-4 bg-white rounded-t-2xl border-b border-gray-100">
              <div className="flex items-center gap-2.5">
                <div
                  className="w-9 h-9 rounded-xl flex items-center justify-center text-white"
                  style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
                >
                  <FileText className="w-4 h-4" />
                </div>
                <div>
                  <div className="text-base font-bold text-gray-800 leading-tight">تعديل معلومات الحالة</div>
                  <div className="text-[11px] text-gray-400 leading-tight">Edit Call Info</div>
                </div>
              </div>
              <button
                onClick={handleClose}
                disabled={saving}
                className="p-2 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition disabled:opacity-50"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="px-6 py-5 space-y-5">
              {/* Location */}
              <section>
                <SectionTitle icon={<MapPin className="w-4 h-4" />}>الموقع</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <Label>العنوان / الموقع</Label>
                    <input className={inputClass} value={location} onChange={(e) => setLocation(e.target.value)} />
                  </div>
                  <div>
                    <Label>معلم قريب</Label>
                    <input className={inputClass} value={landmark} onChange={(e) => setLandmark(e.target.value)} />
                  </div>
                </div>
              </section>

              {/* Emergency */}
              <section>
                <SectionTitle icon={<Activity className="w-4 h-4" />}>تفاصيل الحالة</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <Label>عدد المصابين</Label>
                    <input
                      className={inputClass}
                      value={patientCount}
                      onChange={(e) => setPatientCount(e.target.value)}
                      inputMode="numeric"
                    />
                  </div>
                </div>
                <div className="mt-3">
                  <Label>الأعراض (سطر لكل عرض)</Label>
                  <textarea
                    className={`${inputClass} min-h-[80px] resize-y`}
                    value={symptomsText}
                    onChange={(e) => setSymptomsText(e.target.value)}
                    placeholder={"نزيف\nصعوبة في التنفس"}
                  />
                </div>
              </section>

              {/* Patient */}
              <section>
                <SectionTitle icon={<User className="w-4 h-4" />}>معلومات المريض</SectionTitle>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <Label>الاسم</Label>
                    <input className={inputClass} value={patientName} onChange={(e) => setPatientName(e.target.value)} />
                  </div>
                  <div>
                    <Label>العمر</Label>
                    <input className={inputClass} value={age} onChange={(e) => setAge(e.target.value)} inputMode="numeric" />
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

              {/* Notes */}
              <section>
                <SectionTitle icon={<FileText className="w-4 h-4" />}>ملاحظات</SectionTitle>
                <textarea
                  className={`${inputClass} min-h-[64px] resize-y`}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </section>

              {error && (
                <div className="rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm px-3 py-2 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" />
                  {error}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="sticky bottom-0 flex items-center justify-end gap-3 px-6 py-4 bg-white rounded-b-2xl border-t border-gray-100">
              <button
                onClick={handleClose}
                disabled={saving}
                className="px-4 py-2.5 rounded-xl text-sm font-semibold text-gray-600 border border-gray-200 hover:bg-gray-50 transition disabled:opacity-50"
              >
                إلغاء
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white transition hover:opacity-90 active:scale-95 disabled:opacity-60"
                style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                {saving ? "جارٍ الحفظ..." : "حفظ التعديلات"}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
