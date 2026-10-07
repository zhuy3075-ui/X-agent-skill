# Parameterized author collection and research

This workflow uses the existing agent, one MCP service, one independent worker and PostgreSQL.
The API and worker run on the same host. They can run on this Windows computer or on the later
server. The agent accepts the user's research intent, translates dates and budgets, calls tools,
and writes an evidence-based interpretation. No model API key or second model service is needed.

## Responsibilities and contracts

| Module | Input | Responsibility | Output |
| --- | --- | --- | --- |
| Agent with x-agent-skill | Target, user timezone, dates, account and budgets | Infer ordinary defaults, clarify ambiguous targets, orchestrate tools, interpret evidence | Status report, author profile, cited material shortlist and topic suggestions |
| `credentials.py` | Private file, credential reference, token from hidden prompt/stdin | Validate format and atomically replace a restricted credential entry | Reference and saved flag; no token echo |
| `contracts.py`, `tools.py`, `service.py` | Typed MCP arguments | Reject invalid fields; enqueue idempotent jobs; serve queries | Receipt, safe tool error, or structured data |
| `collection.py`, `worker.py` | PostgreSQL job and selected account | Preflight login; collect posts/comments; commit checkpoints; save report | Durable status, counts, coverage, result or safe failure |
| `source.py`, `accounts.py` | Target, page cursor, account ID | Resolve handles; apply account leases/quotas; call the existing Scweet engine | Normalized records, continuation cursors or explicit source errors |
| `observations.py`, PostgreSQL | One observed page | Deduplicate IDs, upsert current records, append snapshots and content versions | Indexed JSONB, history, comment coverage |
| `reports.py`, `research.py` | Stored author scope or post observations | Compute descriptive features, candidate materials and growth | Versioned evidence and saved analysis runs |

The simpler alternative is to let the agent chain the old monitor/comment tools. That remains
available, but lacks a single durable research-task result. A separate queue service was rejected:
it adds deployment, testing and maintenance cost for the same single-host workload. PostgreSQL
already supports leases, recovery and atomic page commits. Revisit the queue choice if measured
contention or multi-host requirements demand it. AI interpretation stays in the skill so a model
outage does not stop collection and a prompt change does not require a service deployment.

## Provide or replace a token

Set the non-secret worker configuration field `mcp_credentials_file` to a private JSON file.
Use the same file in the helper. For example:

```powershell
.venv/Scripts/python.exe mcp/credentials.py --file C:/path/X-agent-skill/private/worker-env.json --credential-ref SCWEET_X_ACCOUNT_A
```

Enter the token at the hidden prompt. An authorized local agent can use `--stdin` through a
secure input channel. Do not put the token in a CLI argument, repository, job, report or log.
The helper returns `{credential_ref,saved:true,restart_required:false}` or exits nonzero with
a safe cause. The worker rereads the file for each request, before it falls back to environment
values. An explicitly configured unreadable file is an error, not a silent fallback.

Register `{credential_ref:"SCWEET_X_ACCOUNT_A",label:"primary"}` with `accounts_register`.
Pass its returned account UUID to the collection tool. A new token can replace the same reference;
the next collection performs a new login/identity check. Multiple accounts use separate references.
Remote agents without host access ask the operator to provision a reference on the worker host.
The MCP does not expose a raw-token tool or retain credentials in PostgreSQL.

## Tools added for this workflow

The full service now exposes 31 tools. The existing tools and resource schemas are in
[MONITORING.md](MONITORING.md). These three additions are:

| Tool | Input | Output |
| --- | --- | --- |
| `collect_author` | `config` below, `request_id` string (1–200 characters) | `{job_id,status,submitted_at}`; poll `jobs_get` for completion |
| `query_growth` | `post_id` numeric string, `since`, `until`, optional `bucket:hour|day` (day) | `{post_id,since,until,bucket,timezone,observation_count,points,limitations}` |
| `query_analysis_runs` | `author_id` numeric string, optional `limit` (bounded by service, at most 100), `cursor` | `{records,count,has_more,next_cursor}` with report summaries and job IDs |

