"""Unit tests for the write path in hooks/attacca_memory.py.

Run with:  python3 -m unittest discover -s tests -v
No network: every HTTP call goes through a stub client, and the two tests that
drive `main()` patch the client classes out.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "attacca_memory.py"

_spec = importlib.util.spec_from_file_location("attacca_memory", SCRIPT)
am = importlib.util.module_from_spec(_spec)
sys.modules["attacca_memory"] = am
_spec.loader.exec_module(am)


class StubObserveClient:
    """Stands in for HttpObserveClient: returns what it is told, or raises."""

    calls = None

    def __init__(self, response=None, error=None):
        self.response = {} if response is None else response
        self.error = error
        self.calls = []

    def observe(self, session_id, turns, flush=True, timeout=20.0):
        self.calls.append(
            {
                "session_id": session_id,
                "turns": turns,
                "flush": flush,
                "timeout": timeout,
            }
        )
        if self.error:
            raise self.error
        return self.response


class StubRecallClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def recall(self, *args, **kwargs):
        StubRecallClient.calls.append(kwargs or args)
        raise urllib.error.URLError("no recall in these tests")


def make_config(tmpdir, **overrides):
    values = dict(token="atk_test", state_dir=tmpdir, observe_enabled=True)
    values.update(overrides)
    return am.Config(**values)


def stop_event(message, session_id="s1", cwd=None):
    return {
        "session_id": session_id,
        "hook_event_name": "Stop",
        "cwd": cwd or "/tmp/project",
        "last_assistant_message": message,
    }


def session_end_event(session_id="s1"):
    return {
        "session_id": session_id,
        "hook_event_name": "SessionEnd",
        "reason": "other",
        "cwd": "/tmp/project",
    }


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(self.tmp.name)

    def turns(self, client):
        return [turn for call in client.calls for turn in call["turns"]]

    def run_turns(self, count, client, start_ms=1_000_000, prompt="a question worth keeping"):
        for index in range(count):
            now = start_ms + index * 1000
            am.record_prompt(self.config, "cc:s1", f"{prompt} #{index}", now)
            am.handle_stop(stop_event(f"the answer to {index}"), self.config, client, now=now)


class BatchBoundaryTest(QueueTestBase):
    def test_six_turns_send_exactly_one_observe_of_six_turns(self):
        client = StubObserveClient(response={"accepted": 6, "skipped": 0, "refused": 0})
        self.run_turns(2, client)
        self.assertEqual(client.calls, [], "a batch was sent before the sixth turn")
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 2)

        am.record_prompt(self.config, "cc:s1", "the third question", 1_002_000)
        am.handle_stop(stop_event("the third answer"), self.config, client, now=1_002_000)

        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["session_id"], "cc:s1")
        self.assertTrue(call["flush"])
        self.assertEqual(len(call["turns"]), 6)
        self.assertEqual(
            [t["role"] for t in call["turns"]],
            ["user", "assistant"] * 3,
        )
        self.assertEqual(call["turns"][0]["content"], "a question worth keeping #0")
        self.assertEqual(call["turns"][1]["content"], "the answer to 0")
        self.assertEqual(am.load_queue(self.config, "cc:s1"), [])

    def test_a_batch_larger_than_the_boundary_is_sent_in_six_turn_slices(self):
        client = StubObserveClient(response={"accepted": 6, "skipped": 0, "refused": 0})
        self.run_turns(6, client)  # 12 turns -> two batches of six
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([len(c["turns"]) for c in client.calls], [6, 6])
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 0)

    def test_a_pair_older_than_the_age_boundary_is_sent_alone(self):
        self.config = make_config(self.tmp.name, batch_max_age_secs=60.0)
        client = StubObserveClient(response={"accepted": 2, "skipped": 0, "refused": 0})
        am.record_prompt(self.config, "cc:s1", "one question", 1_000_000)
        am.handle_stop(stop_event("one answer"), self.config, client, now=1_000_000)
        self.assertEqual(client.calls, [])

        am.record_prompt(self.config, "cc:s1", "another question", 1_061_000)
        am.handle_stop(stop_event("another answer"), self.config, client, now=1_061_000)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]["turns"]), 4)

    def test_no_prompt_pairs_a_lone_assistant_turn(self):
        client = StubObserveClient(response={"accepted": 1, "skipped": 0, "refused": 0})
        am.handle_stop(stop_event("an answer with no recorded prompt"), self.config, client, now=5)
        self.assertEqual(am.load_queue(self.config, "cc:s1")[0]["turns"][0]["role"], "assistant")

    def test_prompt_id_pairs_exactly_and_drops_stale_prompts(self):
        am.record_prompt(self.config, "cc:s1", "the prompt that failed", 1, "p-1")
        am.record_prompt(self.config, "cc:s1", "the prompt that answered", 2, "p-2")
        client = StubObserveClient()
        event = stop_event("the reply")
        event["prompt_id"] = "p-2"
        am.handle_stop(event, self.config, client, now=3)
        pair = am.load_queue(self.config, "cc:s1")[0]
        self.assertEqual(pair["turns"][0]["content"], "the prompt that answered")
        self.assertEqual(am.load_pending(self.config, "cc:s1"), [])

    def test_a_pair_appended_during_a_flush_is_not_erased(self):
        config = make_config(self.tmp.name, batch_turns=2)

        class RacingClient(StubObserveClient):
            def observe(self, session_id, turns, flush=True, timeout=20.0):
                # Another Stop lands while this one is on the wire.
                am.append_pair(
                    config,
                    "cc:s1",
                    am.build_pair({"content": "arrived during the send", "ts": 9}, "its reply", 9),
                )
                return super().observe(session_id, turns, flush, timeout)

        client = RacingClient(response={"accepted": 2, "skipped": 0, "refused": 0})
        am.append_pair(
            config,
            "cc:s1",
            am.build_pair({"content": "the first", "ts": 1}, "the first reply", 1),
        )
        am.handle_stop(stop_event("second reply"), config, client, now=2)
        queued = am.load_queue(config, "cc:s1")
        # The sent pair is gone; the Stop's own pair and the one that landed
        # during the wire call are both still there.
        contents = [pair["turns"][0]["content"] for pair in queued]
        self.assertEqual(contents, ["second reply", "arrived during the send"])

    def test_an_empty_stop_records_nothing(self):
        client = StubObserveClient()
        am.handle_stop(stop_event(""), self.config, client, now=5)
        self.assertEqual(am.load_queue(self.config, "cc:s1"), [])
        self.assertEqual(client.calls, [])


class RetryTest(QueueTestBase):
    def test_failure_keeps_the_queue_and_the_retry_reuses_every_turn_id(self):
        failing = StubObserveClient(error=urllib.error.URLError("connection refused"))
        self.run_turns(3, failing)
        self.assertEqual(len(failing.calls), 1)
        queued = am.load_queue(self.config, "cc:s1")
        self.assertEqual(len(queued), 3)
        queued_ids = [turn["turn_id"] for pair in queued for turn in pair["turns"]]

        working = StubObserveClient(response={"accepted": 0, "skipped": 6, "refused": 0})
        am.record_prompt(self.config, "cc:s1", "a fourth question", 1_003_000)
        am.handle_stop(stop_event("a fourth answer"), self.config, working, now=1_003_000)

        self.assertEqual(len(working.calls), 1)
        sent = [turn["turn_id"] for turn in working.calls[0]["turns"]]
        self.assertEqual(sent, queued_ids, "the retry invented new turn ids")
        # The first three pairs are gone; the fourth is still queued, unseen.
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 1)

    def test_server_error_keeps_the_batch(self):
        client = StubObserveClient(
            error=urllib.error.HTTPError("http://x/v1/memory/observe", 503, "busy", {}, None)
        )
        self.run_turns(3, client)
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 3)

    def test_a_key_without_the_write_scope_drops_the_batch(self):
        client = StubObserveClient(
            error=urllib.error.HTTPError("http://x/v1/memory/observe", 403, "forbidden", {}, None)
        )
        self.run_turns(3, client)
        self.assertEqual(am.load_queue(self.config, "cc:s1"), [])

    def test_a_refused_tail_keeps_the_batch_for_the_next_day(self):
        client = StubObserveClient(response={"accepted": 3, "skipped": 0, "refused": 3})
        self.run_turns(3, client)
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 3)


class OptOutTest(QueueTestBase):
    def test_observe_disabled_writes_no_queue_and_calls_nothing(self):
        config = make_config(self.tmp.name, observe_enabled=False)
        client = StubObserveClient()
        self.assertFalse(am.observe_active(config, stop_event("x")))
        for index in range(6):
            self.assertFalse(am.observe_active(config, stop_event("x")))
        self.assertEqual(client.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "queue")))

    def test_repo_file_turns_observe_off_and_back_on(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, ".claude"))
        toml = os.path.join(repo, ".claude", "attacca.toml")
        with open(toml, "w", encoding="utf-8") as handle:
            handle.write("# this repository keeps its activity local\nobserve = false\n")

        self.assertTrue(am.repo_opted_out({"cwd": repo}))
        self.assertFalse(am.observe_active(self.config, {"cwd": repo}))

        with open(toml, "w", encoding="utf-8") as handle:
            handle.write("observe = true\n")
        self.assertFalse(am.repo_opted_out({"cwd": repo}))
        self.assertTrue(am.observe_active(self.config, {"cwd": repo}))

    def test_optout_is_found_from_a_subdirectory(self):
        repo = os.path.join(self.tmp.name, "repo")
        nested = os.path.join(repo, "crates", "api")
        os.makedirs(os.path.join(repo, ".claude"))
        os.makedirs(nested)
        with open(os.path.join(repo, ".claude", "attacca.toml"), "w", encoding="utf-8") as handle:
            handle.write("observe = false\n")
        self.assertTrue(am.repo_opted_out({"cwd": nested}))

    def test_a_key_inside_a_section_is_not_the_switch(self):
        path = os.path.join(self.tmp.name, "attacca.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[attacca]\nobserve = false\n")
        self.assertFalse(am.read_repo_optout(path))

    def test_an_unrelated_key_does_not_hide_the_switch(self):
        path = os.path.join(self.tmp.name, "attacca.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('project_id = "abc"\nobserve = false\n')
        self.assertTrue(am.read_repo_optout(path))

    def test_no_repo_file_means_not_opted_out(self):
        self.assertFalse(am.repo_opted_out({"cwd": self.tmp.name}))


class QueueHygieneTest(QueueTestBase):
    def test_the_queue_is_capped_and_drops_the_oldest(self):
        config = make_config(self.tmp.name, queue_max_pairs=3, batch_turns=100)
        client = StubObserveClient()
        for index in range(5):
            am.record_prompt(config, "cc:s1", f"question {index}", 1_000 + index)
            am.handle_stop(stop_event(f"answer {index}"), config, client, now=1_000 + index)
        queued = am.load_queue(config, "cc:s1")
        self.assertEqual(len(queued), 3)
        self.assertEqual(queued[0]["turns"][0]["content"], "question 2")

    def test_a_long_assistant_message_is_clipped_to_the_server_ceiling(self):
        client = StubObserveClient()
        am.handle_stop(stop_event("y" * 40_000), self.config, client, now=5)
        content = am.load_queue(self.config, "cc:s1")[0]["turns"][0]["content"]
        self.assertEqual(len(content), am.MAX_TURN_CONTENT_CHARS)
        self.assertTrue(content.endswith("[attacca: turn truncated]\n"))

    def test_corrupt_queue_lines_are_skipped(self):
        path = am._queue_file(self.config, "cc:s1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write(json.dumps({"pair_id": "p", "created_at": 5, "turns": []}) + "\n")
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 1)

    def test_many_pending_prompts_are_bounded(self):
        for index in range(am.MAX_PENDING_PROMPTS + 5):
            am.record_prompt(self.config, "cc:s1", f"prompt {index}", index)
        self.assertEqual(len(am.load_pending(self.config, "cc:s1")), am.MAX_PENDING_PROMPTS)


class SessionEndTest(QueueTestBase):
    def test_session_end_flushes_what_is_left(self):
        client = StubObserveClient(response={"accepted": 4, "skipped": 0, "refused": 0})
        self.run_turns(2, client)
        self.assertEqual(client.calls, [])
        am.handle_session_end(session_end_event(), self.config, client, now=1_010_000)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]["turns"]), 4)
        self.assertEqual(am.load_queue(self.config, "cc:s1"), [])

    def test_session_end_drains_a_backlog_in_batches(self):
        # Five pairs = ten turns queued by an outage. One flush would strand
        # the last four turns under a session id nothing revisits.
        client = StubObserveClient(response={"accepted": 6, "skipped": 0, "refused": 0})
        for index in range(5):
            am.record_prompt(self.config, "cc:s1", f"q {index}", 1_000 + index)
            am.append_pair(
                self.config,
                "cc:s1",
                am.build_pair({"content": f"q {index}", "ts": 1_000 + index}, f"a {index}", 1_000 + index),
            )
        self.assertEqual(len(am.load_queue(self.config, "cc:s1")), 5)
        am.handle_session_end(session_end_event(), self.config, client, now=1_010_000)
        self.assertEqual([len(call["turns"]) for call in client.calls], [6, 4])
        self.assertEqual(am.load_queue(self.config, "cc:s1"), [])


class UlidTest(unittest.TestCase):
    def test_shape_and_order(self):
        first = am.new_ulid(1_700_000_000_000)
        second = am.new_ulid(1_700_000_001_000)
        self.assertEqual(len(first), 26)
        self.assertEqual(len(second), 26)
        self.assertTrue(first < second, "ULIDs do not sort by creation time")
        self.assertTrue(all(ch in am.ULID_ALPHABET for ch in first))
        self.assertNotEqual(am.new_ulid(5), am.new_ulid(5))


class FilePermissionTest(QueueTestBase):
    def test_queue_and_pending_files_are_owner_only(self):
        am.record_prompt(self.config, "cc:s1", "a question worth keeping", 5)
        am.handle_stop(stop_event("an answer"), self.config, StubObserveClient(), now=5)
        for path in (
            am._pending_file(self.config, "cc:s1"),
            am._queue_file(self.config, "cc:s1"),
        ):
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(
                os.stat(path).st_mode & 0o077, 0, f"{path} is readable by others"
            )


class MainDispatchTest(unittest.TestCase):
    """`main()` end to end, with the HTTP clients patched out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_env = {
            "CLAUDE_PLUGIN_DATA": self.tmp.name,
            "CLAUDE_PLUGIN_OPTION_ATTACCA_URL": "http://127.0.0.1:9",
            "CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN": "atk_test",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }

    def run_main(self, mode, event, observe_client, env_updates=None):
        env = dict(self.base_env)
        env.update(env_updates or {})
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(am, "HttpObserveClient", lambda *a, **k: observe_client):
                with mock.patch.object(am, "HttpRecallClient", StubRecallClient):
                    return am.main([mode], stdin=io.StringIO(json.dumps(event)))

    def test_disabled_observe_makes_no_calls_and_no_files(self):
        client = StubObserveClient()
        for index in range(3):
            event = stop_event(f"answer {index}")
            self.assertEqual(self.run_main("stop", event, client), 0)
            self.run_main("prompt-submit", {"session_id": "s1", "prompt": f"q {index}"}, client)
        self.assertEqual(client.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "queue")))

    def test_enabled_observe_batches_six_turns_through_main(self):
        client = StubObserveClient(response={"accepted": 6, "skipped": 0, "refused": 0})
        env = {"CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED": "true"}
        for index in range(3):
            self.run_main(
                "prompt-submit", {"session_id": "s1", "prompt": f"question {index}"}, client, env
            )
            self.run_main("stop", stop_event(f"answer {index}"), client, env)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]["turns"]), 6)

    def test_a_repo_optout_stops_the_prompt_recall_too(self):
        repo = os.path.join(self.tmp.name, "repo2")
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "attacca.toml"), "w", encoding="utf-8") as handle:
            handle.write("observe = false\n")
        StubRecallClient.calls = []
        client = StubObserveClient()
        env = {"CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED": "true"}
        self.run_main(
            "prompt-submit",
            {"session_id": "s1", "prompt": "a long enough question about this repo", "cwd": repo},
            client,
            env,
        )
        self.assertEqual(StubRecallClient.calls, [], "the prompt text still left the machine")
        self.assertEqual(client.calls, [])

    def test_a_repo_optout_stops_every_outbound_call(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "attacca.toml"), "w", encoding="utf-8") as handle:
            handle.write("observe = false\n")
        client = StubObserveClient()
        env = {"CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED": "true"}
        for index in range(3):
            self.run_main(
                "prompt-submit",
                {"session_id": "s1", "prompt": f"question {index}", "cwd": repo},
                client,
                env,
            )
            self.run_main("stop", stop_event(f"answer {index}", cwd=repo), client, env)
        self.assertEqual(client.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "queue")))


if __name__ == "__main__":
    unittest.main()
