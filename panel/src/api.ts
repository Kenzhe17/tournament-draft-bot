// API-клиент Control Center. Все запросы same-origin (Vite proxy /
// Railway same-domain), сессионная кука httpOnly, CSRF-заголовок
// на все мутирующие запросы.

const CSRF = { "X-CC-Request": "1" };

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...CSRF, ...(init?.headers || {}) },
    ...init,
  });
  if (r.status === 401) {
    window.location.href = "/auth/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!r.ok) {
    let detail: unknown = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* noop */ }
    throw new ApiError(r.status, detail);
  }
  return r.json() as Promise<T>;
}

const get = <T>(p: string) => req<T>(p);
const post = <T>(p: string, body?: unknown) =>
  req<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T>(p: string, body: unknown) =>
  req<T>(p, { method: "PUT", body: JSON.stringify(body) });
const del = <T>(p: string) => req<T>(p, { method: "DELETE" });

// ---------- types ----------
export interface Me {
  id: string; username: string; avatar: string | null;
  guild_id: number | null; role: string; permissions: string[];
}
export interface Health { service: string; status: string; database: string }
export interface Dashboard {
  services: { name: string; status: string; extra?: Record<string, unknown> }[];
  counts?: Record<string, number>;
  [k: string]: unknown;
}
export interface SettingDef {
  key: string; type: string; module: string; description: string;
  scope: string; default: unknown; min: number | null; max: number | null;
  permission: string; runtime_editable: boolean;
  consumers_pending: boolean; applies_to?: string;
}
export interface SettingValue {
  key: string; module: string; value: unknown; version: number;
  is_default: boolean; updated_at: string | null; updated_by: number | null;
}
export interface SettingDetail extends SettingDef {
  value: unknown; version: number; is_default: boolean;
  updated_at: string | null; updated_by: number | null;
}
export interface AuditRow {
  id: number; guild_id: number; actor_id: number; action: string;
  object_type: string | null; object_id: string | null;
  old_value: unknown; new_value: unknown; result: string;
  created_at: string;
}
export interface ShopItem {
  id: string; name: string; description: string; price: number;
  category: string; rarity: string; is_sellable: boolean;
  is_active: boolean; cosmetic_type: string; value: string;
  item_type: string; role_id: number | null; required_level: number;
}
export interface Minigame {
  id: string; command_name?: string; name: string; description: string;
  category: string; difficulty: string; min_bet: number; max_bet: number;
  multiplier: number; is_pvp: boolean; is_pve: boolean; is_active: boolean;
}
export interface PlayerRow {
  user_id: number; name: string; elo: number | null; wins: number | null;
  finals: number | null; games: number | null; level: number | null;
  xp: number | null; total_earnings: number | null; reputation: number | null;
}

// ---------- endpoints ----------
export const api = {
  health: () => get<Health>("/api/health"),
  me: () => get<Me>("/api/me"),
  dashboard: () => get<Dashboard>("/api/dashboard"),

  listSettings: (module?: string) =>
    get<{ modules: string[]; settings: SettingDef[] }>(
      "/api/settings" + (module ? `?module=${module}` : "")),
  listValues: () =>
    get<{ guild_id: number; values: SettingValue[] }>("/api/settings/values"),
  getSetting: (key: string) => get<SettingDetail>(`/api/settings/${encodeURIComponent(key)}`),
  setSetting: (key: string, value: unknown, expected_version?: number, guild_id?: number) =>
    put<{ key: string; value: unknown; version: number }>(
      `/api/settings/${encodeURIComponent(key)}`,
      { value, expected_version, guild_id }),
  settingHistory: (key: string) =>
    get<{ history: Record<string, unknown>[] }>(
      `/api/settings/${encodeURIComponent(key)}/history?limit=50`),
  rollbackSetting: (key: string, target_version: number) =>
    post<{ key: string; version: number }>(
      `/api/settings/${encodeURIComponent(key)}/rollback`,
      { target_version }),

  audit: (params = "") =>
    get<{ items: AuditRow[] }>(`/api/audit?limit=200${params}`),

  shopItems: (all = false) =>
    get<{ items: ShopItem[]; total: number }>(`/api/shop/items?include_inactive=${all}`),
  shopCreate: (item: Partial<ShopItem>) => post("/api/shop/items", item),
  shopUpdate: (id: string, item: Partial<ShopItem>) =>
    put(`/api/shop/items/${encodeURIComponent(id)}`, item),
  shopHide: (id: string) => post(`/api/shop/items/${encodeURIComponent(id)}/hide`),
  shopShow: (id: string) => post(`/api/shop/items/${encodeURIComponent(id)}/show`),
  shopDelete: (id: string) => del(`/api/shop/items/${encodeURIComponent(id)}`),

  games: (all = false) =>
    get<{ games: Minigame[] }>(`/api/games/minigames?include_inactive=${all}`),
  gameUpdate: (id: string, patch: Partial<Minigame>) =>
    put(`/api/games/minigames/${encodeURIComponent(id)}`, patch),

  playersSearch: (q: string) =>
    get<{ items: PlayerRow[]; count: number }>(
      `/api/players/search?q=${encodeURIComponent(q)}`),
  playersLeaderboard: () =>
    get<{ items: PlayerRow[]; count: number }>("/api/players/leaderboard?limit=50"),
  playerDetail: (id: number) => get<Record<string, unknown>>(`/api/players/${id}`),

  actions: (action: string, parameters: Record<string, unknown> = {}, idempotency_key?: string) =>
    post<{ deduplicated: boolean; action: Record<string, unknown> }>("/api/actions", {
      action, parameters,
      idempotency_key: idempotency_key ?? `${action}:${Date.now()}:${Math.random().toString(36).slice(2)}`,
    }),
  actionStatus: (id: string) => get<Record<string, unknown>>(`/api/actions/${id}`),
};

