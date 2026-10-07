# MCP data service

This directory holds the PostgreSQL social monitoring service with stdio and authenticated HTTP.
Do not add `__init__.py` here: it would hide the installed `mcp` SDK.
`scweet_mcp/` holds the importable service modules. `server.py` starts the service.
`worker.py` is an independent collector. PostgreSQL jobs are the API/worker interface.
Both processes share an artifact directory. `MONITORING.md` and `RESEARCH.md` document all 31 tools.
`collection.py` owns parameterized author jobs; `research.py` owns growth and material evidence.
`skills/x-agent-skill/SKILL.md` defines agent orchestration and evidence-based interpretation.
Its `config.json` holds agent preference defaults. Preserve the installed personal copy on skill updates.
The agent resolves natural-language requests into tool parameters; the service does not read skill config.
All runtime settings belong to `ScweetConfig` in `Scweet/config.py`.

- Store complete normalized tweet objects. Never store account credentials.
- Use PostgreSQL JSONB for collected data. The library account database stays in SQLite.
- Read the PostgreSQL connection string from SCWEET_MCP_DATABASE_URL. Never log it.
- Use a tuple cursor over sort_time and tweet_id. Do not use OFFSET pagination.
- Write a batch and its search index in one transaction.
- Commit page observations, checkpoints and events together under the job lease token.
- Keep missing metrics null and incomplete comment coverage explicit.
- Store account environment references, never X token values. Keep the GUI account database unchanged.
- A private credential file can override environment values per request. Tokens never enter MCP arguments.
- One-time author jobs prefer the selected account and can fail over to the checked pool. Budget-limited results are partial.
- One observation cannot show growth; missing metrics and zero-baseline ratios remain null.
- Reuse captured GraphQL fixtures; fixture mode must not access X or mark accounts valid.
- Send logs to stderr. Stdout belongs to the MCP protocol.
- Test with captured records and temporary databases. No X account is needed.
- In this distribution, run `python scripts/check_service.py --config config.json` for read-only verification.
- Database tests need SCWEET_MCP_TEST_DATABASE_URL pointing to a disposable PostgreSQL database.

- Schema 2 adds canonical numeric author archives and category/history links; do not copy collected payloads.
- Comment continuations inherit post, scope and cursor queues; only one successor per canonical parent ID.
- Source retries use bounded exponential backoff. Account-only waits do not consume source attempts.
- Never cool an account merely because one target is forbidden or a partial GraphQL response has errors.

## Hourly monitoring and on-demand reports

The optional MCP now requires schema 3 and exposes 31 tools, including query_post_changes.
Monitors require explicit user intent, default to hourly discovery and bounded lookup rotation, and do not push reports.
The worker scans schedules every 60 seconds and checks pause state before each request.
Reliable missing lookup responses mark unavailable, then suspected_deleted after three hourly observations;
errors and unknown response shapes do not count. Preserve saved content and never claim confirmed deletion.
See mcp/HOURLY.md for scope, defaults, interfaces and local update steps.

This release uses one unified x-agent-skill, install.py and run.py. Never publish credentials or collected data.
