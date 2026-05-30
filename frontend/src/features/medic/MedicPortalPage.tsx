// Medic / Ambulance Portal: shows cases dispatched to the on-duty medic
// and lets them advance the case through its status flow (en route, at
// scene, transporting, at hospital) and review patient/triage details.
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Activity, LogOut, Bell, MapPin, User, Clock,
  Ambulance, CheckCircle, AlertTriangle, FileText,
  ChevronLeft, RefreshCw, Navigation,
} from "lucide-react";
import { authApi, ambulanceApi } from "../../services/api";
import type { CaseRead, UserRead } from "../../types/api";
import PortalSwitcher from "../../components/PortalSwitcher";
import { formatCaseDateTime } from "../../utils/datetime";

// MUST match the backend ``CaseStatus`` enum values exactly. The
// progression buttons stop at ``at_hospital`` — closing the case is
// done by the explicit "إنهاء الحالة" button (POST /complete) so
// closure is a deliberate action, never a side-effect of a status
// click.
type AmbStatus = "en_route" | "at_scene" | "transporting" | "at_hospital";

const STATUS_FLOW: { value: AmbStatus; label: string; labelAr: string; color: string; bg: string; icon: React.ReactNode }[] = [
  { value: "en_route",     label: "En Route",      labelAr: "في الطريق",    color: "text-amber-700",  bg: "bg-amber-50 border-amber-200",   icon: <Navigation className="w-3.5 h-3.5" /> },
  { value: "at_scene",     label: "At Scene",      labelAr: "في الموقع",    color: "text-orange-700", bg: "bg-orange-50 border-orange-200", icon: <MapPin className="w-3.5 h-3.5" /> },
  { value: "transporting", label: "Transporting",  labelAr: "جاري النقل",   color: "text-purple-700", bg: "bg-purple-50 border-purple-200", icon: <Activity className="w-3.5 h-3.5" /> },
  { value: "at_hospital",  label: "At Hospital",   labelAr: "في المستشفى",  color: "text-teal-700",   bg: "bg-teal-50 border-teal-200",     icon: <CheckCircle className="w-3.5 h-3.5" /> },
];

function TriageBadge({ level }: { level: string | null }) {
  if (!level) return <span className="px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full text-xs font-semibold border border-gray-200">غير محدد</span>;
  const map: Record<string, string> = {
    red:    "bg-red-50 text-red-700 border-red-200",
    yellow: "bg-amber-50 text-amber-700 border-amber-200",
    green:  "bg-emerald-50 text-emerald-700 border-emerald-200",
    black:  "bg-gray-800 text-white border-gray-800",
  };
  const labels: Record<string, string> = { red: "🔴 حرجة", yellow: "🟡 متوسطة", green: "🟢 بسيطة", black: "⬛ وفاة" };
  return <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${map[level] ?? "bg-gray-100 text-gray-500 border-gray-200"}`}>{labels[level] ?? level}</span>;
}

// Reusable card with a titled header. ``badge`` (optional) renders an
// element on the opposite side of the header, e.g. a triage badge.
function Panel({ title, icon, children, className, badge }: { title: string; icon: React.ReactNode; children: React.ReactNode; className?: string; badge?: React.ReactNode }) {
  return (
    <div className={`bg-white rounded-2xl border border-[#e4e2db] shadow-sm overflow-hidden ${className ?? ""}`}>
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

function DetailField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="p-3 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
      <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide mb-1">{label}</div>
      <div className="text-sm font-medium text-gray-800">{value || "—"}</div>
    </div>
  );
}


