# Attacca Memory for Claude Code

Bring your [Attacca](https://attacca.cc) long-term memory into Claude Code. What Attacca already
knows about you, your projects and the decisions you have settled is recalled and injected as
context at session start and before each prompt, so you do not start every session by explaining
yourself again.

This is v0.2: the read path is on by default, and the **write path is opt-in**. It recalls memory
and shows you what Attacca knows; with `observe_enabled` it also sends each completed turn back to
Attacca in six-turn batches, so the next session (and Attacca chat) can recall what you settled here.
Write-back ships **switched off**.

The full guide, including what leaves your machine and how to switch each part off, is at
<https://docs.attacca.cc/docs/claude-code>.

## Install (three commands)

Inside a Claude Code session:

```
/plugin marketplace add attacca-cc/attacca-claude-plugin
/plugin install attacca@attacca-claude-plugin
/plugin configure attacca@attacca-claude-plugin
```

The third command is where the plugin gets your settings: leave **Attacca URL** at `https://attacca.cc/api`
unless you self-host, and paste your API key into **Attacca API key** (the option is `sensitive`, so it
is stored as a credential rather than in plain text). `claude plugin install` from a terminal instead
of a session prints the same instruction as `5 userConfig options not yet set (1 required) — run
/plugin configure attacca@attacca-claude-plugin`. Then start a new session (`/clear` is enough) so the
hooks pick the key up, and run `/attacca:status` to see how many nodes Attacca holds for your account.

**The hooks work wherever the key does; the two slash commands need the MCP route.** `/attacca:status`
and `/attacca:recall` call the plugin's MCP server at `${attacca_url}/v1/mcp`, and that route is only
mounted on an instance that sets `MEMORY_MCP_ENABLED`. `https://attacca.cc/api/v1/mcp` answers 404 as
of 2026-11-24, so an install that leaves the default URL gets the recall hooks and a `/attacca:status`
that reports the server as unreachable. Point **Attacca URL** at an instance with the flag on (ours:
`https://staging.attacca.cc/api`) to use them. That is where v0.2 stands, not a misconfiguration on
your side.

### Getting the API key

1. Sign in at <https://attacca.cc> and open **Settings → API keys**.
2. Create a key with the **`memory:read`** scope. Add **`memory:write`** only if you turn on
   `observe_enabled`; a key without it drops observe batches instead of failing a prompt (see
   [Write-back](#write-back-opt-in)).
3. Copy the key once (it starts with `atk_`; Attacca shows it only at creation).

## Verified on a clean machine (2026-11-24)

Installed from scratch into an empty `HOME` (no `~/.claude`, no Claude Code state) with the three
commands above, against `https://staging.attacca.cc/api` with a fresh `memory:read` + `memory:write`
key. Plugin 0.2.2, Claude Code 2.1.330.

- `/plugin marketplace add` took 17 s (cold clone), `install` 4 s, `configure` under 1 s.
- `claude plugin list` reports `attacca@attacca-claude-plugin 0.2.2 — enabled`. The token is stored
  in `~/.claude/.credentials.json` (mode 0600) under `pluginSecrets`, not in `settings.json`.
- `claude mcp list` reports `plugin:attacca:attacca: https://…/api/v1/mcp (HTTP) - ✔ Connected`.
- A real session (headless `claude -p`, pointed at a stub Messages API so no model account was
  needed) carried the recalled block into its first request, and the deferred-tool reminder listed
  the three MCP tools by their real names.

**What the numbers are, and what they are not.** The recall call itself is fast; the hook's wall
time is dominated by starting a Python process on this machine.

| Measurement | n | p50 | p95 | max |
|---|---|---|---|---|
| `POST /v1/memory/recall` wall (client, `budget_tokens: 800`) | 30 | 620 ms | **799 ms** | 924 ms |
| same request, `diagnostics.elapsed_ms` (server-side engine) | 30 | 165 ms | 204 ms | 319 ms |
| `SessionStart` hook wall, fresh session each time | 30 | 3.0 s | 7.7 s | 8.4 s |
| `UserPromptSubmit` hook wall, 30 distinct prompts | 30 | 5.4 s | 8.4 s | 8.6 s |
| `python3 -c pass` on the same host | 5 | 0.8 s | 2.8 s | 2.8 s |
| `python3 -c 'import urllib.request'` on the same host | 5 | 4.0 s | 4.8 s | 4.8 s |

A second, interleaved set of 10 runs per hook measured `SessionStart` p50 5.1 s and
`UserPromptSubmit` p50 5.4 s, so read the hook rows as "several seconds, varying with host noise".

So the per-prompt recall meets its p95 ≤ 800 ms target at the surface (799 ms) and by a wide
margin inside the engine (204 ms). The 2 s SessionStart budget is **not** met on this host: starting
the interpreter and importing the standard library already costs ~4 s here (a virtiofs home volume;
the same host runs a hello-world `rustc` in 8-57 s), and running the identical script from the
container filesystem instead of the home volume did not help (p50 5.7 s), which is the evidence that
the cost is process start rather than the plugin's own I/O.

### On ordinary hardware the 2 s budget holds

`tests/bench_hooks.py` runs the hooks the way Claude Code does: one fresh interpreter per invocation,
the event on stdin, the block on stdout, against a stub that spends 150 ms per recall (the engine's
own p50). Thirty runs per event, on a desktop and in CI:

| Measurement | Host | n | p50 | p95 | Budget |
|---|---|---|---|---|---|
| `SessionStart` hook wall | Windows 11, Python 3.14 | 30 | 272 ms | **294 ms** | 2 s |
| `UserPromptSubmit` hook wall | Windows 11, Python 3.14 | 30 | 273 ms | **287 ms** | 4 s |
| `SessionStart` hook wall | ubuntu-latest (CI) | 30 | 216 ms | **217 ms** | 2 s |
| `UserPromptSubmit` hook wall | ubuntu-latest (CI) | 30 | 215 ms | **216 ms** | 4 s |
| an account with no memory | both | 2 | no output, exit 0 | | |

The Ubuntu rows come from the `bench` job, which runs on every push and fails the build when a p95
crosses its budget, so the budget is enforced rather than quoted:
[the runs](https://github.com/attacca-cc/attacca-claude-plugin/actions/workflows/ci.yml). The hook
costs about 120 ms around the server's own latency (272 ms wall against a 150 ms stub), so the 2 s
budget has room on hardware a user actually has, and the VM rows above are the artefact.

The two budgets the *script* owns were verified directly against a server that accepts the
connection and then sleeps: `SessionStart` abandons the call at 2.0 s and `UserPromptSubmit` at
4.0 s, both exit 0 with empty stdout (`[attacca] … DeadlineExceeded` on stderr with
`ATTACCA_DEBUG=1`), and the circuit breaker records the failure.

**Token size of the injected block.** Counted with `tiktoken`'s `cl100k_base` (`tiktoken 0.14.0`), which
is a proxy for Anthropic's tokenizer rather than the same one, so read these as ±a few percent:

| Block | n | p50 | p95 | max | min |
|---|---|---|---|---|---|
| `SessionStart` profile block | 10 | **738** | 738 | 738 | 738 |
| per-prompt block | 30 | **470** | **544** | **752** | 0 |

Both are now under their budgets (1,200 and 800). Getting there cost something, and the history is
worth keeping:

- **The request budget did not bound the block.** It bounds the nodes the server selects; the
  rendered text adds a header and one line per relation (`- name [type]: ...`,
  `subject --relation--> object`) outside it. Against `budget_tokens: 800` the per-prompt block
  measured 977 tokens at the median and 1,300 at p95, and the profile block measured 1,247 against
  1,200.
- **So the hook enforces the number on the text it injects.** `cap_to_token_budget` cuts a block that
  passes `budget x 2.5` characters at the last whole line and ends it with
  `[attacca: trimmed to fit the token budget]`.
- **2.5 characters per token is deliberately conservative.** The measured ratio ranged from 2.6 to
  3.8 characters per token across blocks, so the cap can stop short of the budget: the profile lands
  at 738 of its 1,200. Sizing the render from the actual rendered text is the real fix and belongs to
  the server; this is the hook keeping its own promise until then.
- **A node the cut dropped is not recorded as injected**, so a later recall can still offer it. A
  ledger that counted dropped nodes would quietly lose them for the rest of the session.

**Hit rate.** One session, 24 distinct prompts: 21 returned a block and 3 returned nothing, so
`memory_recall_nonempty_ratio` = 21/24 = **0.875** for a read-only install on an account with
history (25/30 = 0.83 counting six further prompts in fresh sessions). In all of them a request that
came back empty printed nothing and exited 0. An empty block is a normal
answer (the recall is diff-only per session, and a prompt that shares no term with memory gets
nothing), not a failure, so the write-back default should be judged against this baseline rather than
against "should always inject".

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
- the repository's `.claude/attacca.toml` sets `observe = false` (no outbound call at all)

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
instead of recording a second copy. A 4xx that cannot improve (400, 401, 403, 404, 413, 422 — a key
without `memory:write`, a malformed turn) drops that batch and writes the reason to stderr, because
resending it forever would only fill the queue.

**Turning it off per repository.** A repository can refuse whatever the user setting says by adding
`.claude/attacca.toml` at its root:

```toml
# This repository stays out of Attacca memory.
observe = false
```

The hook looks for that file from the session's working directory upwards (up to twelve parents), so
a session started in a subdirectory still finds it. With it in place the plugin makes **no outbound
call at all** and writes nothing to the queue: the per-prompt recall, the `SessionStart` profile
recall and the turn recording are all skipped. The cost is deliberate: an opted-out repository gets
no account profile injected either, because a repository that says it stays out of Attacca memory
should not have its sessions announce themselves to Attacca at all.

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
to the Attacca instance configured in `attacca_url` (by default, ours at `https://attacca.cc/api`).

With `observe_enabled` on, each completed turn pair also goes to Attacca, batched six turns at a
time: your prompt and Claude's final reply for that turn. Code, file contents and terminal output are
**not** sent, because the hook reads only the prompt and `last_assistant_message`. Text is redacted on
the server before it is stored. Turning `observe_enabled` off, or adding `.claude/attacca.toml` with
`observe = false`, stops the traffic.

Queued turns sit as plain text under `${CLAUDE_PLUGIN_DATA}/queue/` (mode `0600`) until they are
accepted. They are your files on your own machine: deleting that directory discards an unflushed
batch.

Attacca stores what you send it under your own account, and the memory recalled back is yours alone.
Privacy policy: <https://attacca.cc/privacy>.

## Configuration

Claude Code exports every `userConfig` option to hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>`,
which is what the hook script reads (plugin hooks cannot substitute `${user_config.*}` in a
shell-form command). The script also accepts these tuning variables:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_PLUGIN_OPTION_ATTACCA_URL` | `https://attacca.cc/api` | Attacca base URL including the `/api` prefix, no trailing slash |
| `CLAUDE_PLUGIN_OPTION_ATTACCA_TOKEN` | unset | API key with `memory:read` |
| `CLAUDE_PLUGIN_OPTION_RECALL_ON_PROMPT` | `true` | recall before each prompt |
| `CLAUDE_PLUGIN_OPTION_PROJECT_ID` | unset | reserved; the read surface accepts and ignores it until project binding ships |
| `CLAUDE_PLUGIN_OPTION_OBSERVE_ENABLED` | `false` | send completed turns back to Attacca |
| `ATTACCA_START_BUDGET_TOKENS` | `1200` | graph budget for the session-start recall |
| `ATTACCA_PROMPT_BUDGET_TOKENS` | `800` | graph budget for the per-prompt recall |
| `ATTACCA_START_DEADLINE_SECS` | `2` | session-start deadline |
| `ATTACCA_PROMPT_DEADLINE_SECS` | `4` | per-prompt deadline |
| `ATTACCA_CIRCUIT_BREAKER_SECS` | `60` | how long a failure silences the read hooks |
| `ATTACCA_BATCH_TURNS` | `6` | turns per observe batch |
| `ATTACCA_BATCH_MAX_AGE_SECS` | `600` | age that forces an undersized batch out |
| `ATTACCA_QUEUE_MAX_PAIRS` | `200` | cap on queued turn pairs |
| `ATTACCA_OBSERVE_DEADLINE_SECS` | `120` | observe request budget |
| `ATTACCA_DEBUG` | unset | `1` writes diagnostics to stderr |

The hook script is Python 3, standard library only. `hooks/hooks.json` runs it as `python3`; on a
machine where `python3` is not on `PATH` (some Windows installs) the hook cannot start — that shows
up as a hook error notice and nothing else, and will move to a Node single file if Windows turns out
to matter.

## `/attacca:recall` and `/attacca:status`

Both commands use the plugin's MCP server (`${attacca_url}/v1/mcp`, declared in `.mcp.json`). The
server name inside Claude Code is namespaced by the plugin, so the tools are really called:

| Tool | Real name |
|---|---|
| recall | `mcp__plugin_attacca_attacca__memory_recall` |
| search | `mcp__plugin_attacca_attacca__memory_search` |
| status | `mcp__plugin_attacca_attacca__memory_status` |

Note it is `mcp__plugin_attacca_attacca__…`, not `mcp__attacca__…`: the middle segment is the
plugin and the last is the server. Both `commands/*.md` files already use the fully qualified names
in their `allowed-tools`, and the skill refers to them by the bare tool name because that is how a
model reads them.

Claude Code 2.1.330 lists these three as **deferred tools**: their names arrive in a
`<system-reminder>` at session start, and their schemas have to be fetched with `ToolSearch`
(`query: "select:mcp__plugin_attacca_attacca__memory_status"`) before a call is possible. A direct
call without that step fails with `InputValidationError`. This costs nothing in the prompt until a
tool is used, and it is why `/attacca:status` works even though no tool schema is loaded initially.

The read surface is live on an Attacca instance that sets `MEMORY_MCP_ENABLED`. On an instance
without it `/v1/mcp` is a 404 and the commands say so; the hooks above keep working either way. As of
2026-11-24 our own production instance is one of the ones without it, so the default URL installs get
the hooks and not the slash commands.

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
python3 -m unittest discover -s tests -v   # 75 tests, no network
claude plugin validate .                   # manifest, hooks, .mcp.json
```

The tests cover the skip rules, truncation at the 10,000-character cap, the hard deadline, the
fail-open paths and the circuit breaker. The last fail-open test runs the script as a subprocess
against a closed port and asserts exit 0 with empty stdout. `tests/test_manifests.py` is the
cross-check CI runs on its own: every `${user_config.x}` reference in `.mcp.json`/`hooks.json` is
declared in `plugin.json`, every `CLAUDE_PLUGIN_OPTION_*` the script reads is a declared option,
`hooks.json` points at files that exist, and the marketplace serves the plugin from its root.
`claude plugin validate` does not check any of that — it validates each manifest against its own
schema.

## Status

- Read-only recall: working, measured on a clean install (see above).
- Write-back (`observe`, six-turn batches, repository opt-out): working, opt-in (`observe_enabled`,
  default `false`).
- MCP read tools (`memory_recall`, `memory_search`, `memory_status`): live against an instance with
  `MEMORY_MCP_ENABLED`; exposed as `mcp__plugin_attacca_attacca__*` and loaded through `ToolSearch`.
- `notes` / `/attacca:remember`: not in this version.
- License: not chosen yet; the repository is public without one until the owner sets it. Note that
  `claude plugin validate . --profile directory` (the stricter profile used for a marketplace
  submission) fails on `LICENSE_MISSING` until this is settled, and warns about a missing icon.
