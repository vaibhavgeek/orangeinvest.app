"""Interactive chat. Run:  python -m errand_agent.cli --scenario timeout_after_success
Watch the trace in a second pane:  python -m errand_agent.tracefmt --follow traces/demo.jsonl
"""
import argparse
import os
import shutil
import sys

from .agent import Agent
from .budget import Limits
from .clock import SystemClock
from .mock_api import MockProviderAPI, Scenario, SimulatedCrash

SCENARIOS = {
    "default": dict(),
    "timeout_after_success": dict(book_outcomes=["timeout_booked"]),
    "timeout_not_booked": dict(book_outcomes=["timeout_not_booked", "success"]),
    "slot_taken": dict(book_outcomes=["slot_taken", "success"]),
    "price_change": dict(quote_deltas=[250, 0]),
    "crash_after_book": dict(crash_after_book=True, book_outcomes=["success"]),
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="default", choices=sorted(SCENARIOS))
    ap.add_argument("--user", default="u1")
    ap.add_argument("--session", default="demo")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--trace-dir", default="traces")
    ap.add_argument("--fresh", action="store_true", help="wipe saved sessions, memory, provider world and traces")
    ap.add_argument("--llm", action="store_true", help="use Anthropic for parsing (needs ANTHROPIC_API_KEY)")
    a = ap.parse_args(argv)

    if a.fresh:
        shutil.rmtree(a.data_dir, ignore_errors=True)
        trace = os.path.join(a.trace_dir, f"{a.session}.jsonl")
        if os.path.exists(trace):
            os.remove(trace)
    os.makedirs(a.data_dir, exist_ok=True)

    model = None
    if a.llm:
        from .nlu import LLMModel
        model = LLMModel(os.environ["ANTHROPIC_API_KEY"])
    world = os.path.join(a.data_dir, "mock_world.json")
    scenario = Scenario(**SCENARIOS[a.scenario])
    if os.path.exists(world):
        scenario.crash_after_book = False  # crash once; a restarted process should recover, not crash again
    clock = SystemClock()
    api = MockProviderAPI(clock=clock, scenario=scenario, user_id=a.user, path=world)
    agent = Agent(api, user_id=a.user, session_id=a.session, data_dir=a.data_dir, model=model, clock=clock,
                  limits=Limits(), trace_dir=a.trace_dir)
    print(f"Errand agent — scenario '{a.scenario}', user {a.user}. Type 'quit' to exit.")
    try:
        note = agent.resume()
        if note:
            print(f"\nagent> {note}")
        while True:
            try:
                text = input("\nyou> ").strip()
            except EOFError:
                break
            if text.lower() in ("quit", "exit"):
                break
            if text:
                print(f"\nagent> {agent.handle(text)}")
    except SimulatedCrash:
        print("\n💥 Simulated crash: the agent process died right after the provider accepted the booking.\n"
              "Run the same command again (without --fresh) to restart and watch it recover.")
        sys.exit(1)


if __name__ == "__main__":
    main()
