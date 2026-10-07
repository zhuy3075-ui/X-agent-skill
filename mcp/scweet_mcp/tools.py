"""Monitoring MCP tools. Network work is only done by the independent worker."""
import json
from typing import Annotated, Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from mcp.types import ToolAnnotations
from scweet_mcp.accounts import Accounts
from scweet_mcp.contracts import AccountEntry, Id, XId, JobReceipt, Metric, MonitorConfig, PostType, CollectionRequest
from scweet_mcp.jobs import JobQueue, identifier
from scweet_mcp.monitors import Monitors
from scweet_mcp.observations import Observations
from scweet_mcp.reports import EXPORT_FIELDS, Reports
from scweet_mcp.store import StoreError, utc_timestamp

RequestId = Annotated[str, Field(min_length=1, max_length=200)]
Cursor = Annotated[Optional[str], Field(max_length=4096)]
Ids = Annotated[list[Id], Field(min_length=1, max_length=100)]
XIds = Annotated[list[XId], Field(min_length=1, max_length=100)]
Limit = Annotated[int, Field(strict=True, ge=1, le=100)]


class Page(BaseModel):
    records: list[dict[str, Any]]
    count: int
    has_more: bool
    next_cursor: Optional[str]


class CommentsPage(Page):
    coverage: dict[str, Any]


class MonitorResult(BaseModel):
    monitor_id: str
    version: int
    enabled: bool
    next_run_at: str
    estimated_requests_per_day: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)


class JobState(BaseModel):
    job_id: str
    kind: str
    status: str
    submitted_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    progress: dict[str, Any]
    result: Optional[dict[str, Any]]
    error: Optional[dict[str, Any]]
    attempts: int
    cancel_requested: bool


class AccountsResult(BaseModel):
    accounts: list[dict[str, Any]]


class StateResult(BaseModel):
    account_ids: list[str]
    enabled: bool


class CancelResult(BaseModel):
    job_id: str
    status: str
    cancellation_requested: bool


class AckResult(BaseModel):
    acknowledged_ids: list[str]
    already_acknowledged_ids: list[str]


