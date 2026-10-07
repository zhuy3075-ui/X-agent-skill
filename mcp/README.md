# Scweet monitoring MCP

This folder provides a PostgreSQL data service over MCP stdio or authenticated Streamable HTTP,
an independent collector, and one Chinese `x-agent-skill`. The service exposes 31 tools and uses schema 3.

## Install and run

The standalone [X-agent-skill distribution](https://github.com/zhuy3075-ui/X-agent-skill) includes the
required Scweet runtime. From that checkout, run `python install.py`. Configure PostgreSQL in `.env`,
then use the installed virtual environment: `python run.py init-db` followed by `python run.py worker`.

The installer writes `mcp-client.local.json` for stdio clients. It can register Codex if the CLI is present.
Start HTTP with `python run.py mcp --transport streamable-http` and a random
`SCWEET_MCP_API_TOKEN` of at least 32 characters. Protect external access with HTTPS.

In the parent Scweet development checkout, install with `python -m pip install -e . -r mcp/requirements.txt`.
Pass `SCWEET_MCP_DATABASE_URL` to both processes, and use these direct entry points:

```bash
python mcp/server.py --config mcp/config.local.example.json --init-db
python mcp/worker.py --config mcp/config.local.example.json
python mcp/server.py --config mcp/config.local.example.json
```

Direct entry points read process environment, not `.env`. The distribution's `run.py` loads `.env`
without overriding process environment. An API process does not collect from X or need raw X tokens.

## Credentials

Use `python mcp/credentials.py --file /absolute/private/worker-env.json --credential-ref SCWEET_X_ACCOUNT_A`.
The helper reads hidden input and writes the private JSON atomically. Set `mcp_credentials_file` to the
same file in the local runtime config. Register only the reference through `accounts_register`, then
check it through `accounts_check`. Multiple checked accounts support bounded page failover.
Do not put token values in tool arguments, source, skill preferences, reports or Git.

## Contracts and workflow

- [MONITORING.md](MONITORING.md): tools, result structures, PostgreSQL roles and deployment.
- [RESEARCH.md](RESEARCH.md): parameterized collection, profiles, material evidence and growth.
- [RECOVERY.md](RECOVERY.md): retry, cooldown, account rotation, comment continuation and archives.
- [HOURLY.md](HOURLY.md): hourly collection and on-demand reporting.
- [x-agent-skill](skills/x-agent-skill/SKILL.md): intent recognition, automatic parameters and reports.

MCP validates input, writes durable jobs and serves queries. The worker claims jobs, collects pages,
commits observations and checkpoints together, and retries within explicit budgets. Both processes
share PostgreSQL and the artifact directory on one host. PostgreSQL stores JSONB, indexes, canonical
author archives, comments, content versions, metric snapshots and operation history. Existing library
account state remains in SQLite; it is not the monitoring data store.

Create a monitor only for explicit continuous-monitoring intent. Default discovery runs hourly;
stored-post checks rotate in bounded batches. New posts, content edits and availability changes are
recorded. Three reliable missing observations spaced by at least an hour mean suspected deletion,
not confirmed deletion. The agent reports only when asked. No automatic push or model task is needed.

## Verify

In the distribution, run `python scripts/check_service.py --config config.json`. It opens a new stdio
session, discovers 31 tools and reads monitor state. It does not create jobs or collect from X.
Fixture tests require a separate disposable database. Never run them against retained user data.
An exhaustive comment result means exhausted visible pagination within the selected scope; hidden,
deleted and protected comments remain outside that claim. One snapshot cannot establish growth.

The source uses the MIT license. The distribution retains the upstream Scweet copyright.
