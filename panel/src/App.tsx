import { useEffect, useState } from "react";
import { api, loginUrl, logoutUrl, type Me } from "./api";
import Dashboard from "./pages/Dashboard";
import Settings from "./pages/Settings";
import Shop from "./pages/Shop";
import Games from "./pages/Games";
import Players from "./pages/Players";
import Audit from "./pages/Audit";
import StatusPage from "./pages/Status";
import Tournaments from "./pages/Tournaments";
import Moderation from "./pages/Moderation";
import Economy from "./pages/Economy";
import DiscordSettings from "./pages/DiscordSettings";
import Design from "./pages/Design";

const NAV: { id: string; label: string }[] = [
  { id: "dashboard", label: "Dashboard" },
  { id: "players", label: "Players" },
  { id: "settings", label: "Настройки (все модули)" },
  { id: "feature-flags", label: "Feature Flags" },
  { id: "shop", label: "Shop" },
  { id: "games", label: "Games" },
  { id: "economy", label: "Economy" },
  { id: "audit", label: "Audit Log" },
  { id: "tournaments", label: "Tournaments" },
  { id: "matchmaking", label: "Matchmaking" },
  { id: "moderation", label: "Moderation" },
  { id: "ocr", label: "OCR" },
  { id: "discord", label: "Discord Settings" },
  { id: "design", label: "Design" },
];

const SUPPORTED: Record<string, string> = {
  ocr: "OCR-модуль не входит в данную ветку (нет cog'а в коде) — code-managed, панелью не управляется.",
};

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [section, setSection] = useState("dashboard");

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null))
      .finally(() => setAuthChecked(true));
  }, []);

  if (!authChecked) return <div className="main muted">Загрузка…</div>;
  if (!me) {
    return (
      <div className="main" style={{ maxWidth: 480, margin: "15vh auto", textAlign: "center" }}>
        <div className="card">
          <h2>r!z3 Control Center</h2>
          <p className="muted">Вход через Discord OAuth2</p>
          <a className="btn" href={loginUrl} style={{ display: "inline-block", padding: "10px 22px", textDecoration: "none" }}>
            Войти через Discord
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <h1>r!z3 Control</h1>
        {NAV.map(n => (
          <button key={n.id}
            className={`nav-item ${section === n.id ? "active" : ""}`}
            onClick={() => setSection(n.id)}>{n.label}</button>
        ))}
      </aside>
      <main className="main">
        <div className="topbar">
          <div>
            <strong>{me.username}</strong>{" "}
            <span className="badge muted">{me.role}</span>
          </div>
          <a href={logoutUrl} className="muted" style={{ fontSize: 13 }}>Выйти</a>
        </div>
        {section === "dashboard" && <Dashboard />}
        {section === "players" && <Players />}
        {section === "settings" && <Settings module={null} />}
        {section === "feature-flags" && <Settings module="feature" />}
        {section === "shop" && <Shop />}
        {section === "games" && <Games />}
        {section === "audit" && <Audit />}
        {section === "tournaments" && <Tournaments />}
        {section === "matchmaking" && <Settings module="matchmaking" />}
        {section === "moderation" && <Moderation />}
        {section === "economy" && <Economy />}
        {section === "discord" && <DiscordSettings />}
        {section === "design" && <Design />}
        {SUPPORTED[section] && <StatusPage title={NAV.find(n => n.id === section)!.label}
          description={SUPPORTED[section]} />}
      </main>
    </div>
  );
}
