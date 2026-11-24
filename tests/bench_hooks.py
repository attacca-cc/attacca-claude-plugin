"""Measure the two read hooks end to end against a stub server.

    python3 tests/bench_hooks.py --runs 30

The unit tests check behaviour with an in-process stub client. This measures the
thing a user feels: one hook invocation the way Claude Code runs it -- a fresh
interpreter, the event on stdin, the rendered block on stdout -- against an HTTP
server that answers the way Attacca does, including the ~150 ms the recall
engine takes at the median.

Budgets (README, "What it does"): SessionStart 2 s, UserPromptSubmit 4 s.
The script exits 0 only when the p95 of both holds and an account with no
memory produces no output at all.

CI runs this on a stock ubuntu-latest runner (`.github/workflows/ci.yml`, job
`bench`), which is the machine the numbers in the README are quoted from.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "attacca_memory.py"

SESSIONSTART_BUDGET_MS = 2000
PROMPT_BUDGET_MS = 4000

# The blocks the server renders, at the sizes production measured (README:
# profile 738 tokens, per-prompt 470 tokens at the median, ~2.5 chars/token).
PROFILE_TARGET_CHARS = 1845
PROMPT_TARGET_CHARS = 1180

PROMPT = "Why did we decide the plugin keeps its own repository instead of living in the main one?"


def build_block(seed, target_chars):
    """A rendered block of roughly the size the server sends, shaped like one."""
    lines = ["Attacca memory -- what this account already settled", ""]
    size = sum(len(line) + 1 for line in lines)
    index = 0
    while size < target_chars:
        line = (
            f"- {seed}-{index} [project]: the memory read surface keeps one"
            f" decision per line, and a node dropped by the budget is not"
            f" marked injected ({index})"
        )
        lines.append(line)
        size += len(line) + 1
        index += 1
    return "\n".join(lines)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            event = {}
        time.sleep(self.server.delay)
        rendered = self.server.block_for(event)
        payload = {
            "rendered": rendered,
            "nodes": [{"name": "attacca-memory-0"}, {"name": "attacca-memory-1"}],
            "diagnostics": {"why": "ok", "elapsed_ms": int(self.server.delay * 1000)},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence the stub
        pass


def start_stub(delay, block_for):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.delay = delay
    server.block_for = block_for
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/api"


def child_env(base_url, data_dir):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ATTACCA_")}
    env["CLAUDE_PLUGIN_OPTION_ATTACCA_URL"] = base_url
    env["CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN"] = "atk_bench"
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env.pop("CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED", None)
    return env


def run_hook(event, env, timeout=60):
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return elapsed_ms, completed


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def summarise(label, samples, budget_ms):
    p50 = percentile(samples, 0.50)
    p95 = percentile(samples, 0.95)
    worst = max(samples)
    return {
        "label": label,
        "n": len(samples),
        "p50": p50,
        "p95": p95,
        "max": worst,
        "budget": budget_ms,
        "ok": p95 <= budget_ms,
    }

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="hook invocations per event")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="seconds the stub spends before answering (the recall engine's p50 is ~0.17 s)",
    )
    parser.add_argument("--stamp", default="", help="suffix for the session ids, to defeat the recall ledger")
    args = parser.parse_args(argv)

    if not SCRIPT.exists():
        print(f"hook script not found: {SCRIPT}", file=sys.stderr)
        return 2

    block = {"start": build_block("attacca-memory", PROFILE_TARGET_CHARS),
             "prompt": build_block("attacca-memory", PROMPT_TARGET_CHARS)}

    def block_for(event, _block=block):
        # SessionStart asks with the profile budget (1,200), a prompt with 800.
        try:
            budget = int(event.get("budget_tokens") or 0)
        except (TypeError, ValueError):
            budget = 0
        return _block["start"] if budget >= 1000 else _block["prompt"]

    server, base_url = start_stub(args.delay, block_for)
    stamp = args.stamp or str(int(time.time()))
    rows = []
    failures = []

    with tempfile.TemporaryDirectory(prefix="attacca-bench-") as work:
        project = Path(work) / "project"
        project.mkdir()
        data_dir = str(Path(work) / "data")
        env = child_env(base_url, data_dir)

        try:
            for event_name, label, budget, make_event in (
                (
                    "SessionStart",
                    "SessionStart hook wall",
                    SESSIONSTART_BUDGET_MS,
                    lambda i: {
                        "session_id": f"bench-start-{stamp}-{i}",
                        "hook_event_name": "SessionStart",
                        "cwd": str(project),
                    },
                ),
                (
                    "UserPromptSubmit",
                    "UserPromptSubmit hook wall",
                    PROMPT_BUDGET_MS,
                    lambda i: {
                        "session_id": f"bench-prompt-{stamp}-{i}",
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": PROMPT,
                        "cwd": str(project),
                    },
                ),
            ):
                samples = []
                for i in range(args.runs):
                    elapsed_ms, completed = run_hook(make_event(i), env)
                    if completed.returncode != 0:
                        failures.append(
                            f"{event_name} run {i} exited {completed.returncode}: {completed.stderr.strip()[:400]}"
                        )
                    if not completed.stdout.strip():
                        failures.append(f"{event_name} run {i} injected nothing")
                    samples.append(elapsed_ms)
                rows.append(summarise(label, samples, budget))

            # An account with no memory: the server renders nothing, so the hook
            # must print nothing and exit 0.
            empty_server, empty_url = start_stub(args.delay, lambda event: "")
            try:
                empty_env = child_env(empty_url, str(Path(work) / "data-empty"))
                for event_name, event in (
                    (
                        "SessionStart",
                        {
                            "session_id": f"bench-empty-start-{stamp}",
                            "hook_event_name": "SessionStart",
                            "cwd": str(project),
                        },
                    ),
                    (
                        "UserPromptSubmit",
                        {
                            "session_id": f"bench-empty-prompt-{stamp}",
                            "hook_event_name": "UserPromptSubmit",
                            "prompt": PROMPT,
                            "cwd": str(project),
                        },
                    ),
                ):
                    _, completed = run_hook(event, empty_env)
                    if completed.returncode != 0:
                        failures.append(f"empty-memory {event_name} exited {completed.returncode}")
                    if completed.stdout.strip():
                        failures.append(
                            f"empty-memory {event_name} printed something: {completed.stdout[:200]!r}"
                        )
            finally:
                empty_server.shutdown()
        finally:
            server.shutdown()

    host = f"{platform.system()} {platform.release()} / Python {platform.python_version()}"
    print(f"stub delay {int(args.delay * 1000)} ms per recall; {host}")
    print("")
    print("| Measurement | n | p50 | p95 | max | Budget | Verdict |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        verdict = "met" if row["ok"] else "MISSED"
        print(
            f"| {row['label']} | {row['n']} | {row['p50']:.0f} ms | {row['p95']:.0f} ms "
            f"| {row['max']:.0f} ms | {row['budget']} ms | {verdict} |"
        )
    empty_note = "quiet on an empty account" if not any("empty-memory" in f for f in failures) else "NOT quiet on an empty account"
    print("")
    print(f"Empty-memory check: {empty_note}.")

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)

    start_row = rows[0] if rows else {"p50": 0, "p95": 0}
    prompt_row = rows[1] if len(rows) > 1 else {"p50": 0, "p95": 0}
    print(
        "SUMMARY sessionstart_p50_ms={:.0f} sessionstart_p95_ms={:.0f} "
        "prompt_p50_ms={:.0f} prompt_p95_ms={:.0f} runs={}".format(
            start_row["p50"], start_row["p95"], prompt_row["p50"], prompt_row["p95"], args.runs
        )
    )

    ok = all(row["ok"] for row in rows) and not failures
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
