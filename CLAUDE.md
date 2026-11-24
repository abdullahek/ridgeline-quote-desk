# Project Instructions

## Chat History Logging (mandatory, every session)

### 1. At session start
- Before responding to the first prompt of a session, read `.chat-history/log.md` to load previous context.
- If the file or the `.chat-history/` folder does not exist, create them (start the log with a `# Chat History Log` heading).

### 2. After every response
Append exactly one entry to `.chat-history/log.md` for each prompt/response pair, using this exact format:

```
---
- timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<summary of what you generated or answered for this prompt>"
- files_affected: "<comma-separated list of files created or modified, or none>"
```

### 3. Rules
- **Append only.** Never delete, rewrite, or reorder previous entries. Create the file/folder if missing.
- **Do it silently.** Never ask for confirmation before reading or writing the log, and don't mention the logging in responses unless asked.
- **Never skip an exchange.** Every prompt/response pair must be logged, including short questions, follow-ups, and failed or declined tasks.
- **files_affected must be precise.** List only files explicitly created or modified during that response (relative paths). Do not count `.chat-history/log.md` itself. Use `none` if nothing changed.
- **assistant_response_summary is concise but specific.** Mention function names, endpoints, components, commands run, and key decisions made.
- **user_prompt** is the user's original prompt; for very long prompts, keep the opening and summarize the rest in brackets, e.g. `[...pasted 200-line stack trace]`.
- **Never write secrets to the log.** Replace API keys, tokens, passwords, connection strings, private keys, and `.env` values with `***` in every field, including `user_prompt`.
- Escape double quotes inside field values (`\"`) so each field stays on one line.
- Use the current time for `timestamp` (e.g. via `Get-Date -Format o` in PowerShell) when available; otherwise estimate from conversation order.
