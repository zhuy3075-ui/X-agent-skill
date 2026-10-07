"""Validated monitoring contracts; no credentials are accepted by the MCP surface."""
from __future__ import annotations

from typing import Annotated, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
XId = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")]
Metric = Literal["likes", "comments", "reposts", "views", "quotes"]
PostType = Literal["original", "reply", "quote", "repost"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MetricSchedule(Contract):
    max_post_age_hours: int = Field(ge=1)
    interval_seconds: int = Field(ge=60)


class AlertRule(Contract):
    metric: Metric
    window_seconds: int = Field(ge=60)
    min_delta: int = Field(ge=1)
    min_ratio: Optional[float] = Field(default=None, ge=0)
    direction: Literal["increase", "decrease"] = "increase"
    cooldown_seconds: int = Field(default=3600, ge=0)


class Alerts(Contract):
    new_posts: bool = True
    rules: list[AlertRule] = Field(default_factory=list, max_length=30)


class MonitorConfig(Contract):
    author_id: XId
    enabled: bool = True
    timezone: str = "UTC"
    discovery_interval_seconds: int = Field(default=3600, ge=3600)
    initial_backfill_days: int = Field(default=7, ge=0, le=3650)
    post_types: list[PostType] = Field(default_factory=lambda: ["original", "quote"], min_length=1)
    metrics: list[Metric] = Field(default_factory=lambda: ["likes", "comments", "reposts", "views"])
    metric_schedule: list[MetricSchedule] = Field(default_factory=lambda: [
        MetricSchedule(max_post_age_hours=720, interval_seconds=3600),
    ], min_length=1, max_length=10)
    comments_interval_seconds: Optional[int] = Field(default=None, ge=3600)
    alerts: Alerts = Field(default_factory=Alerts)
    destination_refs: list[Id] = Field(default_factory=list, max_length=10)
    lookup_posts_per_run: int = Field(default=5, strict=True, ge=1, le=100)
    report_mode: Literal["on_demand"] = "on_demand"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone, for example Asia/Shanghai") from None
        return value

    @model_validator(mode="after")
    def ordered_schedule(self):
        if self.destination_refs:
            raise ValueError("On-demand monitors do not send notifications; use an empty destination_refs list")
        if any(entry.interval_seconds < 3600 for entry in self.metric_schedule):
            raise ValueError("Lightweight monitor intervals must be at least 3600 seconds")
        ages = [entry.max_post_age_hours for entry in self.metric_schedule]
        if ages != sorted(set(ages)):
            raise ValueError("Metric schedule ages must be distinct and increasing")
        return self


class AccountEntry(Contract):
    credential_ref: str = Field(pattern=r"^SCWEET_X_[A-Z0-9_]+$", max_length=128)
    label: str = Field(min_length=1, max_length=80)
    proxy_ref: Optional[str] = Field(default=None, pattern=r"^SCWEET_PROXY_[A-Z0-9_]+$", max_length=128)


class JobReceipt(Contract):
    job_id: str
    status: str
    submitted_at: str


class CollectionRequest(Contract):
    account_id: Id
    target: str = Field(min_length=1, max_length=200)
    since: str
    until: str
    max_posts: int = Field(strict=True, ge=1, le=10000)
    post_types: list[PostType] = Field(default_factory=lambda: ["original", "quote"], min_length=1)
    include_comments: bool = True
    max_comment_posts: int = Field(default=20, strict=True, ge=1, le=100)
    comment_scope: Literal["direct", "thread"] = "thread"
    max_pages: int = Field(default=100, strict=True, ge=1, le=10000)
    max_runtime_seconds: int = Field(default=300, strict=True, ge=1, le=86400)
    timezone: str = "UTC"

    @field_validator("target")
    @classmethod
    def target_format(cls, value):
        if value.startswith("https://"):
            parsed = urlparse(value)
            if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or parsed.query or parsed.fragment:
                raise ValueError("Use an X profile URL without query parameters")
            value = "@" + parsed.path.strip("/")
        if re.fullmatch(r"[0-9]{1,32}", value):
            return value
        if not re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", value):
            raise ValueError("Use a numeric author ID, handle or X profile URL")
        return "@" + value.lstrip("@").lower()

    @field_validator("timezone")
    @classmethod
    def timezone_format(cls, value):
        return MonitorConfig.valid_timezone(value)

    @model_validator(mode="after")
    def time_range(self):
        from scweet_mcp.store import utc_timestamp
        if utc_timestamp(self.since, "since") >= utc_timestamp(self.until, "until"):
            raise ValueError("since must precede until; both require a timezone")
        if self.include_comments and "reply" in self.post_types and self.comment_scope == "thread":
            raise ValueError("Use direct comment_scope when collecting reply posts; thread scope requires a root post")
        return self


class Coverage(Contract):
    scope: Literal["direct", "thread"] = "thread"
    status: Literal["in_progress", "exhausted_visible", "partial", "unknown"] = "unknown"
    fetched_unique: int = 0
    platform_reported_count: Optional[int] = None
    pending_branches: int = 0
    stop_reason: Optional[str] = None
    checked_at: str