export default function MedicPortalPage() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserRead | null>(null);
  const [cases, setCases] = useState<CaseRead[]>([]);
  const [selected, setSelected] = useState<CaseRead | null>(null);
  const [updating, setUpdating] = useState(false);
  const [notif, setNotif] = useState<string | null>(null);

  const showNotif = (msg: string) => { setNotif(msg); setTimeout(() => setNotif(null), 3000); };

  // Refresh both the list and the currently-open detail view so that
  // late-arriving fields (patient_count and chief_complaint from the LLM,
  // or status changes from the dispatcher) update the open card too,
  // not just the sidebar list.
  const loadCases = useCallback(async () => {
    try {
      const fresh = await ambulanceApi.myCases();
      setCases(fresh);
      setSelected((current) => {
        if (!current) return current;
        const updated = fresh.find((c) => c.id === current.id);
        return updated ?? current;
      });
    } catch {
      /* ignore — best-effort polling */
    }
  }, []);

  useEffect(() => {
    authApi.me().then(setUser).catch(() => navigate("/login", { replace: true }));
    loadCases();
    // 8s instead of 30s — keeps the medic's view in sync with the
    // live NLP pipeline (patient_count, chief_complaint) without
    // hammering the backend.
    const timer = setInterval(loadCases, 8000);
    return () => clearInterval(timer);
  }, [loadCases, navigate]);

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    navigate("/login", { replace: true });
  }

  async function updateStatus(caseId: number | string, status: string) {
    setUpdating(true);
    try {
      const updated = await ambulanceApi.updateStatus(caseId, status);
      setCases((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
      setSelected(updated);
      const step = STATUS_FLOW.find((s) => s.value === status);
      showNotif(`تم تحديث الحالة: ${step?.labelAr ?? status}`);
    } catch { /* ignore */ }
    finally { setUpdating(false); }
  }

  // Forward the case to a hospital. Backend is idempotent: a repeat
  // click is a no-op so we don't need to over-disable the button, but
  // we do block reentrancy locally to avoid the toast/spinner flicker.
  async function sendToHospital(caseId: number | string) {
    setUpdating(true);
    try {
      const updated = await ambulanceApi.sendToHospital(caseId);
      setCases((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
      setSelected(updated);
      const hosp = updated.assigned_hospital?.full_name;
      showNotif(hosp ? `تم الإرسال إلى المستشفى — ${hosp}` : "تم الإرسال إلى المستشفى");
    } catch (error) {
      showNotif(error instanceof Error ? error.message : "تعذّر الإرسال إلى المستشفى");
    } finally {
      setUpdating(false);
    }
  }

  // Finalise the case from the ambulance side. Mirrors the hospital
  // "إنهاء الحالة" action — either party closing the case is final.
  // Idempotent on the backend, but we drop the closed case from the
  // local list to reflect the medic-side filter so the sidebar
  // matches what /my-cases would return on next refresh.
  async function completeCase(caseId: number | string) {
    setUpdating(true);
    try {
      const updated = await ambulanceApi.completeCase(caseId);
      setCases((prev) => prev.filter((c) => c.id !== updated.id));
      setSelected(null);
      showNotif("تم إنهاء الحالة");
    } catch (error) {
      showNotif(error instanceof Error ? error.message : "تعذّر إنهاء الحالة");
    } finally {
      setUpdating(false);
    }
  }

  const currentStepIdx = STATUS_FLOW.findIndex((s) => s.value === selected?.status);

  return (
    <div className="min-h-screen bg-[#f5f4f0] flex flex-col" dir="rtl">

      {/* Nav */}
      <header className="bg-white border-b border-[#e4e2db] shadow-sm sticky top-0 z-50">
        <div className="max-w-[1400px] mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Sare'i EMS" className="w-20 h-20 rounded-xl object-contain bg-white" />
            <div>
              <div className="text-base font-bold text-[#006C35] leading-tight">سارع</div>
              <div className="text-[10px] text-gray-400 leading-tight tracking-wider uppercase">Medic Portal</div>
            </div>
          </div>

          <div className="flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-full px-4 py-1.5">
            <Ambulance className="w-4 h-4 text-amber-600" />
            <span className="text-amber-700 text-sm font-semibold">بوابة المسعف</span>
          </div>

          <div className="flex items-center gap-3">
            <PortalSwitcher user={user} />
            <Bell className="w-5 h-5 text-gray-400 hover:text-[#006C35] cursor-pointer transition-colors" />
            <button onClick={loadCases} className="p-2 rounded-lg hover:bg-[#e6f4ed] text-gray-400 hover:text-[#006C35] transition-colors">
              <RefreshCw className="w-4 h-4" />
            </button>
            <div className="h-5 w-px bg-gray-200" />
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-sm font-bold" style={{ background: "linear-gradient(135deg, #b45309, #d97706)" }}>
                {user?.full_name?.[0]?.toUpperCase() ?? "M"}
              </div>
              <div className="text-right hidden sm:block">
                <div className="text-sm font-semibold text-gray-700 leading-tight">{user?.full_name ?? "..."}</div>
                <div className="text-[11px] text-gray-400 leading-tight">مسعف</div>
              </div>
            </div>
            <button onClick={logout} className="p-2 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      {/* Toast */}
      <AnimatePresence>
        {notif && (
          <motion.div initial={{ opacity: 0, y: -20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -20 }}
            className="fixed top-20 left-1/2 -translate-x-1/2 z-50 bg-[#006C35] text-white px-6 py-2.5 rounded-full shadow-xl text-sm font-medium flex items-center gap-2">
            <CheckCircle className="w-4 h-4" />{notif}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Body */}
      <main className="flex-1 max-w-[1400px] mx-auto w-full px-6 py-6">
        <div className="grid grid-cols-12 gap-5">

          {/* Sidebar */}
          <div className="col-span-12 lg:col-span-4 xl:col-span-3">
            <Panel title="حالاتي المُسنَدة" icon={<Ambulance className="w-4 h-4" />}>
              {cases.length === 0 ? (
                <div className="text-center py-10">
                  <Ambulance className="w-10 h-10 text-gray-200 mx-auto mb-3" />
                  <p className="text-sm text-gray-400">لا توجد حالات مُسنَدة</p>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {cases.map((c) => (
                    <motion.button
                      key={c.id}
                      onClick={() => setSelected(c)}
                      whileHover={{ scale: 1.01 }}
                      whileTap={{ scale: 0.99 }}
                      className={`w-full text-right p-3.5 rounded-xl border transition-all ${
                        selected?.id === c.id
                          ? "bg-[#e6f4ed] border-[#006C35] shadow-sm"
                          : "bg-[#f5f4f0] border-[#e4e2db] hover:border-[#006C35] hover:bg-[#f0f8f4]"
                      }`}
                    >
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-sm font-bold text-gray-800">{c.incident_number}</span>
                        <TriageBadge level={c.triage_priority} />
                      </div>
                      <div className="text-xs text-gray-500 mb-1">{c.patient_name || "مريض غير معروف"}</div>
                      <div className="flex items-center gap-1 text-[11px] text-gray-400 mb-0.5">
                        <MapPin className="w-3 h-3 flex-shrink-0" />
                        <span className="truncate">{c.patient_location?.raw_text || "موقع غير محدد"}</span>
                      </div>
                      <div className="flex items-center gap-1 text-[11px] text-gray-400">
                        <Clock className="w-3 h-3 flex-shrink-0" />
                        <span>{formatCaseDateTime(c.created_at)}</span>
                      </div>
                    </motion.button>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          {/* Main */}
          <div className="col-span-12 lg:col-span-8 xl:col-span-9">
            <AnimatePresence mode="wait">
              {selected ? (
                <motion.div key={selected.id} initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} className="flex flex-col gap-5">

                  {/* Status progress bar */}
                  <Panel title="تحديث حالة المهمة" icon={<Navigation className="w-4 h-4" />}>
                    {/* Timeline */}
                    <div className="flex items-center gap-0 mb-4">
                      {STATUS_FLOW.map((step, i) => {
                        const done = i <= currentStepIdx;
                        const active = i === currentStepIdx;
                        return (
                          <div key={step.value} className="flex items-center flex-1">
                            <div className={`flex flex-col items-center ${i < STATUS_FLOW.length - 1 ? "flex-1" : ""}`}>
                              <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2 transition-all ${
                                active ? "border-[#006C35] bg-[#006C35] text-white shadow-lg shadow-green-200" :
                                done  ? "border-[#006C35] bg-[#e6f4ed] text-[#006C35]" :
                                        "border-gray-200 bg-white text-gray-400"
                              }`}>
                                {done ? <CheckCircle className="w-4 h-4" /> : <span>{i + 1}</span>}
                              </div>
                              <span className="text-[9px] text-gray-400 mt-1 text-center leading-tight max-w-[50px]">{step.labelAr}</span>
                            </div>
                            {i < STATUS_FLOW.length - 1 && (
                              <div className={`h-0.5 flex-1 mb-4 mx-1 transition-all ${i < currentStepIdx ? "bg-[#006C35]" : "bg-gray-200"}`} />
                            )}
                          </div>
                        );
                      })}
                    </div>

                    {/* Action buttons */}
                    <div className="flex flex-wrap gap-2">
                      {STATUS_FLOW.map((step) => {
                        const isCurrent = selected.status === step.value;
                        const isNext = STATUS_FLOW.indexOf(step) === currentStepIdx + 1;
                        return (
                          <button
                            key={step.value}
                            onClick={() => updateStatus(selected.id, step.value)}
                            disabled={updating || isCurrent || (!isNext && !isCurrent)}
                            className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold border transition-all ${
                              isCurrent ? `${step.bg} ${step.color} ring-2 ring-offset-1 ring-current cursor-default` :
                              isNext ? `${step.bg} ${step.color} hover:opacity-80 active:scale-95` :
                              "bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed opacity-60"
                            }`}
                          >
                            {step.icon}{step.labelAr}
                            {isCurrent && <CheckCircle className="w-3 h-3" />}
                          </button>
                        );
                      })}
                    </div>

                    {/* Bottom action row: forward-to-hospital + close-case.
                        Both are idempotent on the backend; the UI just
                        hints at what is meaningful right now. */}
                    {(() => {
                      const alreadySent = Boolean(selected.assigned_hospital);
                      const isClosed = selected.status === "closed";

                      const sendDisabled = updating || alreadySent || isClosed;
                      const sendLabel = alreadySent
                        ? `تم الإرسال — ${selected.assigned_hospital?.full_name ?? ""}`
                        : isClosed
                          ? "الحالة منتهية"
                          : "إرسال إلى المستشفى";

                      const completeDisabled = updating || isClosed;
                      const completeLabel = isClosed ? "الحالة منتهية" : "إنهاء الحالة";

                      return (
                        <div className="mt-4 pt-4 border-t border-[#f0ede6] grid grid-cols-1 md:grid-cols-2 gap-2">
                          <button
                            onClick={() => sendToHospital(selected.id)}
                            disabled={sendDisabled}
                            className={`w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold border transition-all ${
                              sendDisabled
                                ? alreadySent
                                  ? "bg-teal-50 text-teal-700 border-teal-200 cursor-default"
                                  : "bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed"
                                : "bg-purple-50 text-purple-700 border-purple-200 hover:bg-purple-100 active:scale-95"
                            }`}
                          >
                            {alreadySent ? <CheckCircle className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4 rotate-180" />}
                            <span className="truncate">{sendLabel}</span>
                          </button>

                          <button
                            onClick={() => completeCase(selected.id)}
                            disabled={completeDisabled}
                            className={`w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold border transition-all ${
                              completeDisabled
                                ? "bg-gray-50 text-gray-400 border-gray-200 cursor-not-allowed"
                                : "bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-100 active:scale-95"
                            }`}
                          >
                            <CheckCircle className="w-4 h-4" />
                            <span className="truncate">{completeLabel}</span>
                          </button>
                        </div>
                      );
                    })()}
                  </Panel>

                  {/* Patient details */}
                  <Panel title="تفاصيل المريض والحادث" icon={<User className="w-4 h-4" />}
                    badge={<TriageBadge level={selected.triage_priority} />}>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-4">
                      <DetailField label="اسم المريض" value={selected.patient_name} />
                      <DetailField label="العمر" value={selected.patient_age ? `${selected.patient_age} سنة` : null} />
                      <DetailField label="الجنس" value={selected.patient_gender === "male" ? "ذكر" : selected.patient_gender === "female" ? "أنثى" : selected.patient_gender} />
                      <DetailField label="الشكوى الرئيسية" value={selected.chief_complaint} />
                      <DetailField
                        label="عدد المصابين"
                        value={selected.patient_count != null ? String(selected.patient_count) : null}
                      />
                      <DetailField label="الموقع" value={selected.patient_location?.raw_text ?? null} />
                      <DetailField label="المُرسِل" value={selected.dispatcher?.full_name} />
                    </div>
                    {selected.notes && (
                      <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl">
                        <div className="text-[11px] text-amber-600 font-semibold uppercase tracking-wide mb-1 flex items-center gap-1">
                          <AlertTriangle className="w-3 h-3" />ملاحظات مهمة
                        </div>
                        <p className="text-sm text-amber-800">{selected.notes}</p>
                      </div>
                    )}
                  </Panel>

                  {/* Transcript */}
                  {selected.transcript_segments.length > 0 && (
                    <Panel title="نسخ المكالمة" icon={<FileText className="w-4 h-4" />}>
                      <div className="flex flex-col gap-3 max-h-64 overflow-y-auto">
                        {selected.transcript_segments.map((seg) => (
                          <div key={seg.id} className={`flex gap-3 ${seg.speaker !== "caller" ? "flex-row-reverse" : ""}`}>
                            <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                              seg.speaker === "caller" ? "bg-blue-100 text-blue-700" : "text-white"
                            }`} style={seg.speaker !== "caller" ? { background: "linear-gradient(135deg, #006C35, #00883f)" } : {}}>
                              {seg.speaker === "caller" ? "م" : "ط"}
                            </div>
                            <div className={`max-w-[75%] rounded-2xl px-4 py-2.5 ${
                              seg.speaker === "caller" ? "bg-[#f0f8f4] border border-[#d4eddf] rounded-tr-sm" : "bg-[#eff6ff] border border-[#bfdbfe] rounded-tl-sm"
                            }`}>
                              <div className="text-[11px] font-semibold text-gray-400 mb-1">
                                {seg.speaker === "caller" ? "المتصل" : "المشغّل"}
                              </div>
                              <p className="text-sm text-gray-800" dir="rtl">{seg.text}</p>
                            </div>
                          </div>
                        ))}
                      </div>
                    </Panel>
                  )}
                </motion.div>
              ) : (
                <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  className="flex flex-col items-center justify-center h-64 bg-white rounded-2xl border border-[#e4e2db]">
                  <ChevronLeft className="w-12 h-12 text-gray-200 mb-3" />
                  <p className="text-gray-400 font-medium">اختر حالة من القائمة لعرض التفاصيل</p>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="bg-white border-t border-[#e4e2db] py-3 px-6">
        <div className="max-w-[1400px] mx-auto flex items-center justify-between">
          <div className="text-[11px] text-gray-400">منصة سارع للطوارئ الطبية الموحدة</div>
          <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
            بوابة المسعف
          </div>
        </div>
      </footer>
    </div>
  );
}
