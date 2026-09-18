"use client";

import { useCallback, useEffect, useState } from "react";
import { clearToken, me, type User } from "@/lib/api";
import Login from "@/components/Login";
import AdminDashboard from "@/components/AdminDashboard";
import IntakeStudio from "@/components/IntakeStudio";
import CitizenPortal from "@/components/CitizenPortal";

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [citizenPortal, setCitizenPortal] = useState(false);

  useEffect(() => {
    let active = true;
    const finish = (nextUser: User | null) => {
      if (!active) return;
      window.clearTimeout(timeout);
      setUser(nextUser);
      setLoading(false);
    };
    // 后端代理意外不可达时仍应显示登录页，不能无限停留在“加载中”。
    const timeout = window.setTimeout(() => finish(null), 8000);
    me()
      .then((currentUser) => finish(currentUser))
      .catch(() => finish(null));
    return () => {
      active = false;
      window.clearTimeout(timeout);
    };
  }, []);

  const onLogin = useCallback((u: User) => setUser(u), []);

  const onLogout = useCallback(() => {
    clearToken();
    setUser(null);
  }, []);

  if (loading) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: "#6b7280" }}>
        加载中…
      </div>
    );
  }

  if (citizenPortal) {
    return <CitizenPortal onBack={() => setCitizenPortal(false)} />;
  }

  if (!user) {
    return <Login onLogin={onLogin} onCitizenPortal={() => setCitizenPortal(true)} />;
  }

  if (user.role === "department_admin" || user.role === "system_admin") {
    return <AdminDashboard user={user} onLogout={onLogout} />;
  }

  return <IntakeStudio user={user} onLogout={onLogout} />;
}
