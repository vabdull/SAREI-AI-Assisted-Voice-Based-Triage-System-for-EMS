// Application route map. Defines every page URL and wraps the four
// role-specific portals in <ProtectedRoute> so only users with the
// correct role (admin can access all) can open them. Unauthenticated
// visitors are redirected to /login.
import { Routes, Route, Navigate } from "react-router-dom";
import ProtectedRoute from "../components/ProtectedRoute";
import LoginPage from "../features/auth/LoginPage";
import RegisterPage from "../features/auth/RegisterPage";
import DispatcherPortalPage from "../features/dispatcher/DispatcherPortalPage";
import MedicPortalPage from "../features/medic/MedicPortalPage";
import HospitalPortalPage from "../features/hospital/HospitalPortalPage";
import AdminPortalPage from "../features/admin/AdminPortalPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/login" replace />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route
        path="/dispatcher"
        element={
          <ProtectedRoute roles={["dispatcher", "admin"]}>
            <DispatcherPortalPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/medic"
        element={
          <ProtectedRoute roles={["medic", "admin"]}>
            <MedicPortalPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/hospital"
        element={
          <ProtectedRoute roles={["hospital", "admin"]}>
            <HospitalPortalPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/admin"
        element={
          <ProtectedRoute roles={["admin"]}>
            <AdminPortalPage />
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}
