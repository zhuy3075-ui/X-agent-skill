# Hourly collection and on-demand reporting

## Responsibilities

| Module | Responsibility | Boundary |
| --- | --- | --- |
| Skill / agent | Resolve user intent, choose parameters, query and explain evidence | Create monitors only on explicit requests; no scheduled agent or push reports |
| MCP API | Validate contracts, enqueue jobs, read data, pause monitors | No platform I/O in request handlers |
| Independent worker | Hourly scheduling, pagination, account rotation, retries and observations | No model calls; no automatic report generation |
| PostgreSQL | Durable jobs and leases, unique author archives, current posts, versions, snapshots and events | Collected payloads never contain credentials |

Run the MCP server and worker on the same computer, with one PostgreSQL database and artifact directory.
Local development uses the same process boundaries as a server. No new scheduler dependency is required.
The worker polls the queue every 2 seconds and scans schedules every 60 seconds. These are local database
operations; platform collection runs at the monitor's hourly slot. Account checks and error retries have
their own clocks. The agent does not need to be open for the worker to collect.

## Creation and execution

`monitor_upsert(config, request_id, monitoring_requested=false, monitor_id?, expected_version?)`
rejects a request unless `monitoring_requested=true`. The agent must derive this flag from an explicit
request to keep monitoring changes. A one-time research request or a question about past changes is
not that permission. A boolean cannot independently prove human intent; the skill enforces that boundary.
Existing author monitors use their ID and version rather than another insert.

Default MonitorConfig:

```json
{
  "author_id": "<numeric X user ID>",
  "discovery_interval_seconds": 3600,
  "initial_backfill_days": 7,
  "post_types": ["original", "quote"],
  "metric_schedule": [{"max_post_age_hours": 720, "interval_seconds": 3600}],
  "lookup_posts_per_run": 5,
  "comments_interval_seconds": null,
  "report_mode": "on_demand",
  "destination_refs": []
}
```

The first job builds a recent baseline. Further slots discover new posts and queue one bounded lookup
batch. That batch checks at most five known posts in the selected types and age range (30 days by default).
It chooses the least recently checked or scheduled posts first, so failed posts cannot monopolize the batch. Timeline reads can also observe content and metrics.
Twenty known posts can require four hourly slots for independent lookups; this is not a promise to inspect
every old post each hour. Increase `lookup_posts_per_run` or the final schedule age only for a stated need
and with an adequate account budget. Periodic comments are opt-in and use the same selected batch.

Due slots are transactional and locked. Missed slots coalesce into one run after restart. Active jobs
prevent duplicate work. A failed lookup reserves a future slot to avoid a hot retry loop. Retries keep
their existing backoff, account budgets and checkpoints. A target-level refusal records an unknown result and
continues the other posts, without accumulating deletion evidence. Paused monitors do not enqueue jobs, and queued
monitor-owned jobs check the enabled flag before collection. Already running pages can finish at a safe
boundary; `jobs_cancel` stops those jobs explicitly.

With one-page discovery and five independent lookups, a normal day has 24 discovery calls and up to
120 lookup calls per author. Pagination, initial backfill, signature bootstrap, login checks and retries
add cost. This is a planning estimate, not a measured X rate. Daily account caps still apply.

## Storage and state rules

Schema 3 adds `availability`, `availability_checked_at`, `missing_count` and `last_lookup_scheduled_at` to `sm_posts`, with a
rotation index. `sm_versions` retains each distinct content hash; metric-only changes do not create
content versions. `sm_snapshots` retains observed counts. Events keep successive content and availability
transitions, including a return to an earlier content version. A page commits records, events and its
checkpoint together under the job lease. Replay is idempotent. Existing archive/category links remain.

Only a recognized lookup response without upstream errors can count as a missing post. Timeline absence,
HTTP errors, quota exhaustion, rejected login and unknown parse results cannot count. First reliable
absence means `unavailable`. Three reliable absences separated by at least one hour mean
`suspected_deleted`, with `deletion_confirmed=false`. A later returned post resets the count and records
`visible`. The saved body, comments and snapshots are never removed by this state machine.

Repeated invisibility can also mean protected content or a changed account visibility scope. It is not
proof of deletion. No explicit-deletion parser is invented without a captured platform response. Hourly
samples can miss short-lived edits; posts outside the configured age/type range are not checked by default.
Initial backfill is a baseline, not a list of new releases. Past growth and edit/deletion times cannot be
reconstructed from current totals. Event times are discovery times, not exact platform mutation times.

## Read interfaces

The service now exposes 31 tools. New tool:

`query_post_changes(author_id, since?, until?, post_id?, limit=50, cursor?)`

Returns `{records,count,has_more,next_cursor}`. Each record contains
`{event_id,type,post_id,observed_at,payload}`. Types are `new_post`, `content_changed`,
`availability_changed`. Content payloads include previous/current hashes and text. Availability payloads
include previous/current states and a reason, with `deletion_confirmed=false` for missing lookups.
The range uses inclusive `since` and exclusive `until`. Keyset pagination binds the cursor to filters.
This history is repeatable and does not depend on `events_ack`.

Use `query_author_data(category=posts)` for current content and visibility/check timestamps;
`query_metric_snapshots` / `query_growth` for observed counts; `monitors_list` for schedule health and scope;
`query_author_history` / `jobs_get` for failures. Read-only calls neither collect nor create monitors.
The agent reports real counts, dates, coverage and limitations in plain Chinese when asked.

## Local update and server deployment

First run tests with `SCWEET_MCP_TEST_DATABASE_URL` pointing to a disposable database ending in `_test`.
Stop API and worker before a schema-owner runs `server.py --config <config.json> --init-db`.
Then start the API and independent worker with the same `SCWEET_MCP_DATABASE_URL` and config.
Schema 3 is additive; it retains schema 2 collected rows. Existing monitor preferences are not rewritten.
Old schedules coalesce to at least an hourly discovery slot; new configurations reject faster intervals.
Explicitly update any old monitor's age schedule and batch settings to the new contract when needed.

New ScweetConfig defaults: `mcp_schedule_poll_s=60`, `mcp_notifications_enabled=false`,
`mcp_missing_check_interval_s=3600`, `mcp_missing_confirmations=3`. The worker does not deliver pending
webhooks with notifications disabled. On-demand monitors reject destination references and do not create
delivery rows. The legacy webhook transport remains for existing integrations but is not part of this mode.
Keep local credentials in the existing private credential file; never place them in the skill config.
Copy the skill documents to the installed directory and merge only missing preference keys.
Creating/upgrading this software does not itself create a monitor for any author.
