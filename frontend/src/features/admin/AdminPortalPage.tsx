// Admin Portal: system administration screen. Lists users with role
// stats, supports creating/editing/deleting accounts, and shows the
// audit log. Admin-only (enforced by ProtectedRoute + backend RBAC).
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  LogOut, Bell, RefreshCw, Shield, Users,
  FileText, Search, CheckCircle, XCircle, Clock,
  UserPlus, Pencil, Trash2, Loader2,
} from "lucide-react";
import { authApi, adminApi } from "../../services/api";
import type { AuditLogRead, UserRead, AdminUserCreate, AdminUserUpdate } from "../../types/api";
import PortalSwitcher from "../../components/PortalSwitcher";
import UserFormModal from "./UserFormModal";

type Tab = "logs" | "users";

const ROLE_CONFIG: Record<string, { label: string; color: string }> = {
  dispatcher: { label: "مشغّل طوارئ", color: "bg-blue-50 text-blue-700 border-blue-200" },
  medic:      { label: "مسعف",        color: "bg-amber-50 text-amber-700 border-amber-200" },
  hospital:   { label: "مستشفى",      color: "bg-purple-50 text-purple-700 border-purple-200" },
  admin:      { label: "مدير",        color: "bg-red-50 text-red-700 border-red-200" },
};

function Panel({ title, icon, children, actions }: { title: string; icon: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="bg-white rounded-2xl border border-[#e4e2db] shadow-sm overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#f0ede6] bg-[#faf9f6]">
        <div className="flex items-center gap-2.5">
          <span className="text-[#006C35]">{icon}</span>
          <h2 className="text-sm font-semibold text-gray-700 tracking-wide uppercase">{title}</h2>
        </div>
        {actions}
      </div>
      <div>{children}</div>
    </div>
  );
}