`collect_author.config`:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `account_id` | Required UUID | Registered account; used for every source request in this job |
| `target` | Required | Numeric X user ID, handle, @handle or HTTPS X profile URL |
| `since`, `until` | Required | Timezone-aware ISO timestamps; inclusive start, exclusive end for post publication |
| `max_posts` | Required, 1–10000 | Stop after the page that reaches this target; page overshoot is retained |
| `post_types` | `[original,quote]` | Nonempty list of original, quote, reply or repost |
| `include_comments` | true | Collect visible comments of selected posts |
| `max_comment_posts` | 20, 1–100 | Select this many posts in discovery order; other posts remain unscanned for comments |
| `comment_scope` | thread | direct replies or root conversation; use direct if post_types includes reply |
| `max_pages` | 100 | Combined post/comment page budget, capped by mcp_job_max_pages |
| `max_runtime_seconds` | 300 | Collection wall-clock budget across retries, capped by mcp_job_max_runtime_s |
| `timezone` | UTC | IANA timezone for cadence analysis |

Budgets are checked before pages. An in-flight request and bounded analysis may finish after the
collection deadline. Resolving a handle also costs a source request. Comments are selected by the
post scope; comment publication times are not filtered by the post date range. A completed run
does not create a recurring monitor. Use the existing monitor tools for periodic observations.

Example MCP call, after replacing the example UUID with the registration result:

```json
{
  "config": {
    "account_id": "00000000-0000-0000-0000-000000000001",
    "target": "@example",
    "since": "2026-09-01T00:00:00+08:00",
    "until": "2026-10-01T00:00:00+08:00",
    "max_posts": 100,
    "include_comments": true,
    "max_comment_posts": 20,
    "comment_scope": "thread",
    "max_pages": 50,
    "max_runtime_seconds": 300,
    "timezone": "Asia/Shanghai"
  },
  "request_id": "example-september-research-1"
}
```

Changing target, date range or account changes only arguments. Retransmission of the same intent
uses the same request ID; changed arguments with that ID return CONFLICT. A deliberate rerun after
token replacement uses a new request ID. Current records upsert by ID and new observations append.

## Results and errors

`jobs_get(job_id)` returns job_id, kind, status, timestamps, progress, result, error, attempts and
cancel_requested. Status is queued, running, succeeded, partial, failed or cancelled. A queued
retry includes the latest failure; it is not a completed failure or success. Poll with backoff.
If its waiting deadline ends, the agent explains in plain language that collection is still pending
and reports only verified progress. Job IDs and internal codes remain in diagnostics unless the user
asks to troubleshoot. The Chinese skill defines the user-facing result and failure wording.

A collection result contains:

```text
author_id, account_id, source
counts: {posts, comments}                 # unique IDs observed in this run
progress: {pages, fetched, inserted, updated} # fetched can include repeated observations
effective_limits: {max_pages, max_runtime_seconds, max_posts, max_comment_posts}
coverage: {posts_complete_visible, comments: {post_id: coverage}, comments_requested, reasons}
warnings: [safe scope/empty-result messages]
report: {analysis_version, author_id, scope, population_count, sample_count, sampling,
         style, keywords, interaction, comment_demand, viral_materials, evidence, limitations, ...}
report_basis: all_stored_records_in_requested_scope
```

`succeeded` means the recognized visible traversal ended within the selected scope. It does not
prove that X exposed deleted, hidden or omitted data. `partial` includes reasons such as page_budget,
runtime_budget, post_target_reached, comment_post_budget, repeated_cursor or unrecognized_page.
An empty successful traversal includes an explicit warning and must not be presented as proof
that the author has no posts. Cancelled and failed jobs preserve committed pages.

Tool validation sets MCP `isError=true`, names field paths and constraints, and omits input values.
Worker failures have `{code,message,retryable,stage}` under `error`; `result` retains progress and a
data_retained flag. Known failures are:

