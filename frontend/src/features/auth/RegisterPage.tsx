// Registration page: collects account details (including the requested
// role) and creates a new user, then sends the user to the login page.
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { User, Mail, Lock, Shield, AlertCircle, CheckCircle } from "lucide-react";
import { authApi } from "../../services/api";

const ROLES = [
  { value: "dispatcher", label: "مشغّل طوارئ", en: "Dispatcher", color: "text-blue-600" },
  { value: "medic",      label: "مسعف / إسعاف", en: "Medic",       color: "text-orange-600" },
  { value: "hospital",   label: "مستشفى",        en: "Hospital",    color: "text-purple-600" },
  { value: "admin",      label: "مدير النظام",   en: "Admin",       color: "text-red-600" },
];

export default function RegisterPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: "", email: "", full_name: "", password: "", role: "dispatcher" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  function update(field: string, value: string) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await authApi.register(form);
      navigate("/login", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "فشل إنشاء الحساب");
    } finally {
      setLoading(false);
    }
  }

  const selectedRole = ROLES.find((r) => r.value === form.role);

  return (
    <div
      className="h-screen flex flex-col items-center justify-center p-4 overflow-y-auto"
      style={{ background: "linear-gradient(145deg, #f5f4f0 0%, #e8f4ed 50%, #f0ede6 100%)" }}
      dir="rtl"
    >
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="absolute rounded-full opacity-[0.04]"
            style={{ width: 180 + i * 130, height: 180 + i * 130, background: "#006C35", top: `${5 + i * 18}%`, right: `${-8 + i * 16}%` }} />
        ))}
      </div>

      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: "easeOut" }}
        className="relative w-full max-w-md flex flex-col items-center"
      >
        <img src="/logo.png" alt="Sare'i EMS" className="w-[28rem] max-h-[32vh] object-contain drop-shadow-[0_28px_56px_rgba(0,108,53,0.28)] mb-2" />

        <div className="w-full bg-white rounded-3xl shadow-2xl overflow-hidden" style={{ boxShadow: "0 32px 80px rgba(0,108,53,0.12), 0 8px 24px rgba(0,0,0,0.08)" }}>

          <div className="px-8 py-4">
            <div className="text-center mb-3">
              <h2 className="text-xl font-bold text-gray-800">إنشاء حساب جديد</h2>
              <p className="text-sm text-gray-400 mt-0.5">سجّل بياناتك للانضمام إلى الفريق</p>
            </div>

            {error && (
              <motion.div
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                className="flex items-center gap-2 bg-red-50 text-red-700 border border-red-200 rounded-xl px-4 py-3 mb-5 text-sm"
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0" />
                {error}
              </motion.div>
            )}

            <form onSubmit={handleSubmit} className="space-y-3">

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-1">الاسم الكامل</label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                  <input type="text" value={form.full_name} onChange={(e) => update("full_name", e.target.value)}
                    required placeholder="الاسم الكامل"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-right"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }} />
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-1">اسم المستخدم</label>
                <div className="relative">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm font-mono">@</span>
                  <input type="text" value={form.username} onChange={(e) => update("username", e.target.value)}
                    required placeholder="username"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-right"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }} />
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-1">البريد الإلكتروني</label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                  <input type="email" value={form.email} onChange={(e) => update("email", e.target.value)}
                    required placeholder="email@example.com"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-left"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }} />
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-1">كلمة المرور</label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                  <input type="password" value={form.password} onChange={(e) => update("password", e.target.value)}
                    required minLength={8} placeholder="8 أحرف على الأقل"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-right"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }} />
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-1">الدور الوظيفي</label>
                <div className="grid grid-cols-2 gap-2">
                  {ROLES.map((r) => (
                    <button
                      key={r.value}
                      type="button"
                      onClick={() => update("role", r.value)}
                      className={`flex items-center gap-2 px-3 py-2.5 rounded-xl border text-sm font-semibold transition-all text-right ${
                        form.role === r.value
                          ? "border-[#006C35] bg-[#e6f4ed] text-[#006C35]"
                          : "border-gray-200 bg-[#faf9f6] text-gray-600 hover:border-gray-300"
                      }`}
                    >
                      <Shield className={`w-3.5 h-3.5 flex-shrink-0 ${form.role === r.value ? "text-[#006C35]" : "text-gray-400"}`} />
                      <div>
                        <div className="text-xs leading-tight">{r.label}</div>
                        <div className="text-[10px] text-gray-400 leading-tight">{r.en}</div>
                      </div>
                      {form.role === r.value && <CheckCircle className="w-3.5 h-3.5 text-[#006C35] mr-auto" />}
                    </button>
                  ))}
                </div>
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl font-bold text-white text-sm transition-all active:scale-[0.98] disabled:opacity-60 disabled:cursor-not-allowed mt-1"
                style={{ background: loading ? "#6b7280" : "linear-gradient(135deg, #006C35, #00883f)" }}
              >
                {loading ? (
                  <span className="flex items-center gap-2">
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    جاري الإنشاء...
                  </span>
                ) : (
                  <>
                    <CheckCircle className="w-4 h-4" />
                    إنشاء الحساب كـ {selectedRole?.label}
                  </>
                )}
              </button>
            </form>

            <p className="text-center mt-3 text-sm text-gray-400">
              لديك حساب بالفعل؟{" "}
              <Link to="/login" className="text-[#006C35] font-semibold hover:underline">تسجيل الدخول</Link>
            </p>
          </div>
        </div>
        <p className="text-center mt-3 text-xs text-gray-400">منصة سارع للطوارئ الطبية الموحدة</p>
      </motion.div>
    </div>
  );
}
