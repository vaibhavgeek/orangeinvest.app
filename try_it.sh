#!/usr/bin/env bash
# Try the Errand Agent.
#   ./try_it.sh                 run the tests, open the eval table, then open chat + live trace windows
#   ./try_it.sh test            only run the tests and open the eval table
#   ./try_it.sh chat [SCENARIO] only open chat + live trace (default scenario: timeout_after_success)
# Scenarios: default timeout_after_success timeout_not_booked slot_taken price_change crash_after_book
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
MODE="${1:-all}"
SCENARIO="${2:-timeout_after_success}"

run_tests() {
  "$PY" -c "import pytest" 2>/dev/null || "$PY" -m pip install -q -r requirements.txt
  echo "▶ Running tests…"
  "$PY" -m pytest -v
  echo "✔ Eval table: docs/EVAL_RESULTS.md"
  [[ "$(uname)" == "Darwin" ]] && open "$ROOT/docs/EVAL_RESULTS.md" || true
}

run_chat() {
  rm -rf "$ROOT/data" "$ROOT/traces/demo.jsonl"
  mkdir -p "$ROOT/traces"
  if [[ "$(uname)" == "Darwin" ]]; then
    osascript <<EOF
tell application "Terminal"
  activate
  do script "cd '$ROOT' && clear && echo '── LIVE TRACE ──' && $PY -m errand_agent.tracefmt --follow traces/demo.jsonl"
  do script "cd '$ROOT' && clear && $PY -m errand_agent.cli --scenario $SCENARIO"
end tell
EOF
    echo "✔ Opened two Terminal windows: chat (scenario: $SCENARIO) and live trace."
    echo "  Try: Find a plumber for my kitchen tap this week, mornings only, under ₹1,500  →  Indiranagar  →  1  →  yes"
  else
    echo "Open a second terminal and run: $PY -m errand_agent.tracefmt --follow traces/demo.jsonl"
    "$PY" -m errand_agent.cli --scenario "$SCENARIO"
  fi
}

case "$MODE" in
  test) run_tests ;;
  chat) run_chat ;;
  all)  run_tests; run_chat ;;
  *) echo "usage: $0 [all|test|chat [scenario]]"; exit 1 ;;
esac
