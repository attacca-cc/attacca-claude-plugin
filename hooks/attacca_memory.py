#!/usr/bin/env python3
"""Attacca memory hooks for Claude Code. Standard library only.

Four events, one process each:

  session-start   SessionStart      -> POST /v1/memory/recall with a fixed profile query
  prompt-submit   UserPromptSubmit  -> POST /v1/memory/recall for the prompt itself
                                    -> (write path, opt-in) remember the prompt for the Stop hook
  stop            Stop              -> (write path, opt-in) queue the turn pair, flush every 6
  session-end     SessionEnd        -> (write path, opt-in) flush what the queue still holds

The read path is on by default. The write path is off unless `observe_enabled`
is true and the repository has not opted out in `.claude/attacca.toml`.

Everything here fails open. A timeout, a network error, a non-2xx answer or a
payload that does not parse prints nothing and exits 0, because a memory plugin
must never be the reason a prompt is slow, blocked or lost. The Stop and
SessionEnd hooks run with `async: true`, so they are off the response path
entirely.

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
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

try:  # POSIX only; Windows has no fcntl, and a lock is an improvement, not a requirement.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

VERSION = "0.2.0"

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

# ---- The write path (opt-in) -------------------------------------------------
#
# One `Stop` settles one turn pair (your prompt, Claude's reply). Pairs wait in
# `${CLAUDE_PLUGIN_DATA}/queue/<session>.jsonl` until a batch boundary:
#
#   * six queued turns have accumulated -- three pairs, the batch size the P2
#     plan fixed, counted the way the server counts them (`accepted` + `skipped`
#     are per turn), or
#   * the oldest queued pair is ten minutes old (a slow session still drains), or
#   * the session ends (a best-effort SessionEnd flush).
#
# All four numbers below are constants rather than magic literals, because the
# tests and the README both depend on the exact values.
DEFAULT_BATCH_TURNS = 6
DEFAULT_BATCH_MAX_AGE_SECS = 600.0
# Far above any honest session's backlog (200 pairs = 400 turns) and small
# enough that a machine that cannot reach Attacca does not fill the disk.
DEFAULT_QUEUE_MAX_PAIRS = 200
DEFAULT_OBSERVE_DEADLINE_SECS = 120.0

# The server's own ceilings (`/v1/memory/observe`): a turn longer than this is
# rejected with a 400, and a batch larger than 64 turns likewise. A final
# assistant message can run long, so the client truncates rather than sending a
# batch the server will throw away.
MAX_TURN_CONTENT_CHARS = 20_000
MAX_BATCH_TURNS = 64

# At most this many unpaired prompts are kept per session, in case a Stop never
# arrives (an interrupt, a crash). Oldest first.
MAX_PENDING_PROMPTS = 8

# A response body above this is not an observe answer; it is a redirect to a
# captive portal or an error page, and reading it is not worth the memory.
MAX_OBSERVE_RESPONSE_BYTES = 64 * 1024

# Repo opt-out file, relative to the repository root.
REPO_OPTOUT_FILE = os.path.join(".claude", "attacca.toml")

# Statuses that will not get better by sending the same bytes again: a
# malformed turn, a key without `memory:write`, a wrong URL. The batch is
# dropped and the reason goes to the debug log. Everything else (network error,
# timeout, 5xx, 408, 429) keeps the batch for the next boundary.
PERMANENT_OBSERVE_STATUSES = frozenset({400, 401, 403, 404, 413, 422})

# Crockford base32, the alphabet a ULID uses.
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

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
    base_url: str = "https://attacca.cc/api"
    token: str = ""
    recall_on_prompt: bool = True
    project_id: str = ""
    observe_enabled: bool = False
    start_budget_tokens: int = DEFAULT_START_BUDGET_TOKENS
    prompt_budget_tokens: int = DEFAULT_PROMPT_BUDGET_TOKENS
    start_deadline_secs: float = DEFAULT_START_DEADLINE_SECS
    prompt_deadline_secs: float = DEFAULT_PROMPT_DEADLINE_SECS
    circuit_breaker_secs: float = DEFAULT_CIRCUIT_BREAKER_SECS
    batch_turns: int = DEFAULT_BATCH_TURNS
    batch_max_age_secs: float = DEFAULT_BATCH_MAX_AGE_SECS
    queue_max_pairs: int = DEFAULT_QUEUE_MAX_PAIRS
    observe_deadline_secs: float = DEFAULT_OBSERVE_DEADLINE_SECS
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
            base_url=_read(env, "CLAUDE_PLUGIN_OPTION_ATTACCA_URL", "https://attacca.cc/api"),
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
            batch_turns=_read_int(env, "ATTACCA_BATCH_TURNS", DEFAULT_BATCH_TURNS),
            batch_max_age_secs=_read_float(
                env, "ATTACCA_BATCH_MAX_AGE_SECS", DEFAULT_BATCH_MAX_AGE_SECS
            ),
            queue_max_pairs=_read_int(
                env, "ATTACCA_QUEUE_MAX_PAIRS", DEFAULT_QUEUE_MAX_PAIRS
            ),
            observe_deadline_secs=_read_float(
                env, "ATTACCA_OBSERVE_DEADLINE_SECS", DEFAULT_OBSERVE_DEADLINE_SECS
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
    return os.path.join(config.state_dir, f"{_session_slug(session_id)}.json")


def _session_slug(session_id: str) -> str:
    return "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in session_id
    )[:120]


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
# The write path: turn pairs, the queue, and the batch boundary
#
# The design constraint that shapes all of this: a Claude Code session must not
# lose a turn because the network was down, and must not record the same turn
# twice when it retries. So a pair is written to disk first and removed only
# after the server has answered for it, and every turn carries a client-made
# ULID that the server's ledger dedupes on. A retry therefore rewrites nothing
# and creates no second episode.
# ---------------------------------------------------------------------------


def new_ulid(now_ms=None) -> str:
    """A 26-character Crockford base32 ULID: 48-bit time, 80 random bits.

    Sorts by creation time as a string, which is what makes the queue file and
    the server's ledger easy to reason about, and carries no information about
    the user beyond milliseconds.
    """
    millis = int(time.time() * 1000) if now_ms is None else int(now_ms)
    chars = []
    value = millis & ((1 << 48) - 1)
    for _ in range(10):
        chars.append(ULID_ALPHABET[value & 31])
        value >>= 5
    stamp = "".join(reversed(chars))
    randomness = "".join(ULID_ALPHABET[b % 32] for b in os.urandom(16))
    return stamp + randomness


def observe_active(config: Config, event=None) -> bool:
    """Whether this run may record or send anything at all.

    Two independent switches, both defaulting to off-ish: the user config
    (`observe_enabled`, shipped `false`) and a repository opt-out file. The
    repository wins, because a person who writes `observe = false` into a repo
    means it for that repo whatever their global setting says.
    """
    if not config.enabled:
        return False
    if not config.observe_enabled:
        return False
    return not repo_opted_out(event)


def _candidate_project_dirs(event) -> list:
    dirs = []
    if isinstance(event, dict):
        for key in ("cwd", "project_dir"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                dirs.append(value.strip())
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_dir:
        dirs.append(env_dir)
    return dirs


def _observe_flag_from_text(text: str):
    """The top-level `observe` key of an attacca.toml, or None.

    Deliberately small: the file is a few keys, not a configuration language,
    and a hook that runs before every prompt should not carry a TOML parser for
    older Python 3 versions that lack `tomllib`. Only top-level keys count; a
    key inside `[some.section]` is not this switch.
    """
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("["):
            break
        match = re.match(r"observe\s*=\s*(true|false)\s*$", stripped, re.IGNORECASE)
        if match:
            return match.group(1).lower() == "true"
    return None


def read_repo_optout(path: str):
    """True when the file says `observe = false`, False/None otherwise."""
    try:
        if os.path.getsize(path) > 64 * 1024:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:  # Python 3.11+; the regex below is the fallback for older runtimes.
        import tomllib

        data = tomllib.loads(text)
        if isinstance(data, dict) and "observe" in data:
            return data["observe"] is False
    except ImportError:
        pass
    except (ValueError, TypeError):
        # A broken file is not an opt-out: fail towards the user's setting.
        return None
    return _observe_flag_from_text(text) is False


def repo_opted_out(event=None) -> bool:
    """Whether the project this hook runs in asked not to send anything.

    The `cwd` a hook receives is not always the repository root (a session can
    be started in a subdirectory), so the search walks up a bounded number of
    parents. It stops at the first `.claude/attacca.toml` it finds; a nested
    repository's file therefore decides for that repository.
    """
    seen = set()
    for start in _candidate_project_dirs(event):
        directory = os.path.abspath(start)
        for _ in range(12):
            if directory in seen:
                break
            seen.add(directory)
            candidate = os.path.join(directory, REPO_OPTOUT_FILE)
            if os.path.isfile(candidate):
                if read_repo_optout(candidate):
                    _debug(f"observe off: {candidate} sets observe = false")
                    return True
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return False


def _queue_dir(config: Config) -> str:
    return os.path.join(config.state_dir, "queue")


def _queue_file(config: Config, session_id: str) -> str:
    return os.path.join(_queue_dir(config), f"{_session_slug(session_id)}.jsonl")


def _pending_file(config: Config, session_id: str) -> str:
    return os.path.join(_queue_dir(config), f"{_session_slug(session_id)}.pending.json")


def _load_json_list(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _private_write(path: str, text: str) -> None:
    """Write a file only the owner can read.

    The queue and the pending prompts are conversation text. A default-mode
    file is `0644 & ~umask`, which on a shared machine would let every other
    account read what the user said, so both are created `0600` in a `0700`
    directory. `os.replace` keeps the permission bits of the file it moves.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_json(path: str, payload) -> None:
    tmp = path + ".tmp"
    _private_write(tmp, json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)


