// Login page: authenticates the user, stores the JWT and user object,
// then redirects to the portal that matches the user's role.
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { Lock, User, LogIn, AlertCircle } from "lucide-react";
import { authApi } from "../../services/api";

const ROLE_ROUTES: Record<string, string> = {
  dispatcher: "/dispatcher",
  medic: "/medic",
  hospital: "/hospital",
  admin: "/admin",
};

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { access_token } = await authApi.login({ username, password });
      localStorage.setItem("token", access_token);
      const user = await authApi.me();
      localStorage.setItem("user", JSON.stringify(user));
      const dest = ROLE_ROUTES[user.role] ?? "/login";
      navigate(dest, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "فشل تسجيل الدخول");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="h-screen flex flex-col items-center justify-center p-4 overflow-y-auto"
      style={{ background: "linear-gradient(145deg, #f5f4f0 0%, #e8f4ed 50%, #f0ede6 100%)" }}
      dir="rtl"
    >
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        {[...Array(6)].map((_, i) => (
          <div
            key={i}
            className="absolute rounded-full opacity-[0.04]"
            style={{
              width: 200 + i * 120,
              height: 200 + i * 120,
              background: "#006C35",
              top: `${10 + i * 15}%`,
              left: `${-10 + i * 18}%`,
            }}
          />
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

          <div className="px-8 py-6">
            <div className="text-center mb-5">
              <h2 className="text-xl font-bold text-gray-800">تسجيل الدخول</h2>
              <p className="text-sm text-gray-400 mt-0.5">أدخل بياناتك للوصول إلى النظام</p>
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

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-2">اسم المستخدم</label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                  <input
                    type="text"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    required
                    autoFocus
                    placeholder="أدخل اسم المستخدم"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-right"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }}
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-semibold text-gray-600 mb-2">كلمة المرور</label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    placeholder="أدخل كلمة المرور"
                    className="w-full pr-4 pl-10 py-3 border-2 border-gray-300 rounded-xl text-sm bg-white focus:outline-none transition-all text-right"
                    onFocus={(e) => { e.target.style.borderColor = "#006C35"; e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.12)"; }}
                    onBlur={(e) => { e.target.style.borderColor = "#d1d5db"; e.target.style.boxShadow = "none"; }}
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl font-bold text-white text-sm transition-all active:scale-[0.98] disabled:opacity-60 disabled:cursor-not-allowed mt-2"
                style={{ background: loading ? "#6b7280" : "linear-gradient(135deg, #006C35, #00883f)" }}
              >
                {loading ? (
                  <span className="flex items-center gap-2">
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    جاري تسجيل الدخول...
                  </span>
                ) : (
                  <>
                    <LogIn className="w-4 h-4" />
                    دخول
                  </>
                )}
              </button>
            </form>

            <p className="text-center mt-6 text-sm text-gray-400">
              ليس لديك حساب؟{" "}
              <Link to="/register" className="text-[#006C35] font-semibold hover:underline">
                إنشاء حساب
              </Link>
            </p>
          </div>
        </div>

        <p className="text-center mt-5 text-xs text-gray-400">
          منصة سارع للطوارئ الطبية الموحدة
        </p>
      </motion.div>
    </div>
  );
}
