import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Approval, type Playbook, type Reply, type Snapshot, type TraceEvent } from "./api";
import {
  type Active, BookingsPane, expandSteps, expectText, MemoryPane, PlaybooksPane, StatePane, TracePane, verdict,
} from "./panels";

type Msg = { id: number; kind: "user" | "agent" | "system"; text: string };
type Tab = "trace" | "memory" | "bookings" | "state" | "playbooks";
const DEV = new URLSearchParams(window.location.search).get("dev") === "1";
const TABS: Tab[] = DEV ? ["trace", "memory", "bookings", "state", "playbooks"] : ["trace", "memory", "bookings"];
const TAB_LABELS: Record<Tab, string> = { trace: "Trace", memory: "Memory", bookings: "Bookings", state: "State", playbooks: "Playbooks" };
const PARSER_LABELS: Record<string, string> = { rules: "Rule-based (offline)", openai: "OpenAI", anthropic: "Anthropic" };
const MAX_CHARS = 1000;
const EXAMPLES = [
  "Find a plumber in Indiranagar this week, mornings, under ₹1,500",
  "Move my AC servicing to Saturday",
  "What do you remember about me?",
];

let msgId = 0;
const fmtSlot = (s: string) =>
  new Date(s).toLocaleString("en-IN", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });

function ApprovalCard({ ap, busy, onYes, onNo }: { ap: Approval; busy: boolean; onYes: () => void; onNo: () => void }) {
  return (
    <div className="apcard">
      <div className="aptitle">Please confirm</div>
      <div className="aprow"><span>Action</span><b>{ap.action[0].toUpperCase() + ap.action.slice(1)}</b></div>
      <div className="aprow"><span>Provider</span><b>{ap.provider_name}</b></div>
      <div className="aprow"><span>When</span><b>{fmtSlot(ap.slot)}</b></div>
      {ap.action !== "cancel" && <div className="aprow"><span>Price</span><b>₹{ap.price.toLocaleString("en-IN")}</b></div>}
      {ap.booking_id && <div className="aprow"><span>Booking</span><b>{ap.booking_id}</b></div>}
      <div className="apbtns">
        <button className="primary" disabled={busy} onClick={onYes}>Confirm</button>
        <button disabled={busy} onClick={onNo}>Cancel</button>
      </div>
    </div>
  );
}

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>(DEV ? "playbooks" : "trace");
  const [input, setInput] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [active, setActiveState] = useState<Active | null>(null);
  const [form, setForm] = useState({ scenario: "default", parser: "openai", user: "u1", clock: "real" });

  const since = useRef(0);
  const generation = useRef(0); // bumps on every new session, so stale polls are discarded
  const busyRef = useRef(false);
  const activeRef = useRef<Active | null>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const refreshChain = useRef<Promise<Snapshot | null>>(Promise.resolve(null));

  const setActive = (a: Active | null) => { activeRef.current = a; setActiveState(a ? { ...a } : null); };
  const setBusyBoth = (b: boolean) => { busyRef.current = b; setBusy(b); };
  const say = (kind: Msg["kind"], text: string) => setMsgs((m) => [...m, { id: ++msgId, kind, text }]);
  const showToast = (t: string) => { setToast(t); window.setTimeout(() => setToast((x) => (x === t ? null : x)), 6000); };

  // Serialised so overlapping polls never append the same trace rows twice.
  const refresh = useCallback((): Promise<Snapshot | null> => {
    refreshChain.current = refreshChain.current.then(async () => {
      const gen = generation.current;
      try {
        const s = await api<Snapshot>(`/api/state?since=${since.current}`);
        if (gen !== generation.current) return null;
        since.current = s.next;
        if (s.events.length) setEvents((ev) => [...ev, ...s.events]);
        setSnap(s);
        return s;
      } catch {
        return null;
      }
    });
    return refreshChain.current;
  }, []);

  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    if (DEV) api<Playbook[]>("/api/playbooks").then(setPlaybooks).catch((e) => showToast(e.message));
    refresh().then((s) => {
      if (s) setForm(s.config);
      else say("system", "Can't reach the backend right now. Please try again in a moment.");
    });
    window.setInterval(() => { if (!busyRef.current) refresh(); }, 2500);
  }, [refresh]);

  useEffect(() => { chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight }); }, [msgs, busy, snap?.stage]);

  async function send(text: string): Promise<string | null> {
    if (busyRef.current || !text.trim()) return null;
    say("user", text.length > 400 ? `${text.slice(0, 400)}… (${text.length} chars)` : text);
    setBusyBoth(true);
    let reply: string | null = null;
    try {
      const r = await api<Reply>("/api/message", { text });
      reply = r.reply;
      if (r.reply) say(r.system ? "system" : "agent", r.reply);
    } catch (e) { showToast((e as Error).message); }
    setBusyBoth(false);
    await refresh();
    return reply;
  }

  async function control(path: string, body: object = {}): Promise<string | null> {
    if (busyRef.current) return null;
    setBusyBoth(true);
    let reply: string | null = null;
    try {
      const r = await api<Reply>(path, body);
      reply = r.reply;
      if (r.reply) say(r.system ? "system" : "agent", r.reply);
    } catch (e) { showToast((e as Error).message); }
    setBusyBoth(false);
    await refresh();
    return reply;
  }

  async function newSession(over: Partial<typeof form> = {}): Promise<boolean> {
    if (busyRef.current) return false;
    const cfg = { ...form, ...over, user: (over.user ?? form.user).trim() || "u1" };
    setBusyBoth(true);
    try {
      await api("/api/reset", cfg);
    } catch (e) { showToast((e as Error).message); setBusyBoth(false); return false; }
    generation.current++;
    since.current = 0;
    setEvents([]);
    setMsgs([]);
    setForm(cfg);
    if (DEV) say("system", `New session · scenario “${cfg.scenario}” · parser ${cfg.parser} · user ${cfg.user}`
      + `${cfg.clock === "fixed" ? " · clock fixed at Sun 01 Nov 2026 08:00" : ""}.`);
    setBusyBoth(false);
    await refresh();
    return true;
  }

  async function stepPlaybook() {
    const a = activeRef.current;
    if (!a || busyRef.current || a.i >= a.steps.length) return;
    const s = a.steps[a.i];
    a.i++;
    setActive(a);
    let reply: string | null = null;
    if (s.kind === "say") reply = await send(s.text);
    else if (s.kind === "advance") await control("/api/advance", { minutes: s.minutes });
    else reply = await control("/api/restart");
    if (activeRef.current !== a) return;
    if (reply !== null && s.kind !== "advance") a.replies.push(reply);
    if (a.i >= a.steps.length) {
      const snapNow = await refresh();
      if (snapNow) a.fails = verdict(a.pb, snapNow, a.replies);
      say("system", a.fails?.length === 0 ? `■ Playbook finished: ✓ PASS. ${expectText(a.pb.expect)}`
        : `■ Playbook finished: ✗ FAIL — ${a.fails?.join("; ")}`);
    }
    setActive(a);
  }

  async function startPlaybook(pb: Playbook, runAll: boolean) {
    setActive(null);
    if (!(await newSession({ scenario: pb.scenario || "default", clock: "fixed" }))) return;
    say("system", `▶ Playbook: ${pb.title}\n${pb.why}`);
    const a: Active = { pb, steps: expandSteps(pb.steps), i: 0, replies: [] };
    setActive(a);
    if (runAll) while (activeRef.current === a && a.i < a.steps.length) await stepPlaybook();
  }

  function submit() {
    const t = input;
    setInput("");
    send(t);
  }

  const pendingApproval = snap && !snap.crashed && snap.stage === "awaiting_approval" && snap.approval?.status === "pending"
    ? snap.approval : null;
  const choices = snap && !snap.crashed && snap.stage === "choosing"
    ? Array.from({ length: Math.min(snap.options, 9) }, (_, i) => String(i + 1)) : [];

  return (
    <>
      <header>
        <h1>🧰 Errand Agent</h1>
        {DEV && <>
          <span className={"pill stage-" + (snap?.crashed ? "crashed" : snap?.stage ?? "idle")}>
            {snap?.crashed ? "💥 process dead" : snap?.stage ?? "…"}</span>
          <span className="pill mono">🕒 {snap?.now ?? "…"}</span>
          <label>Scenario
            <select value={form.scenario} onChange={(e) => setForm({ ...form, scenario: e.target.value })}>
              {(snap?.scenarios ?? [form.scenario]).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label>Parser
            <select value={form.parser} onChange={(e) => setForm({ ...form, parser: e.target.value })}>
              {(snap?.parsers ?? [form.parser]).map((p) => <option key={p} value={p}>{PARSER_LABELS[p] ?? p}</option>)}
            </select>
          </label>
          <label>User <input type="text" size={4} value={form.user} onChange={(e) => setForm({ ...form, user: e.target.value })} /></label>
          <button disabled={busy} onClick={() => control("/api/advance", { minutes: 15 })}>⏩ +15 min</button>
          <button className="danger" disabled={busy} onClick={() => control("/api/restart")}>💥 Restart process</button>
        </>}
        <span className="spacer" />
        <button disabled={busy} onClick={() => { setActive(null); newSession({ clock: "real" }); }}>New chat</button>
        <button className={open ? "on" : ""} onClick={() => setOpen(!open)}>Details {open ? "▸" : "◂"}</button>
      </header>

      <main className={open ? "with-drawer" : ""}>
        <section className="chatcol">
          <div className="chat" ref={chatRef}>
            {msgs.length === 0 && (
              <div className="empty">
                <div className="hello">What can I book for you?</div>
                <div className="muted">I book, move and cancel home services. I always ask before booking anything.</div>
                <div className="examples">
                  {EXAMPLES.map((t) => <button key={t} className="chip" disabled={busy} onClick={() => send(t)}>{t}</button>)}
                </div>
              </div>
            )}
            {msgs.map((m) => (
              <div key={m.id} className={["msg", m.kind, m.kind === "agent" && m.text.includes("✅") ? "ok" : ""].join(" ")}>
                {m.text}
              </div>
            ))}
            {busy && <div className="typing">…</div>}
            {pendingApproval && !busy &&
              <ApprovalCard ap={pendingApproval} busy={busy} onYes={() => send("yes")} onNo={() => send("no")} />}
          </div>
          {(choices.length > 0 || snap?.crashed) && (
            <div className="quick">
              {snap?.crashed && <button className="danger" disabled={busy} onClick={() => control("/api/restart")}>Restart process</button>}
              {choices.map((c) => <button key={c} disabled={busy} onClick={() => send(c)}>Option {c}</button>)}
            </div>
          )}
          <div className="composer">
            <textarea ref={inputRef} value={input} autoFocus placeholder="Type a message…"
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
            <button className="primary" disabled={busy || !input.trim()} onClick={submit}>Send</button>
          </div>
          {input.length > MAX_CHARS && <div className="counter over">{input.length} / {MAX_CHARS} characters</div>}
        </section>

        {open && (
          <aside className="drawer">
            <div className="tabs">
              {TABS.map((t) => (
                <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
                  {TAB_LABELS[t]}{t === "trace" && events.length > 0 && <span className="badge">({events.length})</span>}
                </button>
              ))}
            </div>
            <div className="pane">
              {tab === "trace" && <TracePane events={events} />}
              {snap && tab === "memory" && <MemoryPane s={snap} />}
              {snap && tab === "bookings" && <BookingsPane bookings={snap.bookings} />}
              {DEV && snap && tab === "state" && <StatePane s={snap} />}
              {DEV && tab === "playbooks" && <PlaybooksPane playbooks={playbooks} active={active} busy={busy} onStart={startPlaybook}
                onStep={stepPlaybook} onPick={(t) => { setInput(t); inputRef.current?.focus(); }} />}
            </div>
          </aside>
        )}
      </main>
      {toast && <div className="toast">{toast}</div>}
    </>
  );
}
