export type TraceEvent = {
  seq: number;
  turn: number;
  type: string;
  [k: string]: unknown;
};

export type Approval = {
  id: string; status: string; action: string; provider_id: string; provider_name: string; slot: string;
  price: number; booking_id: string | null; idempotency_key: string; fingerprint: string;
};

export type MemoryFact = {
  value: unknown; source: string; status: string; evidence?: string; confirmed?: boolean;
};

export type Booking = {
  id: string; provider_name: string; service: string; slot: string; price: number;
  status: "upcoming" | "completed" | "cancelled";
};

export type Snapshot = {
  config: { scenario: string; user: string; clock: string };
  crashed: boolean;
  now: string;
  model: string;
  stage: string;
  intent: string | null;
  request: Record<string, unknown>;
  approval: Approval | null;
  execution: Record<string, unknown> | null;
  hold: { hold_id: string; expires_at: string; quoted_price: number } | null;
  options: number;
  option_labels: string[];
  service: string | null;
  suggestions: string[];
  turns: number;
  max_turns: number;
  closed?: boolean;
  strikes?: number;
  memory: Record<string, MemoryFact>;
  bookings: Booking[];
  events: TraceEvent[];
  next: number;
  scenarios: string[];
  user?: string;
  chat?: number;
  expires_in?: number | null;
  ttl?: number;
};

export type Reply = { reply: string | null; system?: boolean };

export type Step = string | { advance: number } | { restart: true } | { say: string; times?: number }
  | { repeat_text: string; times: number };

export type Playbook = {
  id: string; group: string; title: string; why: string; scenario?: string; steps: Step[];
  expect: {
    new_bookings?: number; cancelled?: number; stage?: string;
    reply_contains?: string[]; reply_not?: string[]; any_reply_contains?: string[];
  };
};

const BASE = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ?? "";
const KEY = "errand-token";
const UNREACHABLE = `Can't reach the backend${BASE ? ` at ${BASE}` : ""}. Is it running?`;

type Stored = { token: string; user: string; deadline: number };

/** An anonymous user lasts 15 minutes. The signed token (user id + expiry, HMAC'd by the server) is kept in
 * localStorage, so a reload or New chat keeps the same user until it runs out. */
function stored(): Stored | null {
  try {
    const s = JSON.parse(localStorage.getItem(KEY) || "null") as Stored | null;
    return s && s.token && s.deadline > Date.now() ? s : null;
  } catch {
    return null;
  }
}

export function sessionInfo(): Stored | null { return stored(); }

/** Correct the local countdown from the server's remaining seconds. */
export function syncDeadline(expiresIn: number | null | undefined) {
  const s = stored();
  if (s && typeof expiresIn === "number") {
    s.deadline = Date.now() + expiresIn * 1000;
    localStorage.setItem(KEY, JSON.stringify(s));
  }
}

export function dropSession() { localStorage.removeItem(KEY); }

/** Store a token the server re-issued (New chat: same user, fresh 15 minutes). Returns the new deadline. */
export function adoptSession(j: { token: string; user: string; expires_in: number }): number {
  const s: Stored = { token: j.token, user: j.user, deadline: Date.now() + j.expires_in * 1000 };
  localStorage.setItem(KEY, JSON.stringify(s));
  return s.deadline;
}

const ACCESS = "errand-access";

/** The server wants the password (no access pass yet, or it ran out). */
export class NeedPassword extends Error {}

let issuing: Promise<Stored> | null = null;

/** Ask the server for a brand-new anonymous user. The first time this needs the password; the server then hands
 * back an access pass so later 15-minute users don't ask again for a day. */
export function newUser(password?: string): Promise<Stored> {
  issuing ??= (async () => {
    try {
      let r: Response;
      try {
        r = await fetch(BASE + "/api/session", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ access: localStorage.getItem(ACCESS) ?? undefined, password }),
        });
      } catch {
        throw new Error(UNREACHABLE);
      }
      const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      if (r.status === 401 && j.password) {
        localStorage.removeItem(ACCESS);
        throw new NeedPassword(password ? j.error || "Wrong password." : "");
      }
      if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
      if (j.access) localStorage.setItem(ACCESS, j.access);
      const s: Stored = { token: j.token, user: j.user, deadline: Date.now() + j.expires_in * 1000 };
      localStorage.setItem(KEY, JSON.stringify(s));
      return s;
    } finally {
      issuing = null;
    }
  })();
  return issuing;
}

/** Thrown when the server rejects the token (expired, forged or missing). */
export class SessionEnded extends Error {}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const s = stored() ?? (await newUser());
  const init: RequestInit = { headers: { Authorization: `Bearer ${s.token}` } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  let r: Response;
  try {
    r = await fetch(BASE + path, init);
  } catch {
    throw new Error(UNREACHABLE);
  }
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (r.status === 401 && j.expired) {
    if (stored()?.token === s.token) dropSession();
    throw new SessionEnded(j.error || "Your session ended.");
  }
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j as T;
}
