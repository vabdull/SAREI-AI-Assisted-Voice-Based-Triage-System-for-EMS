// Admin modal for creating or editing a user account. The same form
// serves both modes: when ``editing`` is set it updates that user (the
// username is fixed and the password is optional); otherwise it creates
// a new account.
import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, UserPlus, Save, Loader2 } from "lucide-react";
import type { UserRead, AdminUserCreate, AdminUserUpdate } from "../../types/api";

const ROLES: { value: string; label: string }[] = [
  { value: "dispatcher", label: "مشغّل طوارئ" },
  { value: "medic", label: "مسعف" },
  { value: "hospital", label: "مستشفى" },
  { value: "admin", label: "مدير" },
];

interface Props {
  open: boolean;
  /** When provided the modal edits this user; otherwise it creates a new one. */
  editing: UserRead | null;
  onClose: () => void;
  onCreate: (data: AdminUserCreate) => Promise<void>;
  onUpdate: (id: number, data: AdminUserUpdate) => Promise<void>;
}

export default function UserFormModal({
  open,
  editing,
  onClose,
  onCreate,
  onUpdate,
}: Props) {
  const isEdit = editing !== null;

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [role, setRole] = useState("dispatcher");
  const [password, setPassword] = useState("");
  const [isActive, setIsActive] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setPassword("");
    setSubmitting(false);
    if (editing) {
      setUsername(editing.username);
      setEmail(editing.email);
      setFullName(editing.full_name);
      setRole(editing.role);
      setIsActive(editing.is_active);
    } else {
      setUsername("");
      setEmail("");
      setFullName("");
      setRole("dispatcher");
      setIsActive(true);
    }
  }, [open, editing]);

  function validate(): string | null {
    if (!fullName.trim()) return "الاسم الكامل مطلوب";
    if (!isEdit) {
      if (!username.trim()) return "اسم المستخدم مطلوب";
      if (!email.trim()) return "البريد الإلكتروني مطلوب";
      if (password.length < 8) return "كلمة المرور يجب أن تكون 8 أحرف على الأقل";
    } else if (password && password.length < 8) {
      return "كلمة المرور يجب أن تكون 8 أحرف على الأقل";
    }
    return null;
  }

  async function handleSubmit() {
    const v = validate();
    if (v) {
      setError(v);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      if (isEdit && editing) {
        const payload: AdminUserUpdate = {
          email: email.trim(),
          full_name: fullName.trim(),
          role,
          is_active: isActive,
        };
        if (password) payload.password = password;
        await onUpdate(editing.id, payload);
      } else {
        await onCreate({
          username: username.trim(),
          email: email.trim(),
          full_name: fullName.trim(),
          role,
          password,
          is_active: isActive,
        });
      }
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "حدث خطأ غير متوقع");
    } finally {
      setSubmitting(false);
    }
  }

  const inputCls =
    "w-full px-3 py-2 bg-[#faf9f6] border border-[#e4e2db] rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-[#006C35]/20 text-right disabled:opacity-60 disabled:cursor-not-allowed";
  const labelCls = "block text-xs font-semibold text-gray-500 mb-1";

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
          dir="rtl"
        >
          <motion.div
            className="bg-white rounded-2xl shadow-xl w-full max-w-md overflow-hidden"
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-4 border-b border-[#f0ede6] bg-[#faf9f6]">
              <div className="flex items-center gap-2.5">
                <span className="text-[#006C35]">
                  {isEdit ? <Save className="w-4 h-4" /> : <UserPlus className="w-4 h-4" />}
                </span>
                <h2 className="text-sm font-semibold text-gray-700">
                  {isEdit ? "تعديل المستخدم" : "إضافة مستخدم جديد"}
                </h2>
              </div>
              <button
                onClick={onClose}
                className="p-1.5 rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-5 space-y-3.5 max-h-[70vh] overflow-y-auto">
              <div>
                <label className={labelCls}>الاسم الكامل</label>
                <input
                  className={inputCls}
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  placeholder="مثال: محمد العتيبي"
                />
              </div>

              <div>
                <label className={labelCls}>
                  اسم المستخدم {isEdit && <span className="text-gray-300">(غير قابل للتعديل)</span>}
                </label>
                <input
                  className={inputCls}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  disabled={isEdit}
                  placeholder="username"
                />
              </div>

              <div>
                <label className={labelCls}>البريد الإلكتروني</label>
                <input
                  className={inputCls}
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="user@example.com"
                />
              </div>

              <div>
                <label className={labelCls}>الدور</label>
                <select
                  className={inputCls}
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                >
                  {ROLES.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className={labelCls}>
                  كلمة المرور{" "}
                  {isEdit && (
                    <span className="text-gray-300">(اتركها فارغة لعدم التغيير)</span>
                  )}
                </label>
                <input
                  className={inputCls}
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="8 أحرف على الأقل"
                  autoComplete="new-password"
                />
              </div>

              <label className="flex items-center gap-2 cursor-pointer select-none pt-1">
                <input
                  type="checkbox"
                  checked={isActive}
                  onChange={(e) => setIsActive(e.target.checked)}
                  className="w-4 h-4 accent-[#006C35]"
                />
                <span className="text-sm text-gray-700">الحساب نشط</span>
              </label>

              {error && (
                <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                  {error}
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-[#f0ede6] bg-[#faf9f6]">
              <button
                onClick={onClose}
                disabled={submitting}
                className="px-4 py-2 rounded-xl text-sm font-semibold text-gray-500 hover:bg-gray-100 transition disabled:opacity-50"
              >
                إلغاء
              </button>
              <button
                onClick={handleSubmit}
                disabled={submitting}
                className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white transition disabled:opacity-60"
                style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
              >
                {submitting ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : isEdit ? (
                  <Save className="w-4 h-4" />
                ) : (
                  <UserPlus className="w-4 h-4" />
                )}
                {isEdit ? "حفظ التغييرات" : "إضافة المستخدم"}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
