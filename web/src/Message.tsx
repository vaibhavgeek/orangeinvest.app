import type { ReactElement } from "react";

// Renders an agent reply: plain text, markdown tables (numbered "[n]" slots become buttons) and the
// "Or say: a · b" line (one-click changes, handled by the backend without a model call).

const SLOT = /\[(\d{1,2})\]\s*([^·]*)/g;

// Markdown links become clickable only for trusted hosts: replies can quote provider reviews, which must not
// be able to plant links.
const LINK = /\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g;
const LINK_HOSTS = new Set(["www.vaibhavgeek.tech", "vaibhavgeek.tech"]);

function Linked({ text }: { text: string }) {
  const out: (string | ReactElement)[] = [];
  let last = 0;
  for (const m of text.matchAll(LINK)) {
    let host = "";
    try { host = new URL(m[2]).hostname; } catch { /* not a URL */ }
    if (!LINK_HOSTS.has(host)) continue;
    out.push(text.slice(last, m.index), <a key={m.index} href={m[2]} target="_blank" rel="noopener noreferrer">{m[1]}</a>);
    last = m.index! + m[0].length;
  }
  out.push(text.slice(last));
  return <>{out}</>;
}

function Cell({ text, live, onPick }: { text: string; live: boolean; onPick: (t: string) => void }) {
  const parts = [...text.matchAll(SLOT)];
  if (!parts.length) return <>{text}</>;
  return (
    <span className="slots">
      {parts.map((m) => live
        ? <button key={m[1]} className="slot" onClick={() => onPick(m[1])}>{m[2].trim() || `Option ${m[1]}`}</button>
        : <span key={m[1]} className="slot off">{m[2].trim()}</span>)}
    </span>
  );
}

function Table({ rows, live, onPick }: { rows: string[]; live: boolean; onPick: (t: string) => void }) {
  const split = (r: string) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const [head, ...body] = rows.filter((r) => !/^\|\s*-/.test(r)).map(split);
  return (
    <div className="tablewrap">
      <table>
        <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}><Cell text={c} live={live} onPick={onPick} /></td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function hasTable(text: string) {
  return text.split("\n").some((l) => l.startsWith("|"));
}

const ITEM = /^(\d{1,2})\. (.+)$/;

/** Has clickable options of its own (a table with slots or a numbered list). */
export function hasOptions(text: string) {
  return hasTable(text) || text.split("\n").some((l) => ITEM.test(l));
}

function Options({ items, live, onPick }: { items: [string, string][]; live: boolean; onPick: (t: string) => void }) {
  return (
    <div className="optlist">
      {items.map(([n, label]) => live
        ? <button key={n} className="opt" onClick={() => onPick(n)}>{label}</button>
        : <span key={n} className="opt off">{label}</span>)}
    </div>
  );
}

export default function MessageBody({ text, live, suggestions, onPick }: {
  text: string; live: boolean; suggestions: string[]; onPick: (t: string) => void;
}) {
  const lines = text.split("\n");
  const out: ReactElement[] = [];
  let para: string[] = [];
  const flush = () => { if (para.length) out.push(<div key={out.length} className="para"><Linked text={para.join("\n")} /></div>); para = []; };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("|")) {
      flush();
      const rows: string[] = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      i--;
      out.push(<Table key={out.length} rows={rows} live={live} onPick={onPick} />);
    } else if (ITEM.test(l)) {
      flush();
      const items: [string, string][] = [];
      for (; i < lines.length && ITEM.test(lines[i]); i++) {
        const m = lines[i].match(ITEM)!;
        items.push([m[1], m[2]]);
      }
      i--;
      out.push(<Options key={out.length} items={items} live={live} onPick={onPick} />);
    } else if (l.startsWith("Or say: ")) {
      flush();
      const labels = l.slice(8).split(" · ");
      out.push(
        <div key={out.length} className="sugs">
          {labels.map((s) => (live && suggestions.includes(s))
            ? <button key={s} className="chip" onClick={() => onPick(s)}>{s}</button>
            : <span key={s} className="chip off">{s}</span>)}
        </div>,
      );
    } else para.push(l);
  }
  flush();
  return <>{out}</>;
}
