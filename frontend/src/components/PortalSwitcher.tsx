// Admin-only dropdown that lets an administrator jump between the four
// role portals (dispatcher, ambulance, hospital, admin). Renders nothing
// for non-admin users.
import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { LayoutGrid, Phone, Ambulance, Building2, Shield, ChevronDown } from "lucide-react";
import type { UserRead } from "../types/api";

const PORTALS = [
  {
    path: "/dispatcher",
    labelAr: "مشغّل طوارئ",
    labelEn: "Dispatcher",
    icon: <Phone className="w-4 h-4" />,
    color: "text-blue-700",
    bg: "bg-blue-50 hover:bg-blue-100",
    border: "border-blue-200",
    dot: "bg-blue-500",
  },
  {
    path: "/medic",
    labelAr: "مسعف",
    labelEn: "Medic",
    icon: <Ambulance className="w-4 h-4" />,
    color: "text-amber-700",
    bg: "bg-amber-50 hover:bg-amber-100",
    border: "border-amber-200",
    dot: "bg-amber-500",
  },
  {
    path: "/hospital",
    labelAr: "مستشفى",
    labelEn: "Hospital",
    icon: <Building2 className="w-4 h-4" />,
    color: "text-purple-700",
    bg: "bg-purple-50 hover:bg-purple-100",
    border: "border-purple-200",
    dot: "bg-purple-500",
  },
  {
    path: "/admin",
    labelAr: "لوحة المدير",
    labelEn: "Admin",
    icon: <Shield className="w-4 h-4" />,
    color: "text-red-700",
    bg: "bg-red-50 hover:bg-red-100",
    border: "border-red-200",
    dot: "bg-red-500",
  },
] as const;

interface Props {
  user: UserRead | null;
}

export default function PortalSwitcher({ user }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  if (user?.role !== "admin") return null;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 px-3 py-1.5 rounded-xl border border-[#e4e2db] bg-[#faf9f6] hover:bg-[#006C35]/5 hover:border-[#006C35]/30 text-gray-600 hover:text-[#006C35] transition-all text-sm font-medium"
      >
        <LayoutGrid className="w-4 h-4" />
        <span className="hidden sm:inline">تبديل البوابة</span>
        <ChevronDown
          className={`w-3.5 h-3.5 transition-transform duration-200 ${open ? "rotate-180" : ""}`}
        />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -6 }}
            transition={{ duration: 0.15 }}
            className="absolute left-0 top-full mt-2 w-56 bg-white rounded-2xl border border-[#e4e2db] shadow-xl overflow-hidden z-[100]"
            style={{ direction: "rtl" }}
          >
            <div className="px-4 py-2.5 border-b border-[#f0ede6] bg-[#faf9f6]">
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                اختر البوابة
              </p>
            </div>

            <div className="p-2 flex flex-col gap-1">
              {PORTALS.map((portal) => {
                const isActive = location.pathname.startsWith(portal.path);
                return (
                  <button
                    key={portal.path}
                    onClick={() => {
                      setOpen(false);
                      navigate(portal.path);
                    }}
                    className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl border transition-all text-right ${
                      isActive
                        ? `${portal.bg} ${portal.border} ${portal.color} border`
                        : "border-transparent hover:bg-[#f5f4f0] text-gray-600"
                    }`}
                  >
                    <span
                      className={`w-8 h-8 rounded-xl flex items-center justify-center shrink-0 ${
                        isActive ? portal.bg : "bg-gray-100"
                      } ${portal.color}`}
                    >
                      {portal.icon}
                    </span>
                    <div className="flex-1 min-w-0 text-right">
                      <div className={`text-sm font-semibold leading-tight ${isActive ? portal.color : "text-gray-700"}`}>
                        {portal.labelAr}
                      </div>
                      <div className="text-[11px] text-gray-400 leading-tight">{portal.labelEn}</div>
                    </div>
                    {isActive && (
                      <span className={`w-2 h-2 rounded-full shrink-0 ${portal.dot}`} />
                    )}
                  </button>
                );
              })}
            </div>

            <div className="px-4 py-2 border-t border-[#f0ede6] bg-[#faf9f6]">
              <p className="text-[10px] text-gray-400 text-center">
                صلاحية المدير — Admin Access
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
