---
description: Remember something in Attacca memory, so it comes back in later sessions
argument-hint: <the sentence to remember>
allowed-tools: mcp__plugin_attacca_attacca__memory_remember
---

Remember this in Attacca memory: $ARGUMENTS

Call the `memory_remember` MCP tool **once**, with `statement` set to "$ARGUMENTS" exactly as the
user wrote it (do not rephrase, summarise or translate it — the sentence they gave is the memory),
and then report back in one short block:

- **Stored**: the sentence, as the tool returned it in `statement`. If `redacted` is `true`, say that
  a credential in it was masked before it was saved, and show the masked sentence rather than the
  original.
- **Kind**: the `kind` that was recorded (`note` unless you passed another).
- **New or repeated**: `created: true` means this is a new memory; `created: false` means the same
  sentence was already remembered and this call refreshed it. Say which, plainly — do not report a
  repeat as a second memory.
- **Where it applies**: an account note (no `project_id`) is recalled in every session; say that.

If the tool refuses because the key lacks the `memory:write` scope, say so and point at the README's
"Getting the API key" section: the user needs a key with `memory:write`, and this is not something
you can grant. If the tool is unavailable at all (the MCP server is not connected), say that and
suggest `/attacca:status` rather than pretending it was saved.

Keep the reply to those few lines: the user wants to know it was kept, not to read the payload.
