"""Unit tests for hooks/attacca_memory.py.

Run with:  python3 -m unittest discover -s tests -v
No network: every HTTP call goes through a stub client.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "attacca_memory.py"

_spec = importlib.util.spec_from_file_location("attacca_memory", SCRIPT)
am = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules[cls.__module__], so the
# module has to be registered before it executes.
sys.modules["attacca_memory"] = am
_spec.loader.exec_module(am)


class StubClient:
    """Stands in for HttpRecallClient: returns, raises, or stalls on demand."""

    def __init__(self, response=None, error=None, delay=0.0):
        self.response = {} if response is None else response
        self.error = error
        self.delay = delay
        self.calls = []

    def recall(self, session_id, message, budget_tokens, timeout=10.0):
        self.calls.append(
            {
                "session_id": session_id,
                "message": message,
                "budget_tokens": budget_tokens,
                "timeout": timeout,
            }
        )
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response


def make_config(tmpdir, **overrides):
    values = dict(token="atk_test", state_dir=tmpdir)
    values.update(overrides)
    return am.Config(**values)


def prompt_event(prompt, session_id="abc-123"):
    return {
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": "/tmp/project",
    }


LONG_PROMPT = "Why did we decide to keep the plugin in its own repository?"


class SkipRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)
        self.state = am.SessionState()
        self.now = 1_000_000.0

    def test_short_prompt_is_skipped(self):
        self.assertEqual(
            am.skip_reason("yes", self.config, self.state, self.now), "too_short"
        )
        self.assertEqual(
            am.skip_reason("x" * (am.MIN_PROMPT_CHARS - 1), self.config, self.state, self.now),
            "too_short",
        )

    def test_prompt_at_the_threshold_is_not_skipped(self):
        self.assertEqual(
            am.skip_reason("x" * am.MIN_PROMPT_CHARS, self.config, self.state, self.now), ""
        )

    def test_slash_command_is_skipped(self):
        self.assertEqual(
            am.skip_reason("/attacca:recall what do you know", self.config, self.state, self.now),
            "slash_command",
        )

    def test_empty_prompt_is_skipped(self):
        self.assertEqual(
            am.skip_reason("   \n\t ", self.config, self.state, self.now), "empty_prompt"
        )

    def test_recall_on_prompt_false_is_skipped(self):
        config = make_config(self.tmp.name, recall_on_prompt=False)
        self.assertEqual(
            am.skip_reason(LONG_PROMPT, config, self.state, self.now),
            "recall_on_prompt=false",
        )

    def test_missing_token_is_skipped(self):
        config = make_config(self.tmp.name, token="")
        self.assertEqual(
            am.skip_reason(LONG_PROMPT, config, self.state, self.now), "no_token"
        )

    def test_circuit_breaker_opens_for_sixty_seconds(self):
        state = am.SessionState(last_failure_at=self.now - 10)
        self.assertEqual(
            am.skip_reason(LONG_PROMPT, self.config, state, self.now), "circuit_open"
        )
        state = am.SessionState(last_failure_at=self.now - 61)
        self.assertEqual(am.skip_reason(LONG_PROMPT, self.config, state, self.now), "")

    def test_skipped_prompt_never_calls_the_server(self):
        client = StubClient()
        out = am.handle_prompt_submit(
            prompt_event("ok"), self.config, client, now=self.now
        )
        self.assertIsNone(out)
        self.assertEqual(client.calls, [])

    def test_slash_command_never_calls_the_server(self):
        client = StubClient()
        am.handle_prompt_submit(
            prompt_event("/attacca:status please show me the memory graph"),
            self.config,
            client,
            now=self.now,
        )
        self.assertEqual(client.calls, [])


class TruncationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)

    def test_truncate_context_caps_at_ten_thousand_characters(self):
        text = "x" * 20_000
        trimmed = am.truncate_context(text)
        self.assertEqual(len(trimmed), am.MAX_CONTEXT_CHARS)
        self.assertTrue(trimmed.endswith(am.TRUNCATION_MARKER))

    def test_short_text_is_untouched(self):
        self.assertEqual(am.truncate_context("short"), "short")

    def test_hook_output_is_capped(self):
        client = StubClient(response={"rendered": "y" * 50_000, "nodes": []})
        out = am.handle_session_start(
            {"session_id": "s1", "hook_event_name": "SessionStart"},
            self.config,
            client,
        )
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(len(context), am.MAX_CONTEXT_CHARS)
        self.assertTrue(context.endswith(am.TRUNCATION_MARKER))


class DeadlineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name, prompt_deadline_secs=0.2)

    def test_slow_server_is_abandoned_within_the_deadline(self):
        client = StubClient(response={"rendered": "late"}, delay=3.0)
        started = time.time()
        out = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        elapsed = time.time() - started
        self.assertIsNone(out)
        self.assertLess(elapsed, 1.5, f"deadline not enforced ({elapsed:.2f}s)")

    def test_call_with_deadline_raises_deadline_exceeded(self):
        with self.assertRaises(am.DeadlineExceeded):
            am.call_with_deadline(lambda: time.sleep(2.0), 0.1)

    def test_deadline_failure_leaves_a_circuit_breaker_mark(self):
        client = StubClient(error=am.DeadlineExceeded("slow"))
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client, now=100.0)
        state = am.load_state(self.config, "cc:abc-123")
        self.assertEqual(state.last_failure_at, 100.0)


class FailOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)

    def test_network_error_returns_nothing(self):
        client = StubClient(error=urllib.error.URLError("connection refused"))
        self.assertIsNone(am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client))
        self.assertIsNone(
            am.handle_session_start({"session_id": "s1"}, self.config, client)
        )

    def test_http_503_returns_nothing(self):
        error = urllib.error.HTTPError(
            "https://attacca.cc/v1/memory/recall", 503, "busy", {}, None
        )
        client = StubClient(error=error)
        self.assertIsNone(am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client))

    def test_bad_payload_returns_nothing(self):
        client = StubClient(error=ValueError("not json"))
        self.assertIsNone(am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client))

    def test_empty_rendered_returns_nothing(self):
        client = StubClient(response={"rendered": "   ", "nodes": []})
        self.assertIsNone(am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client))

    def test_main_is_quiet_with_unparseable_input(self):
        import io

        self.assertEqual(am.main([], stdin=io.StringIO("not json at all")), 0)

    def test_script_exits_zero_with_empty_output_when_the_server_is_down(self):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "CLAUDE_PLUGIN_OPTION_ATTACCA_URL": "http://127.0.0.1:9",
            "CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN": "atk_test",
            "CLAUDE_PLUGIN_DATA": self.tmp.name,
        }
        event = json.dumps(prompt_event(LONG_PROMPT))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "prompt-submit"],
            input=event,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        session_start = subprocess.run(
            [sys.executable, str(SCRIPT), "session-start"],
            input=json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(session_start.returncode, 0)
        self.assertEqual(session_start.stdout, "")


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name, circuit_breaker_secs=60.0)

    def test_second_prompt_after_a_failure_does_not_call_the_server(self):
        client = StubClient(error=urllib.error.URLError("down"))
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client, now=500.0)
        self.assertEqual(len(client.calls), 1)
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client, now=520.0)
        self.assertEqual(len(client.calls), 1, "circuit breaker did not hold")

    def test_circuit_closes_after_the_window(self):
        client = StubClient(error=urllib.error.URLError("down"))
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client, now=500.0)
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client, now=561.0)
        self.assertEqual(len(client.calls), 2)

    def test_success_clears_the_breaker(self):
        failing = StubClient(error=urllib.error.URLError("down"))
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, failing, now=500.0)
        working = StubClient(response={"rendered": "a memory", "nodes": []})
        out = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, working, now=700.0)
        self.assertIsNotNone(out)
        state = am.load_state(self.config, "cc:abc-123")
        self.assertEqual(state.last_failure_at, 0.0)


class SessionStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)

    def test_output_shape_and_request(self):
        client = StubClient(response={"rendered": "Profile: Allen.", "nodes": []})
        out = am.handle_session_start(
            {"session_id": "sess-1", "hook_event_name": "SessionStart", "source": "startup"},
            self.config,
            client,
        )
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "Profile: Allen.")
        call = client.calls[0]
        self.assertEqual(call["session_id"], "cc:sess-1")
        self.assertEqual(call["message"], am.PROFILE_QUERY)
        self.assertEqual(call["budget_tokens"], 1200)

    def test_prompt_budget_defaults_to_eight_hundred(self):
        client = StubClient(response={"rendered": "x", "nodes": []})
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        self.assertEqual(client.calls[0]["budget_tokens"], 800)

    def test_hook_event_name_is_enough_without_argv(self):
        # No argv, event name only: main() must dispatch on hook_event_name.
        self.assertEqual(am._mode([], {"hook_event_name": "UserPromptSubmit"}), "UserPromptSubmit")
        self.assertEqual(am._mode(["session-start"], {}), "session-start")


class AlreadyInjectedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)

    def test_the_same_node_is_not_injected_twice(self):
        response = {"rendered": "About A.", "nodes": [{"id": 1, "name": "A", "kind": "entity"}]}
        client = StubClient(response=response)
        first = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        self.assertIsNotNone(first)
        second = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        self.assertIsNone(second)

    def test_a_new_node_is_injected(self):
        client = StubClient(
            response={"rendered": "About A.", "nodes": [{"id": 1, "name": "A", "kind": "entity"}]}
        )
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        client.response = {
            "rendered": "About B.",
            "nodes": [{"id": 2, "name": "B", "kind": "entity"}],
        }
        out = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "About B.")

    def test_a_claim_keeps_the_answer_even_when_nodes_are_known(self):
        client = StubClient(
            response={"rendered": "About A.", "nodes": [{"id": 1, "name": "A", "kind": "entity"}]}
        )
        am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        client.response = {
            "rendered": "A decision.",
            "nodes": [{"id": 1, "name": "A", "kind": "entity"}],
            "claims": [{"key": "d1", "kind": "decision", "statement": "Keep it separate."}],
        }
        out = am.handle_prompt_submit(prompt_event(LONG_PROMPT), self.config, client)
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()


class DefaultBaseUrlTest(unittest.TestCase):
    """The public API base carries the `/api` prefix the web tier proxies.

    `https://attacca.cc/v1/memory/recall` is answered by nginx with 405 (only
    `/api/` is proxied to the server), so a default without the prefix makes
    every hook fail open and recall nothing.
    """

    def test_the_default_base_url_includes_the_api_prefix(self):
        config = am.Config.from_env({})
        self.assertEqual(config.base_url, "https://attacca.cc/api")

    def test_the_recall_url_is_the_proxied_path(self):
        client = am.HttpRecallClient("https://attacca.cc/api", "atk_test")
        self.assertEqual(client.base_url + "/v1/memory/recall", "https://attacca.cc/api/v1/memory/recall")
