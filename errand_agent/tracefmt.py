"""Pretty-print a JSONL trace, optionally following it live:  python -m errand_agent.tracefmt --follow traces/demo.jsonl"""
import argparse
import json
import os
import time

COLORS = {"tool_call": "36", "state_transition": "35", "approval": "33", "approval_blocked": "31",
          "model_call": "34", "error": "31", "limit_exceeded": "31", "recovery": "32"}


def line(e: dict) -> str | None:
    t = e["type"]
    if t == "user_message":
        s = f"USER   {e['text']}"
    elif t == "agent_reply":
        s = f"AGENT  {e['text'].splitlines()[0][:110]}"
    elif t == "tool_call":
        res = f"ERROR {e['error']}" if e.get("error") else json.dumps(e.get("result"), ensure_ascii=False)[:110]
        s = f"TOOL   {e['tool']}({json.dumps(e['args'], ensure_ascii=False)[:90]}) -> {res}  [{e['latency_ms']}ms]"
    elif t == "state_transition":
        s = f"STATE  {e['from']} -> {e['to']}  ({e['reason']})"
    elif t == "approval":
        extra = {k: e[k] for k in ("action", "provider_id", "slot", "price", "reason", "booking_id") if e.get(k)}
        s = f"APPROVAL {e['event']} {e['approval_id']} {json.dumps(extra, ensure_ascii=False)}"
    elif t == "model_call":
        s = (f"MODEL  {e['model']} in={e['input_tokens']} out={e['output_tokens']} ${e['cost_usd']} "
             f"-> {json.dumps(e['output'], ensure_ascii=False)[:90]}")
        if e.get("dropped_fields"):
            s += f"  DROPPED {e['dropped_fields']}"
    else:
        s = f"{t.upper():6} " + json.dumps({k: v for k, v in e.items() if k not in ("seq", "ts", "session_id", "turn", "type")},
                                           ensure_ascii=False)[:120]
    return f"\x1b[{COLORS.get(t, '0')}m[t{e['turn']}] {s}\x1b[0m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--follow", action="store_true")
    a = ap.parse_args()
    while a.follow and not os.path.exists(a.path):
        time.sleep(0.3)
    with open(a.path) as f:
        while True:
            row = f.readline()
            if row:
                print(line(json.loads(row)), flush=True)
            elif a.follow:
                time.sleep(0.2)
            else:
                break


if __name__ == "__main__":
    main()