def load_pending(config: Config, session_id: str) -> list:
    return [p for p in _load_json_list(_pending_file(config, session_id)) if isinstance(p, dict)]


def save_pending(config: Config, session_id: str, prompts: list) -> None:
    _write_json(_pending_file(config, session_id), prompts[-MAX_PENDING_PROMPTS:])


def record_prompt(
    config: Config, session_id: str, prompt: str, now_ms: int, prompt_id: str = ""
) -> bool:
    """Remember the prompt so the next Stop can pair a reply with it.

    Called on the synchronous prompt path, so it writes one small file and
    returns; nothing here talks to the network. `prompt_id` is Claude Code's own
    id for the prompt: carrying it lets the Stop hook pair exactly rather than
    positionally.
    """
    text = prompt if isinstance(prompt, str) else ""
    if not text.strip():
        return False
    try:
        prompts = load_pending(config, session_id)
        entry = {"content": text, "ts": now_ms}
        if isinstance(prompt_id, str) and prompt_id:
            entry["prompt_id"] = prompt_id
        prompts.append(entry)
        save_pending(config, session_id, prompts)
        return True
    except OSError as exc:
        _debug(f"could not record the prompt: {exc}")
        return False


def load_queue(config: Config, session_id: str) -> list:
    """Every queued turn pair, oldest first. Malformed lines are skipped."""
    path = _queue_file(config, session_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    pairs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            pair = json.loads(line)
        except ValueError:
            continue
        if isinstance(pair, dict) and isinstance(pair.get("turns"), list):
            pairs.append(pair)
    return pairs


def save_queue(config: Config, session_id: str, pairs: list) -> None:
    path = _queue_file(config, session_id)
    try:
        if not pairs:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        _private_write(
            tmp, "".join(json.dumps(pair, ensure_ascii=False) + "\n" for pair in pairs)
        )
        os.replace(tmp, path)
    except OSError as exc:
        _debug(f"could not write the observe queue: {exc}")


def clip_turn(text: str, limit: int = MAX_TURN_CONTENT_CHARS) -> str:
    """Bound one turn to the server's ceiling, keeping the end visible."""
    text = text if isinstance(text, str) else ""
    if len(text) <= limit:
        return text
    marker = "\n[attacca: turn truncated]\n"
    return text[: limit - len(marker)] + marker


def build_pair(prompt, assistant, now_ms: int) -> dict:
    """One turn pair with a client-made ULID per turn.

    A pair whose two halves are both empty is not a turn and never reaches the
    queue; a pair with one empty half keeps the half that exists, because a
    tool-only reply is still a turn the user paid for.
    """
    turns = []
    if isinstance(prompt, dict) and str(prompt.get("content", "")).strip():
        turns.append(
            {
                "role": "user",
                "content": clip_turn(prompt["content"]),
                "ts": int(prompt.get("ts") or now_ms),
                "turn_id": new_ulid(now_ms),
            }
        )
    if isinstance(assistant, str) and assistant.strip():
        turns.append(
            {
                "role": "assistant",
                "content": clip_turn(assistant),
                "ts": now_ms,
                "turn_id": new_ulid(now_ms),
            }
        )
    if not turns:
        return {}
    return {"pair_id": new_ulid(now_ms), "created_at": now_ms, "turns": turns}


def queue_is_due(pairs: list, config: Config, now_ms: int) -> bool:
    """Whether a batch boundary has been reached: six queued turns, or age."""
    if len(_turns_of(pairs)) >= max(1, config.batch_turns):
        return True
    if not pairs:
        return False
    oldest = pairs[0].get("created_at")
    try:
        oldest_ms = int(oldest)
    except (TypeError, ValueError):
        return True
    return (now_ms - oldest_ms) >= config.batch_max_age_secs * 1000.0


def batch_slice(pairs: list, max_turns: int) -> list:
    """The oldest pairs that fill one batch, never splitting a pair.

    A pair is two turns (a prompt and a reply), so a six-turn batch is three
    pairs. A single pair is always taken even if it is larger than the whole
    allowance, because a turn that is never sent is a turn lost.
    """
    batch, count = [], 0
    for pair in pairs:
        size = len(_turns_of([pair]))
        if batch and count + size > max_turns:
            break
        batch.append(pair)
        count += size
        if count >= max_turns:
            break
    return batch


def _turns_of(pairs: list) -> list:
    turns = []
    for pair in pairs:
        for turn in pair.get("turns", []):
            if not isinstance(turn, dict):
                continue
            content = turn.get("content")
            turn_id = turn.get("turn_id")
            role = turn.get("role")
            if not isinstance(content, str) or not content.strip():
                continue
            if not isinstance(turn_id, str) or not turn_id:
                continue
            if role not in ("user", "assistant", "system", "tool"):
                continue
            entry = {"role": role, "content": content, "turn_id": turn_id}
            ts = turn.get("ts")
            if isinstance(ts, int):
                entry["ts"] = ts
            turns.append(entry)
    return turns


class PermanentObserveError(Exception):
    """The server will refuse this batch however often it is sent."""


class _QueueLock:
    """An advisory exclusive lock over one session's queue.

    `Stop` and `SessionEnd` are separate background processes, and a flush can
    hold the wire for up to two minutes, so two of them can overlap. Without a
    lock the append and the trim are read-modify-write races: the flush's stale
    snapshot would rewrite the file without a pair a concurrent Stop had just
    added. The lock file lives beside the queue and is emptied by nothing;
    `flock` releases it when the process exits, however it exits.
    """

    def __init__(self, config: Config, session_id: str):
        self.path = os.path.join(_queue_dir(config), f"{_session_slug(session_id)}.lock")
        self.handle = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            self.handle = open(self.path, "a+")
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:  # a lock is best effort; a queue write still beats none
            _debug(f"could not take the queue lock: {exc}")
            self.handle = None
        return self

    def __exit__(self, *exc_info):
        if self.handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self.handle.close()
        return False


def append_pair(config: Config, session_id: str, pair: dict) -> list:
    """Add one pair under the lock, trimming the oldest pairs past the cap."""
    with _QueueLock(config, session_id):
        pairs = load_queue(config, session_id)
        pairs.append(pair)
        cap = max(1, config.queue_max_pairs)
        if len(pairs) > cap:
            _debug(f"queue over {cap} pairs; dropping the oldest")
            pairs = pairs[-cap:]
        save_queue(config, session_id, pairs)
        return pairs


def drop_pairs(config: Config, session_id: str, sent_pair_ids) -> list:
    """Remove exactly the pairs that were sent, from a freshly read queue.

    Removing by id rather than by index is what keeps a concurrent Stop's pair
    alive: the flush loaded its snapshot before the wire call, and anything
    appended during that call is not in the snapshot but is in the file.
    """
    sent = set(sent_pair_ids)
    with _QueueLock(config, session_id):
        remaining = [
            pair for pair in load_queue(config, session_id) if pair.get("pair_id") not in sent
        ]
        save_queue(config, session_id, remaining)
        return remaining


def flush_queue(config: Config, session_id: str, client, now_ms=None, force=False):
    """Send the oldest batch, then forget exactly what the server took.

    Returns the server's answer, or None when nothing was sent. A batch that
    was accepted (or already admitted under the same `turn_id`s) leaves the
    queue; a transient failure leaves every line where it is, so the next
    boundary sends it again with the same ids.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    pairs = load_queue(config, session_id)
    if not pairs:
        return None
    if not force and not queue_is_due(pairs, config, now_ms):
        return None
    batch = batch_slice(pairs, max(1, min(config.batch_turns, MAX_BATCH_TURNS)))
    sent_ids = [pair.get("pair_id") for pair in batch]
    turns = _turns_of(batch)
    if not turns:
        drop_pairs(config, session_id, sent_ids)
        return None
    try:
        response = client.observe(
            session_id, turns, flush=True, timeout=config.observe_deadline_secs
        )
    except urllib.error.HTTPError as exc:
        if exc.code in PERMANENT_OBSERVE_STATUSES:
            _debug(
                f"observe dropped a batch the server will never take: HTTP {exc.code}; "
                "check that the key has the memory:write scope and the turns are well formed"
            )
            drop_pairs(config, session_id, sent_ids)
            return None
        _debug(f"observe failed with HTTP {exc.code}; batch kept for the next boundary")
        return None
    except Exception as exc:  # noqa: BLE001 - the write path never fails a turn
        _debug(f"observe failed: {type(exc).__name__}: {exc}; batch kept")
        return None
    if not isinstance(response, dict):
        _debug("observe answered something that is not an object; batch kept")
        return None
    refused = response.get("refused") or 0
    if refused:
        # The daily cap refused the tail of the batch. Keep the batch: a resend
        # after the cap resets is deduped by `turn_id`, so retrying is free.
        _debug(f"observe refused {refused} turn(s) at the daily cap; batch kept")
        return response
    drop_pairs(config, session_id, sent_ids)
    _debug(
        "observe sent {len} turn(s): accepted={a} skipped={s} redacted={r} consolidated={c}".format(
            len=len(turns),
            a=response.get("accepted"),
            s=response.get("skipped"),
            r=response.get("redacted"),
            c=response.get("consolidated"),
        )
    )
    return response


def _take_prompt(prompts: list, prompt_id) -> tuple:
    """The prompt this Stop belongs to, and what is left of the pending list.

    Matching on Claude Code's `prompt_id` when both sides carry one makes the
    pairing exact; without it the oldest pending prompt is used, which is what
    keeps working on an older Claude Code. Entries before the chosen one are
    dropped: they are prompts whose turn never produced a Stop (an API error, an
    interrupt), and keeping them would shift every later pair by one.
    """
    if not prompts:
        return None, []
    if isinstance(prompt_id, str) and prompt_id:
        for index, entry in enumerate(prompts):
            if entry.get("prompt_id") == prompt_id:
                return entry, prompts[index + 1:]
    return prompts[0], prompts[1:]


def handle_stop(event, config: Config, client, now=None):
    """Stop: settle this turn pair into the queue, then maybe flush.

    Stop rather than SessionEnd: a Stop fires at the end of every response, so a
    turn is on disk before the session ends rather than only at teardown, and a
    SessionEnd hook only gets whatever is left of the session's exit.
    """
    now_ms = int(time.time() * 1000) if now is None else int(now)
    session_id = _session_id(event)
    if not session_id:
        return None
    prompt, remaining = _take_prompt(
        load_pending(config, session_id),
        event.get("prompt_id") if isinstance(event, dict) else None,
    )
    pair = build_pair(prompt, event.get("last_assistant_message"), now_ms)
    if not pair:
        # Nothing to record. The prompt stays where it was, so a reply that never
        # arrived does not silently consume it.
        return None
    pairs = append_pair(config, session_id, pair)
    # The queue write is what makes the turn durable; only now is the prompt
    # dropped, so a crash between the two re-pairs instead of losing the turn.
    try:
        save_pending(config, session_id, remaining)
    except OSError as exc:
        _debug(f"could not clear the pending prompt: {exc}")
    if not pairs:
        return None
    return flush_queue(config, session_id, client, now_ms)


def handle_session_end(event, config: Config, client, now=None):
    """SessionEnd: drain what the queue still holds, one batch at a time.

    A single flush would strand everything past the first six turns: the queue
    file is keyed by the session id, and no later session looks at it. So the
    loop keeps sending while the server accepts and the queue shrinks, bounded
    so a pathological queue cannot hold the teardown forever.
    """
    now_ms = int(time.time() * 1000) if now is None else int(now)
    session_id = _session_id(event)
    if not session_id:
        return None
    last = None
    for _ in range(20):
        before = len(load_queue(config, session_id))
        if before == 0:
            break
        response = flush_queue(config, session_id, client, now_ms, force=True)
        if response is None:
            break
        last = response
        if len(load_queue(config, session_id)) >= before:
            break  # nothing left the queue; sending again would repeat itself
    return last


class HttpObserveClient:
    """POST /v1/memory/observe against an Attacca instance."""

    def __init__(self, base_url: str, token: str, project_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project_id = project_id

    def observe(self, session_id: str, turns: list, flush: bool = True, timeout: float = 20.0):
        body = {"session_id": session_id, "turns": turns, "flush": bool(flush)}
        request = urllib.request.Request(
            self.base_url + "/v1/memory/observe",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": f"attacca-claude-plugin/{VERSION}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_OBSERVE_RESPONSE_BYTES)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("observe response is not a JSON object")
        return data


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
    observe_client = HttpObserveClient(config.base_url, config.token, config.project_id)
    mode = _mode(argv, event)
    # A repository that opted out gets no outbound traffic from the prompt path
    # at all: the per-prompt recall ships the prompt text, which is exactly what
    # `observe = false` is about. SessionStart still asks for the account
    # profile, which carries no session text.
    if mode in ("prompt-submit", "UserPromptSubmit") and repo_opted_out(event):
        return 0
    try:
        if mode in ("session-start", "SessionStart"):
            output = handle_session_start(event, config, client)
        elif mode in ("prompt-submit", "UserPromptSubmit"):
            # The write path records the prompt first: it is a small local
            # write, and it must happen whether or not the recall below is
            # skipped (a short prompt or a slash command is still a turn).
            if observe_active(config, event):
                session_id = _session_id(event)
                prompt = event.get("prompt") if isinstance(event, dict) else ""
                prompt_id = event.get("prompt_id") if isinstance(event, dict) else ""
                if session_id and isinstance(prompt, str):
                    record_prompt(
                        config,
                        session_id,
                        prompt,
                        int(time.time() * 1000),
                        prompt_id if isinstance(prompt_id, str) else "",
                    )
            output = handle_prompt_submit(event, config, client)
        elif mode in ("stop", "Stop"):
            # Async: whatever comes back is not hook output and must not be
            # printed. Claude Code reads stdout of an async hook as JSON output,
            # and the server's observe answer is not that schema.
            if observe_active(config, event):
                handle_stop(event, config, observe_client)
            return 0
        elif mode in ("session-end", "SessionEnd"):
            if observe_active(config, event):
                handle_session_end(event, config, observe_client)
            return 0
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