def register_tools(server, store, invoke):
    Limit = Annotated[int, Field(strict=True, ge=1, le=min(100, store.config.mcp_page_limit))]
    default_limit = min(50, store.config.mcp_page_limit)
    monitors, observations, accounts, jobs = Monitors(store), Observations(store), Accounts(store), JobQueue(store)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    collect = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    @server.tool(annotations=collect)
    async def collect_author(config: CollectionRequest, request_id: RequestId) -> JobReceipt:
        """Collect posts and visible comments with a preferred account and configured pool failover, then save a report. Poll jobs_get."""
        from scweet_mcp.collection import submit_collection
        return JobReceipt(**await invoke(submit_collection, store=store, config=config, request_id=request_id))

    @server.tool(annotations=read)
    async def query_growth(post_id: XId, since: str, until: str, bucket: Literal["hour", "day"] = "day") -> dict[str, Any]:
        """Read observed metric growth in UTC buckets. Null is unknown, and a single observation has no growth."""
        from scweet_mcp.research import Research
        return await invoke(Research(store).growth, post_id=post_id, since=since, until=until, bucket=bucket)

    @server.tool(annotations=read)
    async def query_analysis_runs(author_id: XId, limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """List saved author research summaries. Read jobs_get for the full report and viral-material evidence."""
        from scweet_mcp.research import Research
        return Page(**await invoke(Research(store).analyses, author_id=author_id, limit=limit, cursor=cursor))

    def enqueue(kind, request_id, **params):
        if params.get("since"):
            start = utc_timestamp(params["since"], "since")
        else:
            start = None
        if params.get("until"):
            end = utc_timestamp(params["until"], "until")
            if start and start >= end:
                raise StoreError("INVALID_INPUT: since must precede until.")
        if "timezone" in params:
            try:
                ZoneInfo(params["timezone"])
            except (ValueError, ZoneInfoNotFoundError):
                raise StoreError("INVALID_INPUT: Use an IANA timezone.") from None
        if kind == "export_comments" and set(params["fields"] or []) - set(EXPORT_FIELDS):
            raise StoreError("INVALID_INPUT: Export fields must come from the documented field list.")
        if kind == "accounts_check":
            params["account_ids"] = [str(identifier(value)) for value in params["account_ids"]]
        return jobs.enqueue(kind, params, request_id)

    @server.tool(annotations=write)
    async def monitor_upsert(config: MonitorConfig, request_id: RequestId, monitor_id: Optional[Id] = None,
                             expected_version: Optional[int] = None, monitoring_requested: bool = False) -> MonitorResult:
        """Only after an explicit user request for continuous monitoring, set monitoring_requested=true. Hourly sampling, no push."""
        if not monitoring_requested:
            raise StoreError("MONITORING_NOT_REQUESTED: Create a monitor only when the user explicitly asks to monitor changes; set monitoring_requested=true for that request.")
        return MonitorResult(**await invoke(monitors.upsert, config=config, request_id=request_id, monitor_id=monitor_id, expected_version=expected_version))

    @server.tool(annotations=read)
    async def query_post_changes(author_id: XId, since: Optional[str] = None, until: Optional[str] = None,
                                 post_id: Optional[XId] = None, limit: Limit = default_limit,
                                 cursor: Cursor = None) -> Page:
        """Read stored new/content/availability changes without collecting or creating monitors. Times are observations, suspected_deleted is unconfirmed."""
        return Page(**await invoke(observations.changes, author_id=author_id, since=since, until=until,
                                  post_id=post_id, limit=limit, cursor=cursor))

    @server.tool(annotations=read)
    async def monitors_list(author_id: Optional[XId] = None, enabled: Optional[bool] = None,
                            limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """List monitor configuration, version, watermark, last error and scheduling lag."""
        return Page(**await invoke(monitors.list, author_id=author_id, enabled=enabled, limit=limit, cursor=cursor))

    @server.tool(annotations=write)
    async def monitor_set_state(monitor_id: Id, enabled: bool, expected_version: int) -> MonitorResult:
        """Enable or pause future scheduling, with optimistic version checking."""
        return MonitorResult(**await invoke(monitors.set_state, monitor_id=monitor_id, enabled=enabled, expected_version=expected_version))

    @server.tool(annotations=collect)
    async def monitor_run_now(monitor_id: Id, request_id: RequestId) -> JobReceipt:
        """Queue one discovery run. Read jobs_get for progress; a paused monitor does no collection."""
        return JobReceipt(**await invoke(monitors.run_now, monitor_id=monitor_id, request_id=request_id))

    @server.tool(annotations=collect)
    async def refresh_post_metrics(post_ids: XIds, request_id: RequestId) -> JobReceipt:
        """Queue metric snapshots. Missing metrics are null, never fabricated zero values."""
        return JobReceipt(**await invoke(enqueue, kind="refresh_metrics", request_id=request_id, post_ids=post_ids))

    @server.tool(annotations=read)
    async def query_metric_snapshots(post_id: XId, since: Optional[str] = None, until: Optional[str] = None,
                                     metrics: Optional[list[Metric]] = None, limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Read immutable snapshots ordered by observation time descending."""
        return Page(**await invoke(observations.snapshots, post_id=post_id, since=since, until=until, metrics=metrics, limit=limit, cursor=cursor))

    @server.tool(annotations=collect)
    async def sync_comments(post_id: XId, request_id: RequestId, mode: Literal["full", "incremental", "reconcile"] = "incremental",
                            scope: Literal["direct", "thread"] = "thread",
                            max_pages: Annotated[int, Field(ge=1, le=10000)] = 100,
                            max_runtime_seconds: Annotated[int, Field(ge=1, le=86400)] = 300) -> JobReceipt:
        """Queue visible comment traversal. Budgets are capped by server config; read coverage before claiming completeness."""
        return JobReceipt(**await invoke(enqueue, kind="sync_comments", request_id=request_id, post_id=post_id, mode=mode, scope=scope, max_pages=max_pages, max_runtime_seconds=max_runtime_seconds))

    @server.tool(annotations=collect)
    async def continue_comments(job_id: Id, request_id: RequestId,
                                max_pages: Annotated[int, Field(ge=1, le=10000)] = 100,
                                max_runtime_seconds: Annotated[int, Field(ge=1, le=86400)] = 300) -> JobReceipt:
        """Continue a terminal incomplete comment job from its saved cursor queue. Keeps post/scope fixed; new bounded batch, no repeated first page."""
        from scweet_mcp.archives import Archives
        return JobReceipt(**await invoke(Archives(store).continue_comments, job_id=job_id, request_id=request_id,
                                        max_pages=max_pages, max_runtime_seconds=max_runtime_seconds))

    @server.tool(annotations=read)
    async def query_author_archives(author_id: Optional[XId] = None, handle: Optional[str] = None,
                                    limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Find an existing author archive by stable numeric identity or handle. Handles can change; author_id is canonical."""
        from scweet_mcp.archives import Archives
        return Page(**await invoke(Archives(store).list, author_id=author_id, handle=handle, limit=limit, cursor=cursor))

    @server.tool(annotations=read)
    async def query_author_history(author_id: XId, limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Read durable operations and current outcomes for one author, newest first; a retry remains the same operation."""
        from scweet_mcp.archives import Archives
        return Page(**await invoke(Archives(store).history, author_id=author_id, limit=limit, cursor=cursor))

    @server.tool(annotations=read)
    async def query_author_data(author_id: XId, category: Literal["posts", "comments", "interactions"],
                               limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Read categorized author data. Comments belong to the root-post owner, not the commenter. Interactions are observed snapshots."""
        from scweet_mcp.archives import Archives
        return Page(**await invoke(Archives(store).data, author_id=author_id, category=category, limit=limit, cursor=cursor))

    @server.tool(annotations=read)
    async def query_comments(post_id: XId, scope: Literal["direct", "thread"] = "thread", parent_comment_id: Optional[XId] = None,
                             published_since: Optional[str] = None, first_seen_since: Optional[str] = None,
                             author_id: Optional[XId] = None, limit: Limit = default_limit, cursor: Cursor = None) -> CommentsPage:
        """Read comments and coverage. Use first_seen_since to find late-discovered new records."""
        return CommentsPage(**await invoke(observations.comments, post_id=post_id, scope=scope, parent_comment_id=parent_comment_id,
            published_since=published_since, first_seen_since=first_seen_since, author_id=author_id, limit=limit, cursor=cursor))

    @server.tool(annotations=write)
    async def export_comments(post_id: XId, request_id: RequestId, scope: Literal["direct", "thread"] = "thread",
                               format: Literal["csv", "jsonl"] = "jsonl", fields: Optional[list[str]] = None,
                               require_complete: bool = True) -> JobReceipt:
        """Queue a consistent export. Job result contains checksum, row count, coverage and a chunked resource URI."""
        return JobReceipt(**await invoke(enqueue, kind="export_comments", request_id=request_id, post_id=post_id, scope=scope,
                                        format=format, fields=fields, require_complete=require_complete))

    @server.tool(annotations=write)
    async def analyze_comments(post_id: XId, request_id: RequestId, since: Optional[str] = None, until: Optional[str] = None,
                                bucket: Literal["hour", "day"] = "day", timezone: str = "UTC",
                                top_k: Annotated[int, Field(ge=1, le=100)] = 20, include_sentiment: bool = False) -> JobReceipt:
        """Queue stored-comment trends, keywords, active authors and optional limited lexical sentiment."""
        return JobReceipt(**await invoke(enqueue, kind="analyze_comments", request_id=request_id, post_id=post_id, since=since,
                                        until=until, bucket=bucket, timezone=timezone, top_k=top_k, include_sentiment=include_sentiment))

    @server.tool(annotations=write)
    async def analyze_author(author_id: XId, since: str, until: str, request_id: RequestId, timezone: str = "UTC",
                              post_types: Optional[list[PostType]] = None,
                              sample_limit: Annotated[int, Field(ge=1, le=100000)] = 10000,
                              include_comments: bool = True, viral_metric: Metric = "likes",
                              viral_min_value: Annotated[int, Field(ge=0)] = 10,
                              viral_ratio: Annotated[float, Field(ge=1, le=1000, allow_inf_nan=False)] = 2.0) -> JobReceipt:
        """Queue stored-author cadence, text features and comment evidence. Does not infer semantic style by itself."""
        return JobReceipt(**await invoke(enqueue, kind="analyze_author", request_id=request_id, author_id=author_id, since=since,
                                        until=until, timezone=timezone, post_types=post_types, sample_limit=sample_limit, include_comments=include_comments,
                                        viral_metric=viral_metric, viral_min_value=viral_min_value, viral_ratio=viral_ratio))

    @server.tool(annotations=write)
    async def accounts_register(entries: Annotated[list[AccountEntry], Field(min_length=1, max_length=100)], request_id: RequestId) -> AccountsResult:
        """Register worker environment references, never token values. Login checks are required before collection."""
        return AccountsResult(**await invoke(accounts.register, entries=entries, request_id=request_id))

    @server.tool(annotations=read)
    async def accounts_list(status: Optional[Literal["unchecked", "valid", "invalid", "locked", "unknown", "duplicate"]] = None,
                            limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Read safe account health and capability summaries, with no secret values."""
        return Page(**await invoke(accounts.list, status=status, limit=limit, cursor=cursor))

    @server.tool(annotations=collect)
    async def accounts_check(account_ids: Ids, request_id: RequestId, level: Literal["login", "capability"] = "login",
                             capabilities: Optional[list[Literal["timeline", "lookup", "comments"]]] = None) -> JobReceipt:
        """Queue serial login or capability checks. A failed probe does not prove a token is invalid."""
        return JobReceipt(**await invoke(enqueue, kind="accounts_check", request_id=request_id, account_ids=account_ids, level=level, capabilities=capabilities))

    @server.tool(annotations=write)
    async def accounts_set_state(account_ids: Ids, enabled: bool, reason: Optional[str] = None) -> StateResult:
        """Enable or disable use of account references. Disabling blocks the next request reservation."""
        return StateResult(**await invoke(accounts.set_state, account_ids=account_ids, enabled=enabled, reason=reason))

    @server.tool(annotations=read)
    async def jobs_get(job_id: Id) -> JobState:
        """Read durable job state, progress, result or sanitized error. queued/running are nonterminal."""
        return JobState(**await invoke(jobs.get, job_id=job_id))

    @server.tool(annotations=write)
    async def jobs_cancel(job_id: Id) -> CancelResult:
        """Request cancellation at the next safe boundary. Already committed pages remain stored."""
        return CancelResult(**await invoke(jobs.cancel, job_id=job_id))

    @server.tool(annotations=read)
    async def events_query(consumer_ref: Id, types: Optional[list[Literal["new_post", "metric_change", "content_changed", "availability_changed"]]] = None,
                           limit: Limit = default_limit, cursor: Cursor = None) -> Page:
        """Read unacknowledged events. Start each polling cycle without a cursor to include late commits."""
        return Page(**await invoke(observations.events, consumer_ref=consumer_ref, types=types, limit=limit, cursor=cursor))

    @server.tool(annotations=write)
    async def events_ack(consumer_ref: Id, event_ids: Ids) -> AckResult:
        """Acknowledge only events successfully processed by this consumer; duplicate acknowledgments are safe."""
        return AckResult(**await invoke(observations.acknowledge, consumer_ref=consumer_ref, event_ids=event_ids))

    @server.resource("scweet://exports/{artifact_id}/{offset}", mime_type="application/json")
    async def export_chunk(artifact_id: str, offset: int) -> str:
        """Read a bounded base64 chunk. Concatenate decoded bytes until next_uri is null."""
        return json.dumps(await invoke(Reports(store).read_chunk, artifact_id=artifact_id, offset=offset))