export const loginUrl = "/auth/login";
export const logoutUrl = "/auth/logout";

// ---------- admin modules ----------
export interface BotSnapshot {
  available: boolean;
  reason?: string;
  last_seen?: string;
  tournaments?: Record<string, TournamentSnap[]>;
  matchmaking?: Record<string, MMQueueSnap[]>;
  vac_active_count?: Record<string, number>;
  guilds?: Record<string, GuildResources>;
}
export interface TournamentSnap {
  id: string; channel_id: number; size: string; phase: string;
  registration: string; formation_mode: string; organizer_id: number;
  is_test: boolean; created_at: string;
  players: Record<string, number>;
}
export interface MMQueueSnap {
  channel_id: number; format: string; state: string; players: number;
  player_names: string[]; required: number | null; timer_started: boolean;
  tournament_channel_id: number; match_num: number;
}
export interface GuildResources {
  name: string;
  channels: { id: string; name: string; type: string }[];
  roles: { id: string; name: string }[];
}
export interface VacRow {
  user_id: number; guild_id: number; reason?: string;
  expires_at?: string; issued_at?: string; level?: number;
  revoked_at?: string; revoked_by?: number; duration?: string;
  [k: string]: unknown;
}
export interface EconomyPlayer {
  user_id: number; guild_id: number; balance: number | null;
  recent_grants: { match_id: string; amount: number; granted_at: string }[];
}
export interface GrantRow {
  match_id: string; user_id: number; amount: number; granted_at: string;
}
export interface TemplateRow {
  guild_id: number; key: string; payload: Record<string, unknown>;
  version: number; updated_by: number; updated_at: string;
}

export const adminApi = {
  snapshot: () => get<BotSnapshot>("/api/admin/snapshot"),
  economyPlayer: (uid: number) => get<EconomyPlayer>(`/api/admin/economy/player/${uid}`),
  economyGrants: () => get<{ items: GrantRow[] }>("/api/admin/economy/grants?limit=100"),
  vacActive: () => get<{ items: VacRow[] }>("/api/admin/moderation/vac/active"),
  vacHistory: (uid?: number) =>
    get<{ items: VacRow[] }>(`/api/admin/moderation/vac/history${uid ? `?user_id=${uid}` : ""}`),
  templates: () => get<{ items: TemplateRow[] }>("/api/admin/templates"),
  template: (key: string) => get<TemplateRow>(`/api/admin/templates/${encodeURIComponent(key)}`),
  putTemplate: (key: string, payload: Record<string, unknown>) =>
    put<{ key: string; version: number }>(`/api/admin/templates/${encodeURIComponent(key)}`, { payload }),
};

export const adminApi2 = {
  modCases: (target?: number) =>
    get<{ items: Record<string, unknown>[] }>(
      `/api/admin/moderation/cases${target ? `?target_id=${target}` : ""}`),
  modReports: () =>
    get<{ items: Record<string, unknown>[] }>("/api/admin/moderation/reports"),
};
