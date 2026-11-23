---
description: Show how much long-term memory Attacca holds for this account
allowed-tools: mcp__plugin_attacca_attacca__memory_status
---

Call the `memory_status` MCP tool and report, in a short block:

- **Nodes**: how many live nodes the account's memory graph holds.
- **Claims**: live claims in scope. For a Claude Code session this is usually `0`, because claims are
  scoped by project and an external `cc:` session has no project binding yet. Say that rather than
  reporting zero claims as if the account had no claims at all.
- **Last consolidation**: when the last deep consolidation pass ran, or that one has not run yet.
- **External sessions**: how many `cc:` sessions the serving process currently holds a ledger for.
  This is process-local, so `0` on a freshly started server is expected.

Then one line on what it means: whether the automatic recall in this session has anything to work
with, or whether the account's memory is still empty and the user should talk to Attacca first.

If the tool is unavailable (the MCP server is not connected), say so and point at the README's
configuration section instead of guessing at numbers.
