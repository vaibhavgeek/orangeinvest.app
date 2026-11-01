import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Playbook, type Reply, type Snapshot, type TraceEvent } from "./api";
import {
  type Active, BookingsPane, expandSteps, expectText, MemoryPane, PlaybooksPane, StatePane, TracePane, verdict,
} from "./panels";

type Msg = { id: number; kind: "user" | "agent" | "system"; text: string; meta?: string };
type Tab = "playbooks" | "trace" | "state" | "memory" | "bookings";
const PARSER_LABELS: Record<string, string> = { rules: "Rule-based (offline)", openai: "OpenAI", anthropic: "Anthropic" };
const MAX_CHARS = 1000;

let msgId = 0;

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<Tab>("playbooks");
  const [input, setInput] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [active, setActiveState] = useState<Active | null>(null);
  const [form, setForm] = useState({ scenario: "default", parser: "rules", user: "u1", clock: "real" });

  const since = useRef(0);
  const generation = useRef(0); // bumps on every new session, so stale polls are discarded
  const busyRef = useRef(false);
  const activeRef = useRef<Active | null>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const refreshChain = useRef<Promise<Snapshot | null>>(Promise.resolve(null));

  const setActive = (a: Active | null) => { activeRef.current = a; setActiveState(a ? { ...a } : null); };
  const setBusyBoth = (b: boolean) => { busyRef.current = b; setBusy(b); };
  const say = (kind: Msg["kind"], text: string, meta?: string) => setMsgs((m) => [...m, { id: ++msgId, kind, text, meta }]);
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
    api<Playbook[]>("/api/playbooks").then(setPlaybooks).catch((e) => showToast(e.message));
    refresh().then((s) => {
      if (s) {
        setForm(s.config);
        say("system", "Connected. Type a request, or pick a test playbook on the right. The Trace tab shows every model call, "
          + "tool call, state change and approval.");
      } else say("system", "Can't reach the backend. Start it with ./try_it.sh web.");
    });
    window.setInterval(() => { if (!busyRef.current) refresh(); }, 2500);
  }, [refresh]);

  useEffect(() => { chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight }); }, [msgs, busy]);

  async function send(text: string): Promise<string | null> {
    if (busyRef.current || !text.trim()) return null;
    say("user", text.length > 400 ? `${text.slice(0, 400)}… (${text.length} chars)` : text);
    setBusyBoth(true);
    const t0 = performance.now();
    let reply: string | null = null;
    try {
      const r = await api<Reply>("/api/message", { text });
      reply = r.reply;
      if (r.reply) say(r.system ? "system" : "agent", r.reply, r.system ? undefined : `${((performance.now() - t0) / 1000).toFixed(1)}s`);
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
    say("system", `New session · scenario “${cfg.scenario}” · parser ${cfg.parser} · user ${cfg.user}`
      + `${cfg.clock === "fixed" ? " · clock fixed at Sun 01 Nov 2026 08:00 (same as the offline tests)" : ""}. `
      + "Provider world, memory and trace wiped.");
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

  const stageCls = snap?.crashed ? "crashed" : snap?.stage ?? "idle";
  const quick: [string, string][] = !snap || snap.crashed ? [] : snap.stage === "awaiting_approval" ? [["yes", "yes"], ["no", "no"]]
    : snap.stage === "choosing" ? Array.from({ length: Math.min(snap.options, 9) }, (_, i) => [String(i + 1), String(i + 1)]) : [];

  return (
    <>
      <header>
        <h1>🧰 Errand Agent</h1>
        <span className={"pill stage-" + stageCls}>{snap?.crashed ? "💥 process dead" : snap?.stage ?? "…"}</span>
        <span className="pill mono" title="Agent clock (moves with ⏩)">🕒 {snap?.now ?? "…"}</span>
        <span className="pill mono" title="Parser in use">{snap?.model ?? "…"}</span>
        <span className="spacer" />
        <label>Scenario
          <select value={form.scenario} onChange={(e) => setForm({ ...form, scenario: e.target.value })}
                  title="Failure mode injected into the mock provider">
            {(snap?.scenarios ?? [form.scenario]).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>Parser
          <select value={form.parser} onChange={(e) => setForm({ ...form, parser: e.target.value })}>
            {(snap?.parsers ?? ["rules"]).map((p) => <option key={p} value={p}>{PARSER_LABELS[p] ?? p}</option>)}
          </select>
        </label>
        <label>User <input type="text" size={4} value={form.user} onChange={(e) => setForm({ ...form, user: e.target.value })}
                           title="Memory and bookings are per user" /></label>
        <button className="primary" disabled={busy} onClick={() => { setActive(null); newSession({ clock: "real" }); }}
                title="Wipe data and start a fresh session with these settings">New session</button>
        <button disabled={busy} onClick={() => control("/api/advance", { minutes: 15 })}
                title="Move the agent clock forward 15 minutes (holds last 10)">⏩ +15 min</button>
        <button className="danger" disabled={busy} onClick={() => control("/api/restart")}
                title="Kill and restart the agent process; state is reloaded from disk">💥 Restart process</button>
      </header>

      <main>
        <section className="chatcol">
          <div className="chat" ref={chatRef}>
            {msgs.map((m) => {
              const cls = ["msg", m.kind, m.kind === "agent" && m.text.includes("Please confirm") ? "approval" : "",
                m.kind === "agent" && m.text.includes("✅") ? "ok" : ""].join(" ");
              return <div key={m.id} className={cls}>{m.text}{m.meta && <span className="meta">{m.meta}</span>}</div>;
            })}
            {busy && <div className="typing">agent is working…</div>}
          </div>
          <div className="quick">
            {snap?.crashed && <button className="danger" disabled={busy} onClick={() => control("/api/restart")}>💥 Restart process</button>}
            {quick.map(([label, text]) => <button key={label} disabled={busy} onClick={() => send(text)}>{label}</button>)}
          </div>
          <div className="composer">
            <textarea ref={inputRef} value={input} autoFocus
                      placeholder="Try: Find a plumber for my kitchen tap in Indiranagar this week, mornings only, under ₹1,500"
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
            <div className="side">
              <button className="primary" disabled={busy} onClick={submit}>Send</button>
              <span className={"counter" + (input.length > MAX_CHARS ? " over" : "")}>
                {input.length > MAX_CHARS ? `${input.length} ⚠︎ >${MAX_CHARS}` : input.length}
              </span>
            </div>
          </div>
        </section>

        <section className="sidecol">
          <div className="tabs">
            {(["playbooks", "trace", "state", "memory", "bookings"] as Tab[]).map((t) => (
              <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
                {{ playbooks: "Test playbooks", trace: "Trace", state: "State", memory: "Memory", bookings: "Provider bookings" }[t]}
                {t === "trace" && events.length > 0 && <span className="badge">({events.length})</span>}
              </button>
            ))}
          </div>
          <div className="pane">
            {tab === "playbooks" && <PlaybooksPane playbooks={playbooks} active={active} busy={busy} onStart={startPlaybook}
              onStep={stepPlaybook} onPick={(t) => { setInput(t); inputRef.current?.focus(); }} />}
            {tab === "trace" && <TracePane events={events} />}
            {snap && tab === "state" && <StatePane s={snap} />}
            {snap && tab === "memory" && <MemoryPane s={snap} />}
            {snap && tab === "bookings" && <BookingsPane bookings={snap.bookings} />}
          </div>
        </section>
      </main>
      {toast && <div className="toast">{toast}</div>}
    </>
  );
}
