---
description: Recall what Attacca remembers about a topic or question
argument-hint: <what to recall>
allowed-tools: mcp__plugin_attacca_attacca__memory_recall
---

Ask Attacca memory what it knows about: $ARGUMENTS

Call the `memory_recall` MCP tool once with `message` set to "$ARGUMENTS" and `session_id` set to
`cc:` followed by the current session id from the hook context (if you do not have it, use
`cc:${CLAUDE_SESSION_ID}`). Then answer in this shape:

1. **What memory says** - the rendered block, quoted or closely paraphrased, not restated as your
   own opinion.
2. **Where it comes from** - the claims' `source_path` and the nodes' names, when the answer
   leans on them.
3. **What memory does not cover** - if `rendered` came back empty, say that plainly: recall is
   diff-only per session, so an empty answer means "nothing new for this session", and
   `diagnostics.why` explains which case it is.

Do not invent memory. If the tool is unavailable (the MCP server is not connected), say so and
suggest `/attacca:status`.
