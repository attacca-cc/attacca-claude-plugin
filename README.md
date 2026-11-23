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

Both hooks call `POST /v1/memory/recall` on your Attacca instance with a `cc:<session_id>` external
session id, and put the rendered block into Claude's context.

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
- MCP read tools (`memory_recall`, `memory_search`, `memory_status`): declared, landing with
  `/v1/mcp`.
- Write-back (`observe`, `notes`): not in this version.
- License: not chosen yet; the repository is public without one until the owner sets it.
