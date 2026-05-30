// Route guard: redirects unauthenticated users to /login and, when
// ``roles`` is provided, restricts the route to those roles (sending
// mismatched users to their own portal). Reads the JWT from localStorage.
import { Navigate } from "react-router-dom";
import type { ReactNode } from "react";

type Role = "dispatcher" | "medic" | "hospital" | "admin";

interface Props {
  children: ReactNode;
  /**
   * If provided, the user must have one of these roles to render
   * ``children``. When the role does not match we redirect to the
   * role's canonical landing page (or /login if the role is unknown).
   *
   * When ``roles`` is omitted, the route only requires a valid token
   * (any authenticated user may access it).
   */
  roles?: Role[];
}

const ROLE_LANDING: Record<Role, string> = {
  dispatcher: "/dispatcher",
  medic: "/medic",
  hospital: "/hospital",
  admin: "/admin",
};

// Read the current user's role from the persisted user object in
// localStorage, returning null if absent, unparseable, or unrecognised.
function readUserRole(): Role | null {
  try {
    const raw = localStorage.getItem("user");
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { role?: string } | null;
    const role = parsed?.role;
    if (
      role === "dispatcher" ||
      role === "medic" ||
      role === "hospital" ||
      role === "admin"
    ) {
      return role;
    }
    return null;
  } catch {
    return null;
  }
}

export default function ProtectedRoute({ children, roles }: Props) {
  const token = localStorage.getItem("token");

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  if (roles && roles.length > 0) {
    const role = readUserRole();
    if (role === null) {
      return <Navigate to="/login" replace />;
    }
    if (!roles.includes(role)) {
      // Logged in but wrong portal — bounce to the user's own portal
      // so they don't sit on an unauthorized blank page.
      return <Navigate to={ROLE_LANDING[role]} replace />;
    }
  }

  return <>{children}</>;
}