function StatCard({ label, value, icon, color }: { label: string; value: number; icon: React.ReactNode; color: string }) {
  return (
    <div className="bg-white rounded-2xl border border-[#e4e2db] p-5 shadow-sm flex items-center gap-4">
      <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${color}`}>
        {icon}
      </div>
      <div>
        <div className="text-2xl font-bold text-gray-800">{value}</div>
        <div className="text-sm text-gray-400">{label}</div>
      </div>
    </div>
  );
}

export default function AdminPortalPage() {
  const navigate = useNavigate();
  const [user, setUser] = useState<UserRead | null>(null);
  const [tab, setTab] = useState<Tab>("users");
  const [logs, setLogs] = useState<AuditLogRead[]>([]);
  const [users, setUsers] = useState<UserRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");

  const [formOpen, setFormOpen] = useState(false);
  const [editingUser, setEditingUser] = useState<UserRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UserRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);

  useEffect(() => {
    authApi.me().then(setUser).catch(() => navigate("/login", { replace: true }));
  }, [navigate]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  function openCreate() {
    setEditingUser(null);
    setFormOpen(true);
  }

  function openEdit(u: UserRead) {
    setEditingUser(u);
    setFormOpen(true);
  }

  async function handleCreate(data: AdminUserCreate) {
    const created = await adminApi.createUser(data);
    setUsers((prev) => [created, ...prev]);
    setToast({ kind: "ok", msg: `تم إنشاء المستخدم @${created.username}` });
  }

  async function handleUpdate(id: number, data: AdminUserUpdate) {
    const updated = await adminApi.updateUser(id, data);
    setUsers((prev) => prev.map((u) => (u.id === id ? updated : u)));
    if (user?.id === id) setUser(updated);
    setToast({ kind: "ok", msg: "تم حفظ التغييرات" });
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await adminApi.deleteUser(deleteTarget.id);
      setUsers((prev) => prev.filter((u) => u.id !== deleteTarget.id));
      setToast({ kind: "ok", msg: `تم حذف المستخدم @${deleteTarget.username}` });
      setDeleteTarget(null);
    } catch (e) {
      setToast({ kind: "err", msg: e instanceof Error ? e.message : "تعذّر حذف المستخدم" });
    } finally {
      setDeleting(false);
    }
  }

  function loadData() {
    setLoading(true);
    if (tab === "logs") {
      adminApi.getAuditLogs().then(setLogs).catch(() => {}).finally(() => setLoading(false));
    } else {
      adminApi.getUsers().then(setUsers).catch(() => {}).finally(() => setLoading(false));
    }
  }

  useEffect(() => { loadData(); }, [tab]);

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    navigate("/login", { replace: true });
  }

  const filteredLogs = logs.filter((l) =>
    !search || l.action.includes(search) || l.resource_type.includes(search)
  );

  const filteredUsers = users.filter((u) =>
    !search || u.username.includes(search) || u.full_name.includes(search) || u.email.includes(search)
  );

  const roleStats = Object.keys(ROLE_CONFIG).map((r) => ({ role: r, count: users.filter((u) => u.role === r).length }));

  return (
    <div className="min-h-screen bg-[#f5f4f0] flex flex-col" dir="rtl">

      {/* Nav */}
      <header className="bg-white border-b border-[#e4e2db] shadow-sm sticky top-0 z-50">
        <div className="max-w-[1400px] mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Sare'i EMS" className="w-20 h-20 rounded-xl object-contain bg-white" />
            <div>
              <div className="text-base font-bold text-[#006C35] leading-tight">سارع</div>
              <div className="text-[10px] text-gray-400 leading-tight tracking-wider uppercase">Admin Portal</div>
            </div>
          </div>

          <div className="flex items-center gap-2 bg-red-50 border border-red-200 rounded-full px-4 py-1.5">
            <Shield className="w-4 h-4 text-red-600" />
            <span className="text-red-700 text-sm font-semibold">لوحة المدير</span>
          </div>

          <div className="flex items-center gap-3">
            <PortalSwitcher user={user} />
            <Bell className="w-5 h-5 text-gray-400 hover:text-[#006C35] cursor-pointer transition-colors" />
            <button onClick={loadData} className="p-2 rounded-lg hover:bg-[#e6f4ed] text-gray-400 hover:text-[#006C35] transition-colors">
              <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <div className="h-5 w-px bg-gray-200" />
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-sm font-bold" style={{ background: "linear-gradient(135deg, #dc2626, #ef4444)" }}>
                {user?.full_name?.[0]?.toUpperCase() ?? "A"}
              </div>
              <div className="text-right hidden sm:block">
                <div className="text-sm font-semibold text-gray-700 leading-tight">{user?.full_name ?? "..."}</div>
                <div className="text-[11px] text-gray-400 leading-tight">مدير النظام</div>
              </div>
            </div>
            <button onClick={logout} className="p-2 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 max-w-[1400px] mx-auto w-full px-6 py-6 flex flex-col gap-5">

        {/* Stats */}
        {tab === "users" && users.length > 0 && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {roleStats.map(({ role, count }) => {
              const cfg = ROLE_CONFIG[role];
              return (
                <StatCard
                  key={role}
                  label={cfg.label}
                  value={count}
                  icon={<Users className="w-5 h-5" />}
                  color={cfg.color}
                />
              );
            })}
          </div>
        )}

        {/* Tabs + search */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-3">
          <div className="flex gap-2 bg-white rounded-xl border border-[#e4e2db] p-1">
            {([
              { key: "users", label: "إدارة المستخدمين", icon: <Users className="w-4 h-4" /> },
              { key: "logs",  label: "سجلات التدقيق",    icon: <FileText className="w-4 h-4" /> },
            ] as { key: Tab; label: string; icon: React.ReactNode }[]).map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold transition-all ${
                  tab === t.key
                    ? "text-white shadow-sm"
                    : "text-gray-500 hover:text-gray-700 hover:bg-[#f5f4f0]"
                }`}
                style={tab === t.key ? { background: "linear-gradient(135deg, #006C35, #00883f)" } : {}}
              >
                {t.icon}{t.label}
              </button>
            ))}
          </div>

          <div className="relative flex-1 max-w-xs sm:mr-auto">
            <Search className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="بحث..."
              className="w-full pr-9 pl-4 py-2 bg-white border border-[#e4e2db] rounded-xl text-sm focus:outline-none text-right"
              onFocus={(e) => e.target.style.boxShadow = "0 0 0 3px rgba(0,108,53,0.15)"}
              onBlur={(e) => e.target.style.boxShadow = "none"}
            />
          </div>
        </div>

        {/* Content */}
        <AnimatePresence mode="wait">
          {loading ? (
            <motion.div key="loading" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-[#006C35]/20 border-t-[#006C35] rounded-full animate-spin" />
            </motion.div>
          ) : tab === "users" ? (
            <motion.div key="users" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
              <Panel title={`المستخدمون (${filteredUsers.length})`} icon={<Users className="w-4 h-4" />}
                actions={
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-gray-400">{users.filter((u) => u.is_active).length} نشط</span>
                    <button
                      onClick={openCreate}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold text-white transition active:scale-95"
                      style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}
                    >
                      <UserPlus className="w-3.5 h-3.5" />
                      إضافة مستخدم
                    </button>
                  </div>
                }>
                {filteredUsers.length === 0 ? (
                  <div className="text-center py-12 text-gray-400">
                    <Users className="w-10 h-10 text-gray-200 mx-auto mb-3" />
                    <p className="text-sm">لا يوجد مستخدمون</p>
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-[#f0ede6] bg-[#faf9f6]">
                          {["الاسم الكامل", "اسم المستخدم", "البريد الإلكتروني", "الدور", "الحالة", "تاريخ الإنشاء", "إجراءات"].map((h) => (
                            <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {filteredUsers.map((u, i) => {
                          const role = ROLE_CONFIG[u.role] ?? { label: u.role, color: "bg-gray-100 text-gray-600 border-gray-200" };
                          return (
                            <motion.tr
                              key={u.id}
                              initial={{ opacity: 0, y: 4 }}
                              animate={{ opacity: 1, y: 0 }}
                              transition={{ delay: i * 0.03 }}
                              className="border-b border-[#f5f4f0] hover:bg-[#faf9f6] transition-colors"
                            >
                              <td className="px-4 py-3">
                                <div className="flex items-center gap-2.5">
                                  <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold text-white flex-shrink-0"
                                    style={{ background: "linear-gradient(135deg, #006C35, #00883f)" }}>
                                    {u.full_name?.[0]?.toUpperCase() ?? "?"}
                                  </div>
                                  <span className="font-medium text-gray-800">{u.full_name}</span>
                                </div>
                              </td>
                              <td className="px-4 py-3 text-gray-500 font-mono text-xs">@{u.username}</td>
                              <td className="px-4 py-3 text-gray-500 text-xs">{u.email}</td>
                              <td className="px-4 py-3">
                                <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${role.color}`}>{role.label}</span>
                              </td>
                              <td className="px-4 py-3">
                                {u.is_active
                                  ? <span className="flex items-center gap-1 text-emerald-600 text-xs font-semibold"><CheckCircle className="w-3.5 h-3.5" />نشط</span>
                                  : <span className="flex items-center gap-1 text-red-500 text-xs font-semibold"><XCircle className="w-3.5 h-3.5" />معطّل</span>
                                }
                              </td>
                              <td className="px-4 py-3 text-gray-400 text-xs">
                                {new Date(u.created_at).toLocaleDateString("ar-SA")}
                              </td>
                              <td className="px-4 py-3">
                                <div className="flex items-center gap-1.5">
                                  <button
                                    onClick={() => openEdit(u)}
                                    title="تعديل"
                                    className="p-1.5 rounded-lg text-gray-400 hover:text-[#006C35] hover:bg-[#e6f4ed] transition"
                                  >
                                    <Pencil className="w-4 h-4" />
                                  </button>
                                  <button
                                    onClick={() => setDeleteTarget(u)}
                                    disabled={u.id === user?.id}
                                    title={u.id === user?.id ? "لا يمكنك حذف حسابك" : "حذف"}
                                    className="p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 transition disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-gray-400"
                                  >
                                    <Trash2 className="w-4 h-4" />
                                  </button>
                                </div>
                              </td>
                            </motion.tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            </motion.div>
          ) : (
            <motion.div key="logs" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
              <Panel title={`سجلات التدقيق (${filteredLogs.length})`} icon={<FileText className="w-4 h-4" />}
                actions={
                  <span className="flex items-center gap-1 text-xs text-gray-400"><Clock className="w-3.5 h-3.5" />الأحدث أولاً</span>
                }>
                {filteredLogs.length === 0 ? (
                  <div className="text-center py-12 text-gray-400">
                    <FileText className="w-10 h-10 text-gray-200 mx-auto mb-3" />
                    <p className="text-sm">لا توجد سجلات</p>
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-[#f0ede6] bg-[#faf9f6]">
                          {["الوقت", "الإجراء", "النوع", "المورد", "IP"].map((h) => (
                            <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wide">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {filteredLogs.map((log, i) => (
                          <motion.tr
                            key={log.id}
                            initial={{ opacity: 0, y: 4 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ delay: i * 0.02 }}
                            className="border-b border-[#f5f4f0] hover:bg-[#faf9f6] transition-colors"
                          >
                            <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">
                              {new Date(log.created_at).toLocaleString("ar-SA")}
                            </td>
                            <td className="px-4 py-3">
                              <span className="px-2 py-0.5 bg-[#f5f4f0] text-gray-700 rounded-lg text-xs font-mono border border-[#e4e2db]">
                                {log.action}
                              </span>
                            </td>
                            <td className="px-4 py-3 text-gray-600 text-xs">{log.resource_type}</td>
                            <td className="px-4 py-3 text-gray-400 font-mono text-xs">
                              {log.resource_id ? `#${String(log.resource_id).slice(0, 8)}` : "—"}
                            </td>
                            <td className="px-4 py-3 text-gray-400 font-mono text-xs">{log.ip_address ?? "—"}</td>
                          </motion.tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            </motion.div>
          )}
        </AnimatePresence>
      </main>

      {/* Add / Edit user modal */}
      <UserFormModal
        open={formOpen}
        editing={editingUser}
        onClose={() => setFormOpen(false)}
        onCreate={handleCreate}
        onUpdate={handleUpdate}
      />

      {/* Delete confirmation */}
      <AnimatePresence>
        {deleteTarget && (
          <motion.div
            className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => !deleting && setDeleteTarget(null)}
            dir="rtl"
          >
            <motion.div
              className="bg-white rounded-2xl shadow-xl w-full max-w-sm overflow-hidden"
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="p-6 text-center">
                <div className="w-12 h-12 rounded-full bg-red-50 flex items-center justify-center mx-auto mb-4">
                  <Trash2 className="w-6 h-6 text-red-500" />
                </div>
                <h3 className="text-base font-bold text-gray-800 mb-1.5">حذف المستخدم</h3>
                <p className="text-sm text-gray-500">
                  هل أنت متأكد من حذف <span className="font-semibold text-gray-700">{deleteTarget.full_name}</span>
                  {" "}(@{deleteTarget.username})؟ لا يمكن التراجع عن هذا الإجراء.
                </p>
              </div>
              <div className="flex items-center justify-center gap-2 px-6 pb-6">
                <button
                  onClick={() => setDeleteTarget(null)}
                  disabled={deleting}
                  className="px-4 py-2 rounded-xl text-sm font-semibold text-gray-500 hover:bg-gray-100 transition disabled:opacity-50"
                >
                  إلغاء
                </button>
                <button
                  onClick={confirmDelete}
                  disabled={deleting}
                  className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white bg-red-500 hover:bg-red-600 transition disabled:opacity-60"
                >
                  {deleting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                  حذف
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Toast */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 20 }}
            className={`fixed bottom-6 left-1/2 -translate-x-1/2 z-[110] px-4 py-2.5 rounded-xl text-sm font-semibold text-white shadow-lg ${
              toast.kind === "ok" ? "bg-[#006C35]" : "bg-red-500"
            }`}
            dir="rtl"
          >
            {toast.msg}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Footer */}
      <footer className="bg-white border-t border-[#e4e2db] py-3 px-6">
        <div className="max-w-[1400px] mx-auto flex items-center justify-between">
          <div className="text-[11px] text-gray-400">منصة سارع للطوارئ الطبية الموحدة</div>
          <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
            بوابة مدير النظام
          </div>
        </div>
      </footer>
    </div>
  );
}
