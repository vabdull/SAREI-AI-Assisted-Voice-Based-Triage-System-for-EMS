// Hospital Portal: shows cases assigned to the receiving hospital so
// staff can review the incoming patient's triage, transcript, and
// details ahead of arrival.
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Activity, LogOut, Bell, MapPin, User, RefreshCw,
  Building2, FileText, ChevronLeft, CheckCircle, AlertTriangle,
  Ambulance, Clock,
} from "lucide-react";
import { authApi, hospitalApi } from "../../services/api";
import type { CaseRead, UserRead } from "../../types/api";
import PortalSwitcher from "../../components/PortalSwitcher";
import { formatCaseDateTime } from "../../utils/datetime";

function TriageBadge({ level, size = "sm" }: { level: string | null; size?: "sm" | "lg" }) {
  if (!level) return <span className="px-2 py-0.5 bg-gray-100 text-gray-500 rounded-full text-xs font-semibold border border-gray-200">غير محدد</span>;
  const map: Record<string, { cls: string; label: string; glow?: string }> = {
    red:    { cls: "bg-red-50 text-red-700 border-red-200",         label: "🔴 حرجة",   glow: "#ef4444" },
    yellow: { cls: "bg-amber-50 text-amber-700 border-amber-200",   label: "🟡 متوسطة", glow: "#f59e0b" },
    green:  { cls: "bg-emerald-50 text-emerald-700 border-emerald-200", label: "🟢 بسيطة", glow: "#10b981" },
    black:  { cls: "bg-gray-800 text-white border-gray-800",         label: "⬛ وفاة" },
  };
  const cfg = map[level] ?? { cls: "bg-gray-100 text-gray-500 border-gray-200", label: level };
  if (size === "lg") {
    return (
      <div className={`rounded-2xl border-2 p-5 text-center ${cfg.cls}`}>
        <div className="text-2xl font-bold mb-1">{cfg.label}</div>
        <div className="text-xs opacity-70">مستوى الخطورة</div>
      </div>
    );
  }
  return <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${cfg.cls}`}>{cfg.label}</span>;
}

function Panel({ title, icon, children, className }: { title: string; icon: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-white rounded-2xl border border-[#e4e2db] shadow-sm overflow-hidden ${className ?? ""}`}>
      <div className="flex items-center gap-2.5 px-5 py-3.5 border-b border-[#f0ede6] bg-[#faf9f6]">
        <span className="text-[#006C35]">{icon}</span>
        <h2 className="text-sm font-semibold text-gray-700 tracking-wide uppercase">{title}</h2>
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

// Keys MUST match the backend ``CaseStatus`` enum values exactly
// (active, en_route, at_scene, transporting, at_hospital, closed).
const STATUS_COLORS: Record<string, string> = {
  active:       "bg-blue-50 text-blue-700 border-blue-200",
  en_route:     "bg-amber-50 text-amber-700 border-amber-200",
  at_scene:     "bg-orange-50 text-orange-700 border-orange-200",
  transporting: "bg-purple-50 text-purple-700 border-purple-200",
  at_hospital:  "bg-teal-50 text-teal-700 border-teal-200",
  closed:       "bg-gray-100 text-gray-500 border-gray-200",
};

const STATUS_AR: Record<string, string> = {
  active: "نشطة", en_route: "في الطريق", at_scene: "في الموقع",
  transporting: "جاري النقل", at_hospital: "في المستشفى", closed: "مغلقة",
};

