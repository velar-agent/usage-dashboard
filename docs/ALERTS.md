# Token usage pipeline — alert rules (approved by user 2026-11-15)

All issue alerts go ONLY to "token usage service manager" (id 23f8fbe7-0e0a-426b-b3db-8aca5116cd5f)
via one priority SendToAgent message. Logging and gathering bots never message the user about issues
or pipeline health; the manager alone tells the user.

## Who reports what
- token usage gethering bot (93a8f107-8126-4808-916b-57612e939450): any service that fails to collect
  (FAIL line, missing file, or collect-all.sh breaks).
- token usage logging bot (9988c596-89c6-4e72-96c5-194c2c70cecb): log_raw.py errors it can't fix,
  or the last 2 collection requests produced no new raw files.

## Dedupe
- One message per new (service, problem_key). Same open problem: stay silent (logging bot: at most once per 3h).
- Worsened severity counts as new. Send ONE `status: recovered` message when it clears.

## Format (plain text body)
[TOKEN-USAGE-ALERT]
severity: warning | error | critical
status: open | recovered
source: token usage logging bot | token usage gethering bot
source_id: <uuid>
time: <ISO 8601, +09:00>
service: grok | codex | claude | antigravity | muse | pipeline
problem_key: e.g. collect_fail:claude:401, log_script_error, no_new_raw_after_2_requests
summary: <한국어 한 줄>
detail: <what you saw, error text>

Severity: warning = one service failed / data stale; error = script broke or 2 collect requests
produced no new raw; critical = several services down or processed/ can't be written.

## Manager handling
- No periodic checks. Act only on inbound alerts (tolerate free text).
- Dedupe by (source, service, problem_key): new or worsened -> one short Korean SendToUser
  (what broke, which bot, since when, what the user may need to do); unchanged -> silent;
  recovered -> one short "복구됨" note.
- Don't re-paste the raw alert (it's already visible in the chat); summarize.
- Track open issues in /workspace/token-usage/alerts-state.json.
