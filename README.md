# Attacca Memory for Claude Code

Bring your [Attacca](https://attacca.cc) long-term memory into Claude Code. What Attacca already
knows about you, your projects and the decisions you have settled is recalled and injected as
context at session start and before each prompt, so you do not start every session by explaining
yourself again.

This is v0.1: a **read-only** plugin. It recalls memory and shows you what Attacca knows. It does
not write anything back yet; sending session activity to Attacca is a later version and ships
switched off.

## Install (three commands)

Inside a Claude Code session:

```
/plugin marketplace add attacca-cc/attacca-claude-plugin
/plugin install attacca@attacca-claude-plugin
/plugin configure attacca@attacca-claude-plugin
```

The third command is where the plugin gets your settings: leave **Attacca URL** at `https://attacca.cc`
unless you self-host, and paste your API key into **Attacca API key** (the option is `sensitive`, so it
is stored as a credential rather than in plain text). `claude plugin install` from a terminal instead
of a session prints the same instruction as `5 userConfig options not yet set (1 required) — run
/plugin configure attacca@attacca-claude-plugin`. Then start a new session (`/clear` is enough) so the
hooks pick the key up, and run `/attacca:status` to see how many nodes Attacca holds for your account.

### Getting the API key

1. Sign in at <https://attacca.cc> and open **Settings → API keys**.
2. Create a key with the **`memory:read`** scope. v0.1 needs nothing else; `memory:write` is for the
   read-write version and is never sent by this plugin.
3. Copy the key once (it starts with `atk_`; Attacca shows it only at creation).

## What it does

| Hook | When | What it sends | Budget |
|---|---|---|---|
| `SessionStart` | a session starts, resumes, is cleared, compacted or forked | a fixed profile query | 2 s, 1,200 tokens |
| `UserPromptSubmit` | before each prompt you type | the prompt text | 4 s hard deadline, 800 tokens |
| `Stop` | Claude finishes a response | the turn pair, to the local queue (async) | off the response path |
| `SessionEnd` | the session terminates | a last flush of whatever is queued (async) | best effort, 1.5 s shared |

The first two call `POST /v1/memory/recall` on your Attacca instance with a `cc:<session_id>` external
session id, and put the rendered block into Claude's context. The last two are the write path, and
they do nothing unless `observe_enabled` is on (see [Write-back](#write-back-opt-in)).

Everything fails open. If Attacca is slow, down, returns 5xx, or returns something that does not
parse, the hook prints nothing and exits 0. A prompt is never delayed past its deadline, blocked or
lost because of this plugin.

### When it stays quiet

- the prompt starts with `/` (a slash command, not a topic)
- the prompt is empty or under 20 characters
- `recall_on_prompt` is off (session start still recalls)
- every node in the answer was already injected in this session
- a recall failed in the last 60 seconds (circuit breaker)
- no API key is configured

## Write-back (opt-in)

`observe_enabled` ships **`false`**. Turned on, each turn you finish becomes a record in Attacca
memory, so `memory_recall` in this session and a chat in Attacca a few minutes later can find what
you settled here.

**What is sent.** Each completed turn is the pair (your prompt, Claude's final reply) and nothing
else. No file contents, no tool output, no terminal history: the hook reads only the prompt it was
given and `last_assistant_message` from the `Stop` event. The text is passed through the server's
redaction before it is stored, so a credential pasted into a prompt is masked on the server rather
than trusted to every client.

**How a turn is queued.** `Stop` fires when Claude finishes a response, so that is where a turn pair
is settled. The pair is appended to `${CLAUDE_PLUGIN_DATA}/queue/<session>.jsonl` with a
client-generated ULID `turn_id` on each half. The file *is* the queue: the send happens later, and a
turn survives a crashed session.

**Why `Stop` and not `SessionEnd`.** Two reasons, both from the hooks reference:
- `Stop` fires at the end of *every* response, so a turn is on disk while the session is still
  alive. A `SessionEnd`-only design loses everything when a session is killed or a laptop closes.
- A plugin's `SessionEnd` hook runs on a shared **1.5-second** budget that a plugin cannot raise
  (only the user's own settings can raise it), and a turn is worth more than what fits in it. A
  `SessionEnd` hook still makes a best-effort flush of the remaining queue, but it is not where a
  turn is recorded.

Both write hooks are declared `"async": true`, so Claude Code starts them and carries on: the
response you are waiting for is never held open by this plugin. Async hooks cannot block or decide
anything, and nothing is printed on these paths (Claude Code would read stdout of an async hook as
hook output).

**Batching.** Each `Stop` checks the queue against a boundary:

| Constant (env name) | Default | Meaning |
|---|---|---|
| `ATTACCA_BATCH_TURNS` | `6` | turns per batch; a pair is two turns, so three prompts |
| `ATTACCA_BATCH_MAX_AGE_SECS` | `600` | send an undersized batch once its oldest pair is this old |
| `ATTACCA_QUEUE_MAX_PAIRS` | `200` | cap on queued pairs; the oldest are dropped past it |
| `ATTACCA_OBSERVE_DEADLINE_SECS` | `120` | the observe request's own budget |

At the boundary the oldest six turns go to `POST /v1/memory/observe` with `flush: true`, so the call
that records them also distils them into an episode. The 120-second budget is deliberate:
`flush: true` runs a consolidation pass, which is a model call, and a normal socket timeout would
abandon a request the server was still working on.

**Exactly once, without a transaction.** A batch leaves the queue only after the server has answered
for it. A failure (network, 5xx, 429, timeout) leaves every line in place, and the next boundary
sends the same turns again under the same `turn_id`s; the server's ledger takes them as `skipped`
instead of recording a second copy. A 4xx that cannot improve (400, 401, 403, 404, 422 — a key
without `memory:write`, a malformed turn) drops that batch and writes the reason to stderr, because
resending it forever would only fill the queue.

**Turning it off per repository.** A repository can refuse whatever the user setting says by adding
`.claude/attacca.toml` at its root:

```toml
# This repository stays out of Attacca memory.
observe = false
```

The hook looks for that file from the session's working directory up to the filesystem root, so a
session started in a subdirectory still finds it. With it in place **no outbound call is made and
nothing is written to the queue**: the turn is not recorded locally either, because a local copy of
what an opted-out repository said is exactly what the opt-out is about.

**Known limitation: headless sessions.** In `claude -p`, Claude Code kills an async hook still
running at teardown. The queue write is fast and usually lands, but the flush may not, so a `-p` run
can leave turns queued under a session nothing will flush. Interactive sessions do not have this
problem, because the hooks run while the session lives.

**Deciding whether to turn it on by default.** Write-back ships off so the read path is measured
first. The procedure to revisit that:
1. Collect at least two weeks of read-only installs, with `/metrics` (`memory_recall_*`,
   `memory_observe_turns`) and the app's memory pipeline page as the record.
2. Turn `observe_enabled` on for internal accounts only, and compare
   `memory_recall_nonempty_ratio` before and after: write-back should raise it.
3. Watch `memory_observe_turns` against the daily turn cap and the consolidation backlog. If
   episodes pile up, the batch size or the cap is wrong, not the switch.
4. Only then change the plugin's default, recording the before/after numbers in the commit that
   flips it.

## Privacy

**Two parties see your session.** The prompt text you type goes to Anthropic (as it always does) and
to the Attacca instance configured in `attacca_url` (by default, ours). In v0.1 that is the whole
traffic: the prompt and nothing else. Your code, file contents and terminal output are **not** sent,
because v0.1 has no write path and no tool that reads files. A later version that sends session
activity to Attacca is opt-in (`observe_enabled`, default `false`) and will be documented when it
ships.

Attacca stores what you send it under your own account, and the memory recalled back is yours alone.
Privacy policy: <https://attacca.cc/privacy>.

## Configuration

Claude Code exports every `userConfig` option to hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>`,
which is what the hook script reads (plugin hooks cannot substitute `${user_config.*}` in a
shell-form command). The script also accepts these tuning variables:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_PLUGIN_OPTION_ATTACCA_URL` | `https://attacca.cc` | Attacca base URL, no trailing slash |
| `CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN` | unset | API key with `memory:read` |
| `CLAUDE_PLUGIN_OPTION_RECALL_ON_PROMPT` | `true` | recall before each prompt |
| `CLAUDE_PLUGIN_OPTION_PROJECT_ID` | unset | reserved; the read surface accepts and ignores it until project binding ships |
| `CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED` | `false` | reserved for the read-write version |
| `ATTACCA_START_BUDGET_TOKENS` | `1200` | graph budget for the session-start recall |
| `ATTACCA_PROMPT_BUDGET_TOKENS` | `800` | graph budget for the per-prompt recall |
| `ATTACCA_START_DEADLINE_SECS` | `2` | session-start deadline |
| `ATTACCA_PROMPT_DEADLINE_SECS` | `4` | per-prompt deadline |
| `ATTACCA_CIRCUIT_BREAKER_SECS` | `60` | how long a failure silences the hooks |
| `ATTACCA_DEBUG` | unset | `1` writes diagnostics to stderr |

The hook script is Python 3, standard library only. `hooks/hooks.json` runs it as `python3`; on a
machine where `python3` is not on `PATH` (some Windows installs) the hook cannot start — that shows
up as a hook error notice and nothing else, and will move to a Node single file if Windows turns out
to matter.

## `/attacca:recall` and `/attacca:status`

Both commands use the plugin's MCP server (`${attacca_url}/v1/mcp`, declared in `.mcp.json`). That
server ships with the read tools in a later release; until then the commands explain that the MCP
read surface is not available on your instance, and the hooks above keep working on their own.

## Hook output schema, as implemented

Taken from the official reference at <https://code.claude.com/docs/en/hooks> at implementation time
(2026-11-23), not from memory:

- Context is added through `hookSpecificOutput` with the event name:

  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": "Current branch: feat/auth-refactor\n..."
    }
  }
  ```

  `UserPromptSubmit` uses the same shape with `"hookEventName": "UserPromptSubmit"`.
- `additionalContext` is capped at **10,000 characters** per string; longer output is replaced with a
  file path plus a 2,000-character preview, so this plugin truncates itself at 10,000.
- Exit **0** with no output means "no decision, add nothing", which is the fail-open path.
- `UserPromptSubmit` command hooks default to a **30 s** timeout; `SessionStart` and other command
  hooks default to **600 s**. `hooks/hooks.json` sets `timeout: 10` as a backstop above the script's
  own deadline.
- `SessionStart` matcher values are `startup`, `resume`, `clear`, `compact`, `fork`.

## Development

```
python3 -m unittest discover -s tests -v   # 30 tests, no network
claude plugin validate .                   # manifest, hooks, .mcp.json
```

The tests cover the skip rules, truncation at the 10,000-character cap, the hard deadline, the
fail-open paths and the circuit breaker. The last fail-open test runs the script as a subprocess
against a closed port and asserts exit 0 with empty stdout.

## Status

- Read-only recall: working.
- Write-back (`observe`, six-turn batches, repository opt-out): working, opt-in (`observe_enabled`,
  default `false`).
- MCP read tools (`memory_recall`, `memory_search`, `memory_status`): declared, landing with
  `/v1/mcp`.
- `notes` / `/attacca:remember`: not in this version.
- License: not chosen yet; the repository is public without one until the owner sets it.