export default function HospitalPortalPage() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserRead | null>(null);
  const [cases, setCases] = useState<CaseRead[]>([]);
  const [selected, setSelected] = useState<CaseRead | null>(null);
  const [completing, setCompleting] = useState(false);
  const [notif, setNotif] = useState<string | null>(null);

  const showNotif = (msg: string) => {
    setNotif(msg);
    setTimeout(() => setNotif(null), 3000);
  };

  // Polling refreshes both the list and the currently open detail view.
  // The second update matters because fields can arrive later (e.g.
  // patient_count and chief_complaint from the LLM, or a status change
  // from the medic); without it, the open card would keep showing stale
  // data from the moment it was selected.
  const loadCases = useCallback(async () => {
    try {
      const fresh = await hospitalApi.incoming();
      setCases(fresh);
      setSelected((current) => {
        if (!current) return current;
        const updated = fresh.find((c) => c.id === current.id);
        return updated ?? current;
      });
    } catch {
      /* ignore — periodic refresh is best-effort */
    }
  }, []);

  useEffect(() => {
    authApi.me().then(setUser).catch(() => navigate("/login", { replace: true }));
    loadCases();
    // Refresh every 8s so newly extracted fields (patient_count,
    // chief_complaint) and status changes appear promptly during a call.
    const timer = setInterval(loadCases, 8000);
    return () => clearInterval(timer);
  }, [loadCases, navigate]);

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    navigate("/login", { replace: true });
  }

  async function selectCase(c: CaseRead) {
    try { setSelected(await hospitalApi.getCase(c.id)); }
    catch { setSelected(c); }
  }

  // Finalise the case from the hospital side. Backend is idempotent
  // (a closed case stays closed; second click is a safe no-op) but we
  // block reentrancy locally to avoid the toast/spinner flicker.
  async function completeCase(caseId: number | string) {
    if (completing) return;
    setCompleting(true);
    try {
      const updated = await hospitalApi.completeCase(caseId);
      // Case is now ``closed`` — the /incoming filter no longer
      // returns it, so we drop it from the local list and clear the
      // selection. A full refetch keeps everything consistent.
      setCases((prev) => prev.filter((c) => c.id !== updated.id));
      setSelected(null);
      showNotif("تم إنهاء الحالة");
    } catch (error) {
      showNotif(error instanceof Error ? error.message : "تعذّر إنهاء الحالة");
    } finally {
      setCompleting(false);
    }
  }

  const criticalCount = cases.filter((c) => c.triage_priority === "red").length;
  const incomingCount = cases.filter((c) => ["en_route", "transporting"].includes(c.status)).length;

  return (
    <div className="min-h-screen bg-[#f5f4f0] flex flex-col" dir="rtl">

      {/* Nav */}
      <header className="bg-white border-b border-[#e4e2db] shadow-sm sticky top-0 z-50">
        <div className="max-w-[1400px] mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Sare'i EMS" className="w-20 h-20 rounded-xl object-contain bg-white" />
            <div>
              <div className="text-base font-bold text-[#006C35] leading-tight">سارع</div>
              <div className="text-[10px] text-gray-400 leading-tight tracking-wider uppercase">Hospital Portal</div>
            </div>
          </div>

          <div className="flex items-center gap-2 bg-purple-50 border border-purple-200 rounded-full px-4 py-1.5">
            <Building2 className="w-4 h-4 text-purple-600" />
            <span className="text-purple-700 text-sm font-semibold">بوابة المستشفى</span>
          </div>

          <div className="flex items-center gap-3">
            <PortalSwitcher user={user} />
            <span className="relative">
              <Bell className="w-5 h-5 text-gray-400 hover:text-[#006C35] cursor-pointer transition-colors" />
              {incomingCount > 0 && (
                <span className="absolute -top-1 -right-1 w-3.5 h-3.5 bg-red-500 rounded-full text-[9px] text-white flex items-center justify-center font-bold">{incomingCount}</span>
              )}
            </span>
            <button onClick={loadCases} className="p-2 rounded-lg hover:bg-[#e6f4ed] text-gray-400 hover:text-[#006C35] transition-colors">
              <RefreshCw className="w-4 h-4" />
            </button>
            <div className="h-5 w-px bg-gray-200" />
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-sm font-bold" style={{ background: "linear-gradient(135deg, #6d28d9, #8b5cf6)" }}>
                {user?.full_name?.[0]?.toUpperCase() ?? "H"}
              </div>
              <div className="text-right hidden sm:block">
                <div className="text-sm font-semibold text-gray-700 leading-tight">{user?.full_name ?? "..."}</div>
                <div className="text-[11px] text-gray-400 leading-tight">مستشفى</div>
              </div>
            </div>
            <button onClick={logout} className="p-2 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      {/* Stats bar */}
      <div className="bg-white border-b border-[#e4e2db]">
        <div className="max-w-[1400px] mx-auto px-6 py-3 flex items-center gap-4">
          <div className="flex items-center gap-2 px-3 py-1.5 bg-[#f5f4f0] rounded-xl border border-[#e4e2db]">
            <Activity className="w-4 h-4 text-[#006C35]" />
            <span className="text-sm font-semibold text-gray-700">{cases.length}</span>
            <span className="text-xs text-gray-400">إجمالي الحالات</span>
          </div>
          {criticalCount > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-red-50 rounded-xl border border-red-200">
              <AlertTriangle className="w-4 h-4 text-red-500" />
              <span className="text-sm font-semibold text-red-700">{criticalCount}</span>
              <span className="text-xs text-red-500">حرجة</span>
            </div>
          )}
          {incomingCount > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-50 rounded-xl border border-amber-200 animate-pulse">
              <Ambulance className="w-4 h-4 text-amber-500" />
              <span className="text-sm font-semibold text-amber-700">{incomingCount}</span>
              <span className="text-xs text-amber-500">في الطريق</span>
            </div>
          )}
          <div className="mr-auto flex items-center gap-1 text-xs text-gray-400">
            <Clock className="w-3.5 h-3.5" />
            يتجدد تلقائياً كل 8 ثوانٍ
          </div>
        </div>
      </div>

      {/* Toast */}
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

      {/* Body */}
      <main className="flex-1 max-w-[1400px] mx-auto w-full px-6 py-6">
        <div className="grid grid-cols-12 gap-5">

          {/* Cases list */}
          <div className="col-span-12 lg:col-span-4 xl:col-span-3">
            <Panel title="الحالات الواردة" icon={<Ambulance className="w-4 h-4" />}>
              {cases.length === 0 ? (
                <div className="text-center py-10">
                  <Building2 className="w-10 h-10 text-gray-200 mx-auto mb-3" />
                  <p className="text-sm text-gray-400">لا توجد حالات واردة حالياً</p>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {cases.map((c) => {
                    const isIncoming = ["en_route", "transporting"].includes(c.status);
                    return (
                      <motion.button
                        key={c.id}
                        onClick={() => selectCase(c)}
                        whileHover={{ scale: 1.01 }}
                        whileTap={{ scale: 0.99 }}
                        className={`w-full text-right p-3.5 rounded-xl border transition-all ${
                          selected?.id === c.id
                            ? "bg-[#e6f4ed] border-[#006C35] shadow-sm"
                            : isIncoming
                            ? "bg-amber-50 border-amber-200 hover:border-amber-400"
                            : "bg-[#f5f4f0] border-[#e4e2db] hover:border-[#006C35] hover:bg-[#f0f8f4]"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1.5">
                          <span className="text-sm font-bold text-gray-800">{c.incident_number}</span>
                          <TriageBadge level={c.triage_priority} />
                        </div>
                        <div className="text-xs text-gray-500 mb-1">{c.patient_name || "مريض غير معروف"}</div>
                        <div className="flex items-center gap-1 text-[11px] text-gray-400 mb-1.5">
                          <Clock className="w-3 h-3 flex-shrink-0" />
                          <span>{formatCaseDateTime(c.created_at)}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${STATUS_COLORS[c.status] ?? "bg-gray-100 text-gray-500 border-gray-200"}`}>
                            {STATUS_AR[c.status] ?? c.status}
                          </span>
                          {isIncoming && <span className="w-2 h-2 bg-amber-500 rounded-full animate-pulse" />}
                        </div>
                      </motion.button>
                    );
                  })}
                </div>
              )}
            </Panel>
          </div>

          {/* Detail */}
          <div className="col-span-12 lg:col-span-8 xl:col-span-9">
            <AnimatePresence mode="wait">
              {selected ? (
                <motion.div key={selected.id} initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0 }} className="flex flex-col gap-5">

                  {/* Header card */}
                  <div className="bg-white rounded-2xl border border-[#e4e2db] p-5 shadow-sm">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <div className="flex items-center gap-2 mb-1">
                          <h2 className="text-xl font-bold text-gray-800">{selected.incident_number}</h2>
                          <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${STATUS_COLORS[selected.status] ?? "bg-gray-100 text-gray-500 border-gray-200"}`}>
                            {STATUS_AR[selected.status] ?? selected.status}
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5 text-sm text-gray-500">
                          <MapPin className="w-3.5 h-3.5 text-[#006C35]" />
                          {selected.patient_location?.raw_text || "موقع غير محدد"}
                        </div>
                      </div>
                      <TriageBadge level={selected.triage_priority} size="lg" />
                    </div>

                    {/* Hospital finalises the case lifecycle. Disabled
                        once already closed (idempotent on the backend
                        either way). */}
                    {(() => {
                      const isClosed = selected.status === "closed";
                      const disabled = completing || isClosed;
                      const label = isClosed
                        ? "الحالة منتهية"
                        : completing
                          ? "جارٍ الإنهاء..."
                          : "إنهاء الحالة";
                      return (
                        <div className="mt-4 pt-4 border-t border-[#f0ede6] flex justify-end">
                          <button
                            onClick={() => completeCase(selected.id)}
                            disabled={disabled}
                            className={`flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border transition-all ${
                              isClosed
                                ? "bg-gray-100 text-gray-500 border-gray-200 cursor-default"
                                : completing
                                  ? "bg-gray-50 text-gray-400 border-gray-200 cursor-wait"
                                  : "text-white border-transparent hover:opacity-90 active:scale-95"
                            }`}
                            style={
                              !disabled
                                ? { background: "linear-gradient(135deg, #006C35, #00883f)" }
                                : {}
                            }
                          >
                            <CheckCircle className="w-4 h-4" />
                            {label}
                          </button>
                        </div>
                      );
                    })()}
                  </div>

                  {/* Patient details */}
                  <Panel title="معلومات المريض" icon={<User className="w-4 h-4" />}>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                      <DetailField label="اسم المريض" value={selected.patient_name} />
                      <DetailField label="العمر" value={selected.patient_age ? `${selected.patient_age} سنة` : null} />
                      <DetailField label="الجنس" value={selected.patient_gender === "male" ? "ذكر" : selected.patient_gender === "female" ? "أنثى" : selected.patient_gender} />
                      <DetailField label="الشكوى الرئيسية" value={selected.chief_complaint} />
                      <DetailField
                        label="عدد المصابين"
                        value={selected.patient_count != null ? String(selected.patient_count) : null}
                      />
                      <DetailField label="المشغّل" value={selected.dispatcher?.full_name} />
                    </div>
                    {selected.notes && (
                      <div className="mt-3 p-3 bg-amber-50 border border-amber-200 rounded-xl">
                        <div className="text-[11px] text-amber-600 font-semibold uppercase tracking-wide mb-1 flex items-center gap-1">
                          <AlertTriangle className="w-3 h-3" />ملاحظات
                        </div>
                        <p className="text-sm text-amber-800">{selected.notes}</p>
                      </div>
                    )}
                  </Panel>

                  {/* Transcript */}
                  {selected.transcript_segments.length > 0 && (
                    <Panel title="نسخ المكالمة الأصلية" icon={<FileText className="w-4 h-4" />}>
                      <div className="flex flex-col gap-3 max-h-52 overflow-y-auto">
                        {selected.transcript_segments.map((seg) => (
                          <div key={seg.id} className={`flex gap-3 ${seg.speaker !== "caller" ? "flex-row-reverse" : ""}`}>
                            <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${seg.speaker === "caller" ? "bg-blue-100 text-blue-700" : "text-white"}`}
                              style={seg.speaker !== "caller" ? { background: "linear-gradient(135deg, #006C35, #00883f)" } : {}}>
                              {seg.speaker === "caller" ? "م" : "ط"}
                            </div>
                            <div className={`max-w-[75%] rounded-xl px-3 py-2 ${seg.speaker === "caller" ? "bg-[#f0f8f4] border border-[#d4eddf]" : "bg-[#eff6ff] border border-[#bfdbfe]"}`}>
                              <p className="text-xs text-gray-500 mb-0.5">{seg.speaker === "caller" ? "المتصل" : "المشغّل"}</p>
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
                  <p className="text-gray-400 font-medium">اختر حالة لعرض التفاصيل</p>
                  <p className="text-xs text-gray-300 mt-1">يتجدد تلقائياً كل 8 ثوانٍ</p>
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
            <span>بوابة المستشفى</span>
          </div>
        </div>
      </footer>
    </div>
  );
}
