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

// One isolated backend session per browser tab: its own mock provider, memory, clock and trace.
function sessionId(): string {
  let id = sessionStorage.getItem("errand-session");
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem("errand-session", id);
  }
  return id;
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { headers: { "X-Session-Id": sessionId() } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  let r: Response;
  try {
    r = await fetch(BASE + path, init);
  } catch {
    throw new Error(`Can't reach the backend${BASE ? ` at ${BASE}` : ""}. Is it running?`);
  }
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j as T;
}