| Code | Cause and action |
| --- | --- |
| INVALID_INPUT | Missing field, invalid type/range/date/target; correct the named field using tools/list |
| INVALID_TOKEN_FORMAT | Credential helper input is empty, has whitespace or invalid length; provide one token |
| MISSING_CREDENTIAL | Worker cannot resolve the account/proxy reference; provision it locally |
| CREDENTIAL_FILE_ERROR | File is invalid or unreadable; fix JSON/path/access permissions |
| TOKEN_INVALID | X rejected the login; replace the token and submit a new collection |
| ACCOUNT_LOCKED | X requires browser security verification; unlock the account |
| LOGIN_UNCONFIRMED | Login/identity cannot be verified; inspect network/proxy/session and retry |
| ACCOUNT_DISABLED / ACCOUNT_BUSY | Enable the account or wait for its current lease |
| ACCOUNT_CHANGED / DUPLICATE_ACCOUNT | Recheck a replaced identity, or use its existing account reference |
| RATE_LIMITED | Local or platform budget exhausted; wait for the retry window |
| SOURCE_AUTH_REJECTED / SOURCE_FORBIDDEN | Request authentication or target access refused; recheck login/visibility |
| SOURCE_NOT_FOUND / TARGET_UNAVAILABLE | Endpoint or target could not be resolved; check handle, access and manifest |
| SOURCE_UNAVAILABLE | Source setup/request/parse failed; inspect the named stage and safe logs |
| DATABASE_ERROR | Connection, lock, timeout or permission issue; follow the safe database action message |

## Profile, candidate materials and growth

Author reports measure publication hours/weekdays, intervals, text lengths, questions, links,
media, keyword evidence, comment demand evidence and per-metric known/missing counts and medians.
The agent interprets tone, topic direction and audience needs from cited text. Lexical keywords
are not semantic topic labels. Model conclusions remain separate from measurements.

`viral_materials` defaults to likes at least `max(10, 2 * sample_median_likes)`. It returns the
threshold, known/missing sample sizes, candidate post IDs/URLs, observation time, text/features,
ratios, topic evidence and selected-vs-sample feature rates. `analyze_author` also accepts
`viral_metric`, `viral_min_value` and `viral_ratio`. Null counters are excluded, never replaced
with zero. A zero baseline has a null ratio. Small samples and different post ages limit the
interpretation. High performance does not prove causation or guarantee a repeatable viral post.

Reports are saved in sm_analysis_runs. Its author/time index supports `query_analysis_runs`.
Each summary includes job_id, kind, created_at, scope, population_count, sample_count, keywords
and material_count. Read `jobs_get` to retrieve the full saved result and cited examples.

`query_growth` reads the selected post's stored snapshots in observation-time UTC buckets.
Use at most 366 daily buckets or 31 days of hourly buckets per call. A point contains bucket,
first_at, last_at, observations, latest metrics, per-metric `{delta,ratio}`, and baseline.
The first point uses the first observation in its bucket; later points use the previous bucket's
last observation. A single observation has no growth; unknown counters and zero-baseline ratios
are null. Signed decreases stay negative. Missing buckets are absent, not zero activity.

## Storage and verification

The existing posts/comments/current JSON, content versions, partitioned snapshots, job checkpoints,
accounts and analysis tables remain in PostgreSQL. The only schema addition is an analysis author/time
index; rerun `server.py --init-db` with the schema-owner role during an upgrade. No data is moved.
Credential values remain in a separate restricted local file or worker environment.

The upstream research test suite covers validation, token replacement, rejected login, preferred
accounts, fixture posts/comments, resumable failures, budgets, cancellation, reports, growth and
MCP-to-independent-worker communication. It is not bundled in this distribution. Run the included
scripts/check_service.py for protocol and database checks. Live endpoint behavior requires a bounded
user-authorized target and is not established by fixture success.

## Recovery and archive update

The account ID is a preferred account when `mcp_account_rotation` is enabled (default true).
The worker can retry a page with other configured, checked accounts. Set rotation false to keep author
jobs pinned. See [RECOVERY.md](RECOVERY.md) for limits and diagnostics; no credentials enter tool arguments.
The numeric author ID is also the canonical archive ID. Schema 2 records job history and category links.
Four new tools query archives/history/categories and continue incomplete comment batches; their contracts
are in [MONITORING.md](MONITORING.md). A continuation preserves post, scope and pending branches.

## On-demand monitoring

See [HOURLY.md](HOURLY.md) for schema 3 and hourly background collection with user-requested reports.
A collect_author or analysis job never creates a monitor. Only explicit requests to keep monitoring
changes permit monitor_upsert with monitoring_requested=true. New, edited and unavailable post
observations are queried with query_post_changes; suspected deletion is not confirmed deletion.
