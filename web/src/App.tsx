import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Approval, type Playbook, type Reply, type Snapshot, type TraceEvent } from "./api";
import {
  type Active, BookingsPane, expandSteps, expectText, MemoryPane, PlaybooksPane, ProfilePane, StatePane, TracePane, verdict,
} from "./panels";
import MessageBody, { hasOptions } from "./Message";

type Msg = { id: number; kind: "user" | "agent" | "system"; text: string };
type Tab = "trace" | "memory" | "bookings" | "state" | "playbooks";
const DEV = new URLSearchParams(window.location.search).get("dev") === "1";
const TABS: Tab[] = DEV ? ["trace", "memory", "bookings", "state", "playbooks"] : ["trace", "memory", "bookings"];
const TAB_LABELS: Record<Tab, string> = { trace: "Trace", memory: "Memory", bookings: "Bookings", state: "State", playbooks: "Playbooks" };
const MAX_CHARS = 1000;
const EXAMPLES = [
  "Find a plumber in Indiranagar this week, mornings, under ₹1,500",
  "Move my AC servicing to Saturday",
  "What do you remember about me?",
];

let msgId = 0;
const fmtDay = (s: string) =>
  new Date(s).toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
const fmtTime = (s: string) =>
  new Date(s).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true });

const SERVICE_LABELS: Record<string, string> = {
  plumber: "Plumber", electrician: "Electrician", ac_service: "AC servicing", cleaning: "Home cleaning",
  carpenter: "Carpenter", pest_control: "Pest control",
};

function ApprovalCard({ ap, service, busy, onYes, onNo }: {
  ap: Approval; service: string | null; busy: boolean; onYes: () => void; onNo: () => void;
}) {
  return (
    <div className="apcard">
      <div className="aptitle">Please confirm</div>
      <div className="aprow"><span>Action</span><b>{ap.action[0].toUpperCase() + ap.action.slice(1)}</b></div>
      {service && <div className="aprow"><span>Service</span><b>{SERVICE_LABELS[service] ?? service}</b></div>}
      <div className="aprow"><span>Provider</span><b>{ap.provider_name}</b></div>
      <div className="aprow"><span>Date</span><b>{fmtDay(ap.slot)}</b></div>
      <div className="aprow"><span>Time</span><b>{fmtTime(ap.slot)}</b></div>
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
  const [panel, setPanel] = useState<"profile" | "details" | null>(null);
  const open = panel !== null;
  const toggle = (p: "profile" | "details") => setPanel((x) => (x === p ? null : p));
  const [tab, setTab] = useState<Tab>(DEV ? "playbooks" : "trace");
  const [input, setInput] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [active, setActiveState] = useState<Active | null>(null);
  const [form, setForm] = useState({ scenario: "default", user: "u1", clock: "real" });

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

  async function send(text: string, shown?: string): Promise<string | null> {
    if (busyRef.current || !text.trim()) return null;
    const echo = shown ?? text;
    say("user", echo.length > 400 ? `${echo.slice(0, 400)}… (${echo.length} chars)` : echo);
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
    if (DEV) say("system", `New session · scenario “${cfg.scenario}” · user ${cfg.user}`
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

  /** A clicked option goes to the agent as its number but is shown as what was picked. */
  function pick(t: string) {
    const label = /^\d+$/.test(t) ? snap?.option_labels?.[Number(t) - 1] : undefined;
    return send(t, label);
  }

  const pendingApproval = snap && !snap.crashed && snap.stage === "awaiting_approval" && snap.approval?.status === "pending"
    ? snap.approval : null;
  const lastAgent = [...msgs].reverse().find((m) => m.kind === "agent");
  const closed = !!snap?.closed;
  const live = !!snap && !snap.crashed && !closed && !busy && ["choosing", "collecting"].includes(snap.stage);
  // Tables and numbered lists have their own clickable options; anything else in "choosing" gets chips here.
  const choices = snap && !snap.crashed && snap.stage === "choosing" && !(lastAgent && hasOptions(lastAgent.text))
    ? Array.from({ length: Math.min(snap.options, 20) }, (_, i) => String(i + 1)) : [];

  return (
    <>
      <header>
        <h1>🧰 Errand Agent</h1>
        <span className="city" title="The only city for now">📍 Bangalore</span>
        {DEV && <>
          <span className={"pill stage-" + (snap?.crashed ? "crashed" : snap?.stage ?? "idle")}>
            {snap?.crashed ? "💥 process dead" : snap?.stage ?? "…"}</span>
          <span className="pill mono">🕒 {snap?.now ?? "…"}</span>
          <label>Scenario
            <select value={form.scenario} onChange={(e) => setForm({ ...form, scenario: e.target.value })}>
              {(snap?.scenarios ?? [form.scenario]).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label>User <input type="text" size={4} value={form.user} onChange={(e) => setForm({ ...form, user: e.target.value })} /></label>
          <button disabled={busy} onClick={() => control("/api/advance", { minutes: 15 })}>⏩ +15 min</button>
          <button className="danger" disabled={busy} onClick={() => control("/api/restart")}>💥 Restart process</button>
        </>}
        <span className="spacer" />
        <button disabled={busy} onClick={() => { setActive(null); newSession({ clock: "real" }); }}>New chat</button>
        <button className={panel === "profile" ? "on" : ""} onClick={() => toggle("profile")}>Profile</button>
        <button className={panel === "details" ? "on" : ""} onClick={() => toggle("details")}>
          Details {panel === "details" ? "▸" : "◂"}</button>
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
                {m.kind === "agent"
                  ? <MessageBody text={m.text} live={live && m === lastAgent} suggestions={snap?.suggestions ?? []}
                                 onPick={(t) => pick(t)} />
                  : m.text}
              </div>
            ))}
            {busy && <div className="typing">…</div>}
            {pendingApproval && !busy &&
              <ApprovalCard ap={pendingApproval} service={snap?.service ?? null} busy={busy}
                            onYes={() => send("yes", "Yes, confirm")} onNo={() => send("no", "No, cancel")} />}
          </div>
          {(choices.length > 0 || snap?.crashed || closed) && (
            <div className="quick">
              {snap?.crashed && <button className="danger" disabled={busy} onClick={() => control("/api/restart")}>Restart process</button>}
              {closed && <button className="primary" disabled={busy}
                                 onClick={() => { setActive(null); newSession({ clock: "real" }); }}>Start a New chat</button>}
              {choices.map((c) => <button key={c} disabled={busy} onClick={() => pick(c)}>
                {snap?.option_labels?.[Number(c) - 1] ?? `Option ${c}`}</button>)}
            </div>
          )}
          <div className="composer">
            <textarea ref={inputRef} value={input} autoFocus disabled={closed}
                      placeholder={closed ? "This chat is closed. Start a New chat to book a service." : "Type a message…"}
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
            <button className="primary" disabled={busy || closed || !input.trim()} onClick={submit}>Send</button>
          </div>
          {input.length > MAX_CHARS && <div className="counter over">{input.length} / {MAX_CHARS} characters</div>}
        </section>

        {panel === "profile" && (
          <aside className="drawer">
            <div className="pane">
              {snap ? <ProfilePane s={snap} /> : <div className="muted">Loading…</div>}
            </div>
          </aside>
        )}
        {panel === "details" && (
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
