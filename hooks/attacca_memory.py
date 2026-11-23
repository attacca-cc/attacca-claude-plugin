#!/usr/bin/env python3
"""Attacca memory hooks for Claude Code. Standard library only.

Two events, one process each:

  session-start   SessionStart      -> POST /v1/memory/recall with a fixed profile query
  prompt-submit   UserPromptSubmit  -> POST /v1/memory/recall for the prompt itself

Everything here fails open. A timeout, a network error, a non-2xx answer or a
payload that does not parse prints nothing and exits 0, because a memory plugin
must never be the reason a prompt is slow, blocked or lost.

Configuration comes from the environment, not from a config file: plugin hooks
cannot substitute ${user_config.*} in a shell-form command, and Claude Code
exports every option as CLAUDE_PLUGIN_OPTION_<KEY> instead
(https://code.claude.com/docs/en/plugins/manifest-reference, "Reference a saved
value"). The names this reads are listed in README.md.

Hook output schema (https://code.claude.com/docs/en/hooks):
  - SessionStart adds context through
    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
    (or plain-text stdout, which is added to context the same way).
  - UserPromptSubmit adds context through
    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}
  - Exit 0 with no output means "no decision, add nothing".
  - additionalContext is capped by Claude Code at 10,000 characters per string.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field

VERSION = "0.1.0"

# Claude Code caps additionalContext, systemMessage and plain stdout at 10,000
# characters per string and replaces anything longer with a file path plus a
# 2,000-character preview. Staying under the cap is how the text keeps reaching
# Claude instead of being moved to a file nobody reads.
MAX_CONTEXT_CHARS = 10_000
TRUNCATION_MARKER = "\n\n[attacca: context truncated at 10000 characters]"

# A prompt under this many characters is a "yes", "continue", "fix it" -- too
# small to carry a topic worth spending a recall round trip on.
MIN_PROMPT_CHARS = 20

# Defaults. Every one of these can be overridden by an environment variable,
# which is how the tests drive them without waiting real seconds.
DEFAULT_START_BUDGET_TOKENS = 1200
DEFAULT_PROMPT_BUDGET_TOKENS = 800
DEFAULT_START_DEADLINE_SECS = 2.0
DEFAULT_PROMPT_DEADLINE_SECS = 4.0
DEFAULT_CIRCUIT_BREAKER_SECS = 60.0

# A recall response is a rendered block plus node and claim metadata; 1 MiB is
# far past any honest answer and bounds what a single hook run can read.
MAX_RESPONSE_BYTES = 1 << 20

# The fixed query asked at session start. There is no user prompt yet, so it
# asks for exactly what a fresh session needs: who the user is, what they are
# working on, and what has been settled recently.
PROFILE_QUERY = (
    "Account profile: who this user is, the projects they are working on now, "
    "their standing preferences, and the decisions they have settled recently."
)

EXTERNAL_SESSION_PREFIX = "cc:"


def _debug(message: str) -> None:
    """Write a diagnostic line to stderr when ATTACCA_DEBUG is set."""
    if os.environ.get("ATTACCA_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        sys.stderr.write(f"[attacca] {message}\n")


def _read(env, name, default=None):
    value = env.get(name)
    return default if value is None or value == "" else value


def _read_bool(env, name, default):
    value = env.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _read_int(env, name, default):
    try:
        return int(env[name])
    except (KeyError, ValueError):
        return default


def _read_float(env, name, default):
    try:
        return float(env[name])
    except (KeyError, ValueError):
        return default


@dataclass
class Config:
    base_url: str = "https://attacca.cc"
    token: str = ""
    recall_on_prompt: bool = True
    project_id: str = ""
    observe_enabled: bool = False
    start_budget_tokens: int = DEFAULT_START_BUDGET_TOKENS
    prompt_budget_tokens: int = DEFAULT_PROMPT_BUDGET_TOKENS
    start_deadline_secs: float = DEFAULT_START_DEADLINE_SECS
    prompt_deadline_secs: float = DEFAULT_PROMPT_DEADLINE_SECS
    circuit_breaker_secs: float = DEFAULT_CIRCUIT_BREAKER_SECS
    state_dir: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        state_dir = _read(env, "CLAUDE_PLUGIN_DATA")
        if not state_dir:
            state_dir = os.path.join(
                os.path.expanduser("~"), ".cache", "attacca-claude-plugin"
            )
        return cls(
            base_url=_read(env, "CLAUDE_PLUGIN_OPTION_ATTACCA_URL", "https://attacca.cc"),
            token=_read(env, "CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN", ""),
            recall_on_prompt=_read_bool(
                env, "CLAUDE_PLUGIN_OPTION_RECALL_ON_PROMPT", True
            ),
            project_id=_read(env, "CLAUDE_PLUGIN_OPTION_PROJECT_ID", ""),
            observe_enabled=_read_bool(
                env, "CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED", False
            ),
            start_budget_tokens=_read_int(
                env, "ATTACCA_START_BUDGET_TOKENS", DEFAULT_START_BUDGET_TOKENS
            ),
            prompt_budget_tokens=_read_int(
                env, "ATTACCA_PROMPT_BUDGET_TOKENS", DEFAULT_PROMPT_BUDGET_TOKENS
            ),
            start_deadline_secs=_read_float(
                env, "ATTACCA_START_DEADLINE_SECS", DEFAULT_START_DEADLINE_SECS
            ),
            prompt_deadline_secs=_read_float(
                env, "ATTACCA_PROMPT_DEADLINE_SECS", DEFAULT_PROMPT_DEADLINE_SECS
            ),
            circuit_breaker_secs=_read_float(
                env, "ATTACCA_CIRCUIT_BREAKER_SECS", DEFAULT_CIRCUIT_BREAKER_SECS
            ),
            state_dir=state_dir,
        )


# ---------------------------------------------------------------------------
# Per-session state: which nodes this session has already been shown, and when
# the last recall failed (the circuit breaker).
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    injected_nodes: list = field(default_factory=list)
    last_failure_at: float = 0.0


def _state_file(config: Config, session_id: str) -> str:
    safe = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in session_id
    )[:120]
    return os.path.join(config.state_dir, f"{safe}.json")


def load_state(config: Config, session_id: str) -> SessionState:
    try:
        with open(_state_file(config, session_id), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return SessionState()
    if not isinstance(raw, dict):
        return SessionState()
    nodes = raw.get("injected_nodes")
    if not isinstance(nodes, list):
        nodes = []
    try:
        last_failure = float(raw.get("last_failure_at") or 0.0)
    except (TypeError, ValueError):
        last_failure = 0.0
    return SessionState(
        injected_nodes=[n for n in nodes if isinstance(n, str)],
        last_failure_at=last_failure,
    )


def save_state(config: Config, session_id: str, state: SessionState) -> None:
    try:
        os.makedirs(config.state_dir, exist_ok=True)
        path = _state_file(config, session_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "injected_nodes": state.injected_nodes,
                    "last_failure_at": state.last_failure_at,
                },
                handle,
            )
        os.replace(tmp, path)
    except OSError as exc:
        # State is an optimisation, never a requirement: without it a session
        # gets duplicate context, never missing context.
        _debug(f"could not write session state: {exc}")


# ---------------------------------------------------------------------------
# The HTTP client
# ---------------------------------------------------------------------------


class HttpRecallClient:
    """POST /v1/memory/recall against an Attacca instance."""

    def __init__(self, base_url: str, token: str, project_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_id = project_id

    def recall(
        self, session_id: str, message: str, budget_tokens: int, timeout: float = 10.0
    ):
        body = {
            "session_id": session_id,
            "message": message,
            "budget_tokens": budget_tokens,
        }
        # Accepted and ignored by the server until project binding (P3) ships;
        # sending it now costs nothing and needs no client change later.
        if self.project_id:
            body["project_id"] = self.project_id
        request = urllib.request.Request(
            self.base_url + "/v1/memory/recall",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": f"attacca-claude-plugin/{VERSION}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("recall response is not a JSON object")
        return data


class DeadlineExceeded(Exception):
    """The recall did not answer inside its budget."""


def call_with_deadline(function, deadline_secs: float):
    """Run `function` on a worker thread and give up after `deadline_secs`.

    urllib's `timeout` bounds one socket operation, not the whole exchange, and
    a hanging TLS handshake or a slow trickle of bytes would outlive it. A
    daemon thread plus a join is the only way to promise the hook returns
    within its budget; the thread is abandoned, and the process exits right
    after, which kills it.
    """
    result = {}

    def run():
        try:
            result["value"] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline_secs)
    if worker.is_alive():
        raise DeadlineExceeded(f"no answer within {deadline_secs}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


# ---------------------------------------------------------------------------
# Turning a recall response into hook output
# ---------------------------------------------------------------------------


def truncate_context(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATION_MARKER))
    return text[:keep] + TRUNCATION_MARKER


def rendered_context(response) -> str:
    """The block the server rendered, or an empty string.

    The server answers a second recall for the same `cc:` session with
    `rendered: ""` and `diagnostics.why: "no_delta"` -- it keeps a per-session
    ledger of what it already said. Empty is the healthy answer, not a failure.
    """
    if not isinstance(response, dict):
        return ""
    rendered = response.get("rendered")
    return rendered.strip() if isinstance(rendered, str) else ""


def node_names(response) -> list:
    if not isinstance(response, dict):
        return []
    nodes = response.get("nodes")
    if not isinstance(nodes, list):
        return []
    names = []
    for node in nodes:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def already_injected(response, injected) -> bool:
    """True when this answer carries nothing the session has not already seen."""
    names = node_names(response)
    if not names:
        return False
    if isinstance(response, dict) and response.get("claims"):
        return False
    known = set(injected)
    return all(name in known for name in names)


def _session_id(event) -> str:
    raw = ""
    if isinstance(event, dict):
        raw = event.get("session_id") or ""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return EXTERNAL_SESSION_PREFIX + raw.strip()


def _record_success(config: Config, session_id: str, state: SessionState, response) -> None:
    state.last_failure_at = 0.0
    for name in node_names(response):
        if name not in state.injected_nodes:
            state.injected_nodes.append(name)
    # Keep the file from growing without bound across a long session.
    if len(state.injected_nodes) > 500:
        state.injected_nodes = state.injected_nodes[-500:]
    save_state(config, session_id, state)


def _record_failure(config: Config, session_id: str, state: SessionState, now: float) -> None:
    state.last_failure_at = now
    save_state(config, session_id, state)


def _output_context(event_name: str, text: str):
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": truncate_context(text),
        }
    }


def handle_session_start(event, config: Config, client, now=None):
    """SessionStart: ask for the account profile, 2 seconds, fail open."""
    now = time.time() if now is None else now
    session_id = _session_id(event)
    if not session_id:
        _debug("session-start skipped: no session_id in the hook input")
        return None
    state = load_state(config, session_id)
    try:
        response = call_with_deadline(
            lambda: client.recall(
                session_id,
                PROFILE_QUERY,
                config.start_budget_tokens,
                timeout=config.start_deadline_secs,
            ),
            config.start_deadline_secs,
        )
    except Exception as exc:  # noqa: BLE001 - fail open by design
        _debug(f"session-start recall failed: {type(exc).__name__}: {exc}")
        _record_failure(config, session_id, state, now)
        return None
    text = rendered_context(response)
    _record_success(config, session_id, state, response)
    if not text:
        _debug("session-start recall returned nothing to inject")
        return None
    return _output_context("SessionStart", text)


def skip_reason(prompt: str, config: Config, state: SessionState, now: float) -> str:
    """Why this prompt should not be recalled for, or "" to go ahead."""
    if not config.enabled:
        return "no_token"
    if not config.recall_on_prompt:
        return "recall_on_prompt=false"
    text = prompt.strip()
    if not text:
        return "empty_prompt"
    if text.startswith("/"):
        # A slash command is the user driving a command, not describing a topic.
        return "slash_command"
    if len(text) < MIN_PROMPT_CHARS:
        return "too_short"
    if (
        state.last_failure_at
        and (now - state.last_failure_at) < config.circuit_breaker_secs
    ):
        return "circuit_open"
    return ""


def handle_prompt_submit(event, config: Config, client, now=None):
    """UserPromptSubmit: recall for the prompt, 4 seconds, fail open."""
    now = time.time() if now is None else now
    session_id = _session_id(event)
    if not session_id:
        return None
    prompt = event.get("prompt") if isinstance(event, dict) else ""
    prompt = prompt if isinstance(prompt, str) else ""
    state = load_state(config, session_id)
    reason = skip_reason(prompt, config, state, now)
    if reason:
        _debug(f"prompt-submit skipped: {reason}")
        return None
    try:
        response = call_with_deadline(
            lambda: client.recall(
                session_id,
                prompt.strip(),
                config.prompt_budget_tokens,
                timeout=config.prompt_deadline_secs,
            ),
            config.prompt_deadline_secs,
        )
    except Exception as exc:  # noqa: BLE001 - fail open by design
        _debug(f"prompt-submit recall failed: {type(exc).__name__}: {exc}")
        _record_failure(config, session_id, state, now)
        return None
    text = rendered_context(response)
    if not text:
        _record_success(config, session_id, state, response)
        return None
    if already_injected(response, state.injected_nodes):
        _debug("prompt-submit skipped: every node was already injected")
        _record_success(config, session_id, state, response)
        return None
    _record_success(config, session_id, state, response)
    return _output_context("UserPromptSubmit", text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _mode(argv, event) -> str:
    if argv:
        return argv[0].strip()
    name = event.get("hook_event_name") if isinstance(event, dict) else ""
    return name if isinstance(name, str) else ""


def main(argv=None, stdin=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    try:
        raw = stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError) as exc:
        _debug(f"hook input was not JSON: {exc}")
        return 0
    if not isinstance(event, dict):
        return 0

    config = Config.from_env()
    if not config.enabled:
        _debug("no Attacca API key configured; nothing to do")
        return 0

    client = HttpRecallClient(config.base_url, config.token, config.project_id)
    mode = _mode(argv, event)
    try:
        if mode in ("session-start", "SessionStart"):
            output = handle_session_start(event, config, client)
        elif mode in ("prompt-submit", "UserPromptSubmit"):
            output = handle_prompt_submit(event, config, client)
        else:
            _debug(f"unknown hook mode: {mode!r}")
            return 0
    except Exception as exc:  # noqa: BLE001 - the hook never fails a prompt
        _debug(f"hook handler raised: {type(exc).__name__}: {exc}")
        return 0

    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
