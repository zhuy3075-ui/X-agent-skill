# Comment recovery and account pools

## Failure classification

Separate source availability, account health, target visibility, quotas and retry exhaustion.
Later success does not prove the original cause; preserve safe response diagnostics.
Page and time budgets independently limit comment coverage.

## Corrected behavior and settings

| Code location | Trigger and corrected path |
| --- | --- |
| `scweet_mcp/source.py:71` (`BudgetEngine`) | Preserve local quota/signature exceptions that the base engine previously converted to network status |
| `scweet_mcp/source.py:132` (`preflight`) and `:170` (`page`) | Failed preferred token falls back to checked accounts; a cooling alternative yields a durable wait |
| `scweet_mcp/source.py:229` (`response_error`) | Separate target refusal, auth, rate limits and partial data; record safe status/codes |
| `scweet_mcp/accounts.py:61` (`lease`), `:72` (`unavailable`), `:90` (`reserve`) | Exclude tried accounts, calculate pool availability and commit quota cooldown before raising |
| `scweet_mcp/jobs.py:142` (`finish`) | Exponential source retries versus attempt-preserving, deadline-bounded pool waits |
| `scweet_mcp/worker.py:88` | Carry delay and diagnostic fields across durable worker retries |
| `scweet_mcp/collection.py:124` | Keep the failed target's coverage and continue other posts after a target-specific refusal |
| `scweet_mcp/archives.py:103` (`continue_comments`) | Canonicalize parent ID and inherit pending/visited cursors without parallel duplicate children |

| Setting | Default | Meaning |
| --- | --- | --- |
| `mcp_job_max_attempts` | 5 | Maximum source attempts, including the first execution |
| `mcp_retry_delay_s` | 30 | Backoff starts at 30 seconds, then 60, 120 and 240 |
| `mcp_retry_max_delay_s` | 300 | Backoff ceiling; a known longer reset time still wins |
| `mcp_account_wait_max_s` | 1800 | Maximum elapsed time from first execution for account-only waiting |
| `mcp_unknown_cooldown_s` | 60 | Short cooldown for transient account/source availability, not a permanent ban |
| `mcp_account_rotation` | true | Allow the preferred account to fail over to other enabled, checked accounts |
| `mcp_account_rotation_attempts` | 3 | Maximum distinct accounts tried for one page call |

No-account waits do not spend a source attempt. The job's next available time follows the earliest known
lease/cooldown recovery, subject to the waiting deadline. Source failures have `details`, `last_source_error`,
`will_retry` and `retry_after_seconds`; raw responses and credentials never enter these diagnostics.
Active execution time is accumulated separately from queue waiting. Page and time budgets still apply.

403/404 and unclassified 200 response errors no longer cool the entire account. Verified authentication
rejection/lock affects only that account. Local quota cooldown commits before the wait is raised. HTTP 429
honors a later reset header, with the configured rate window as fallback. A source guard survives the base
engine's exception translation. Partial data is retained with incomplete coverage. These rules do not
raise the platform's quotas and do not guarantee access to every comment.

For an initial multi-post comment scan, use 100 pages/300 active seconds per batch rather than 5 pages/15
seconds, if the user's budget permits. Continue saved batches rather than resubmitting the first page.
Do not raise all limits to their maximum: measure actual pages, duration, visible coverage and account usage.
Increasing retry counts alone does not repair an empty or invalid account pool.

## Configure more than one token

Each token uses a different reference in the private credential file configured by `mcp_credentials_file`.
Run the hidden-input helper for A and then B; existing entries are preserved atomically:

```powershell
.venv/Scripts/python.exe mcp/credentials.py --file C:/path/X-agent-skill/private/worker-env.json --credential-ref SCWEET_X_ACCOUNT_A
.venv/Scripts/python.exe mcp/credentials.py --file C:/path/X-agent-skill/private/worker-env.json --credential-ref SCWEET_X_ACCOUNT_B
```

Call `accounts_register` with both `{credential_ref,label}` entries and a unique request ID. Call
`accounts_check` for their returned IDs and wait for its outcome. Only enabled, checked accounts enter
the page pool. Different tokens for the same X identity are detected as duplicates, not extra quota.
The worker rereads private token values at request time. New registered references need a successful
check; replacing a token needs identity revalidation. No token value goes in MCP arguments or skill config.
Set `mcp_account_rotation=false` to require the selected account for author jobs. Unpinned comment jobs
still choose from the configured pool but only attempt one account per page call in that mode.

Configure at least two distinct valid accounts to use live rotation. Simulated switching tests do not
establish platform access or throughput for a real account pool.

## Pagination and archive deployment

See [the monitoring guide](MONITORING.md) for four new tools and schema 2 installation. Archives use one
canonical author ID with category links, not duplicated files. Do not run regression fixtures against the
runtime database: they truncate data. Tests require a separate database with a name ending in `_test`.

The unified `x-agent-skill` permits implicit invocation for platform plus collection intent.
It recognizes 推特, Twitter and social-platform X in context, and calls actual available tools. This is
agent routing, not a deterministic background keyword watcher. The same skill owns
parameter defaults, pagination budgets, archive reuse and plain-language reports.
