"""Independent worker. PostgreSQL is the durable scheduler and communication channel."""
import hashlib
import logging
import threading
import time
from datetime import timedelta

from psycopg.types.json import Jsonb
from scweet_mcp.accounts import Accounts
from scweet_mcp.jobs import JobQueue, JobCancelled, LeaseLost, identifier, utcnow, wire
from scweet_mcp.monitors import Monitors
from scweet_mcp.observations import Observations, post_type
from scweet_mcp.reports import Reports
from scweet_mcp.source import Source
from scweet_mcp.source_errors import AccountWait
from scweet_mcp.store import StoreError, utc_timestamp

logger = logging.getLogger("scweet_mcp.worker")


class Worker:
    def __init__(self, store, source=None):
        self.store, self.jobs = store, JobQueue(store)
        self.source = source or Source(store)
        self.observations, self.accounts = Observations(store), Accounts(store)
        self.next_schedule = 0

    def schedule_checks(self):
        if self.source.name == "fixture":
            return
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT * FROM sm_accounts WHERE enabled AND
                (checked_at IS NULL OR checked_at<now()-%s*interval '1 second')
                AND (lease_until IS NULL OR lease_until<now()) FOR UPDATE SKIP LOCKED LIMIT 100""",
                (self.store.config.mcp_account_check_interval_s,)).fetchall()
            for row in rows:
                active = conn.execute("SELECT 1 FROM sm_jobs WHERE kind='accounts_check' AND params->'account_ids' ? %s AND status IN ('queued','running')", (str(row["id"]),)).fetchone()
                if not active:
                    self.jobs.enqueue("accounts_check", {"account_ids": [str(row["id"])], "level": "login", "capabilities": None},
                                      f"check:{row['id']}:{row['checked_at']}:{int(time.time()//self.store.config.mcp_account_check_interval_s)}", conn)

    def tick(self, schedule=True):
        if schedule and time.monotonic() >= self.next_schedule:
            Monitors(self.store).schedule()
            self.schedule_checks()
            self.next_schedule = time.monotonic() + self.store.config.mcp_schedule_poll_s
        job = self.jobs.claim()
        if not job:
            return False
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(self.store.config.mcp_job_lease_s / 3):
                try:
                    if not self.jobs.heartbeat(job):
                        return
                except StoreError:
                    logger.warning("Job heartbeat failed; writes still require a valid lease")
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        attempt_start = time.monotonic()
        previous_active = job["progress"].get("active_seconds", 0)
        try:
            logger.info("Job %s started (%s)", job["id"], job["kind"])
            result, status = self.execute(job)
            job["progress"]["active_seconds"] = previous_active + time.monotonic()-attempt_start
            self.jobs.finish(job, result=result, status=status)
        except LeaseLost:
            logger.warning("Job ownership changed; previous worker stopped")
        except JobCancelled:
            self.jobs.finish(job, status="cancelled")
        except Exception as exc:
            job["progress"]["active_seconds"] = previous_active + time.monotonic()-attempt_start
            # Only deliberate, sanitized StoreError messages cross the boundary.
            message = str(exc) if isinstance(exc, StoreError) else "INTERNAL_ERROR: Worker operation failed. Check configuration and retry."
            code = message.split(":", 1)[0]
            retry = code not in {"INVALID_INPUT", "NOT_FOUND", "INCOMPLETE_COVERAGE", "FIXTURE_MODE", "MISSING_CREDENTIAL",
                                 "TOKEN_INVALID", "ACCOUNT_LOCKED", "ACCOUNT_DISABLED", "DUPLICATE_ACCOUNT",
                                 "CREDENTIAL_FILE_ERROR", "TARGET_UNAVAILABLE", "SOURCE_FORBIDDEN", "NO_VALID_ACCOUNT", "AUTHOR_ID_CONFLICT"}
            logger.error("Job %s failed (%s)", job["id"], code)
            previous_error = job.get("error") or {}
            source_error = previous_error.get("last_source_error")
            if not isinstance(exc, AccountWait):
                source_error = {"code": code, "details": getattr(exc, "details", {})}
            try:
                self.jobs.finish(job, status="failed", result={"progress": job["progress"],
                    "data_retained": job["progress"].get("fetched", 0) > 0},
                    error={"code": code, "message": message, "retryable": retry,
                           "stage": job["checkpoint"].get("stage", "preflight"),
                           "details": getattr(exc, "details", {}), "last_source_error": source_error}, retry=retry,
                    retry_after=getattr(exc, "retry_after", None), wait_for_account=isinstance(exc, AccountWait))
            except LeaseLost:
                pass
        finally:
            stop.set()
            thread.join(timeout=2)
        return True

    def execute(self, job):
        kind, params = job["kind"], job["params"]
        if params.get("monitor_id"):
            with self.store._connection() as conn:
                monitor = conn.execute("SELECT enabled FROM sm_monitors WHERE id=%s", (identifier(params["monitor_id"]),)).fetchone()
            if not monitor or not monitor["enabled"]:
                return {"reason": "monitor_disabled"}, "cancelled"
        if kind == "collect_author":
            from scweet_mcp.collection import collect_author
            return collect_author(self.store, self.source, job)
        if kind in {"monitor", "sync_comments"}:
            return self.collect(job)
        if kind == "refresh_metrics":
            missing = list(job["checkpoint"].get("missing_post_ids", []))
            unknown = list(job["checkpoint"].get("unknown_post_ids", []))
            target_errors = list(job["checkpoint"].get("target_errors", []))
            start = job["checkpoint"].get("index", 0)
            for index in range(start, len(params["post_ids"])):
                self.check(job)
                target = params["post_ids"][index]
                try:
                    page = self.source.page("metrics", [target])
                except StoreError as exc:
                    code = str(exc).split(":", 1)[0]
                    if code not in {"SOURCE_FORBIDDEN", "TARGET_UNAVAILABLE"}:
                        raise
                    # A refusal of one post must not discard the other posts in a batch.
                    unknown.append(target)
                    target_errors.append({"post_id": target, "code": code})
                    self.observations.write_page(job, f"lookup:{target}", [],
                        {"index": index+1, "missing_post_ids": missing, "unknown_post_ids": unknown,
                         "target_errors": target_errors}, source=self.source.name)
                    continue
                records = [row for row in page["records"] if row["tweet_id"] == target]
                if not records:
                    missing.append(target)
                if page["unknown"]:
                    unknown.append(target)
                self.observations.write_page(job, f"lookup:{target}", records,
                    {"index": index+1, "missing_post_ids": missing, "unknown_post_ids": unknown,
                     "target_errors": target_errors}, source=self.source.name,
                    missing_post_ids=[target] if not records and not page["unknown"] else [])
            return {"progress": job["progress"], "missing_post_ids": missing, "unknown_post_ids": unknown,
                    "target_errors": target_errors, "source": self.source.name}, "partial" if missing or unknown else "succeeded"
        if kind == "accounts_check":
            result = []
            for account_id in params["account_ids"]:
                self.check(job)
                account = self.accounts.lease(identifier(account_id), check=True)
                if not account:
                    result.append({"account_id": account_id, "health": "busy_or_missing"})
                    continue
                try:
                    result.append(self.source.check(account, params["level"], params.get("capabilities")))
                finally:
                    self.accounts.release(account)
            return {"accounts": result}, "succeeded"
        if kind == "export_comments":
            # A crash after manifest commit must not create another artifact.
            with self.store._connection() as conn:
                existing = conn.execute("SELECT manifest FROM sm_exports WHERE job_id=%s", (job["id"],)).fetchone()
            return existing["manifest"] if existing else Reports(self.store).export(job), "succeeded"
        if kind in {"analyze_comments", "analyze_author"}:
            return Reports(self.store).analyze(job), "succeeded"
        raise StoreError("INVALID_INPUT: Unsupported worker job kind.")

    def check(self, job):
        with self.store._connection() as conn:
            self.jobs.guard(conn, job)
            if job["params"].get("monitor_id"):
                monitor = conn.execute("SELECT enabled FROM sm_monitors WHERE id=%s", (identifier(job["params"]["monitor_id"]),)).fetchone()
                if not monitor or not monitor["enabled"]:
                    raise JobCancelled("Monitor is paused; stop before the next platform request.")

    def collect(self, job):
        params, is_monitor = job["params"], job["kind"] == "monitor"
        monitor = None
        if is_monitor:
            with self.store._connection() as conn:
                monitor = conn.execute("SELECT * FROM sm_monitors WHERE id=%s", (identifier(params["monitor_id"]),)).fetchone()
            if not monitor:
                raise StoreError("NOT_FOUND: Monitor no longer exists.")
            if not monitor["enabled"]:
                return {"reason": "monitor_disabled"}, "cancelled"
            target, operation = monitor["author_id"], "timeline"
        else:
            target, operation = params["post_id"], "comments"
        state = dict(job["checkpoint"])
        if not state and (is_monitor or params.get("mode") == "incremental"):
            with self.store._connection() as conn:
                # A new bounded run continues the previous incomplete traversal. Full/reconcile starts again.
                previous = conn.execute("""SELECT checkpoint FROM sm_jobs WHERE kind=%s AND id<>%s
                    AND status IN ('partial','succeeded') AND params->>%s=%s
                    AND (%s OR params->>'scope'=%s) ORDER BY finished_at DESC LIMIT 1""",
                    (job["kind"], job["id"], "monitor_id" if is_monitor else "post_id",
                     str(monitor["id"]) if monitor else target, is_monitor, params.get("scope"))).fetchone()
                if previous and previous["checkpoint"].get("pending"):
                    state = previous["checkpoint"]
        pending = state.get("pending", [None])
        visited = set(state.get("visited", []))
        unknown = state.get("unknown", False)
        start = time.monotonic()-job["progress"].get("active_seconds", 0)
        pages = job["progress"]["pages"]
        max_pages = min(params.get("max_pages", self.store.config.mcp_job_max_pages), self.store.config.mcp_job_max_pages)
        runtime = min(params.get("max_runtime_seconds", self.store.config.mcp_job_max_runtime_s), self.store.config.mcp_job_max_runtime_s)
        cutoff = None
        if monitor:
            cutoff = (monitor["watermark"]-timedelta(seconds=self.store.config.mcp_overlap_s)) if monitor["watermark"] else job["created_at"]-timedelta(days=monitor["config"]["initial_backfill_days"])
            if state.get("cutoff"):
                cutoff = utc_timestamp(state["cutoff"], "cutoff")
        reason = None
        while pending and pages < max_pages and time.monotonic()-start < runtime:
            self.check(job)
            cursor = pending[0]
            key = hashlib.sha256((cursor or "initial").encode()).hexdigest()
            if key in visited:
                pending.pop(0)
                unknown = True
                continue
            page = self.source.page(operation, target, cursor)
            if page.get("account_id"):
                used = job["progress"].setdefault("accounts_used", [])
                if page["account_id"] not in used:
                    used.append(page["account_id"])
            pending.pop(0)
            visited.add(key)
            unknown = unknown or page["unknown"]
            reason = page.get("stop_reason") or reason
            for following in page["cursors"]:
                if hashlib.sha256(following.encode()).hexdigest() in visited:
                    unknown = True
                elif following not in pending:
                    pending.append(following)
            records = page["records"]
            if monitor:
                records = [row for row in records if row.get("author_id") == target
                           and post_type(row) in monitor["config"]["post_types"]
                           and row.get("timestamp") and utc_timestamp(row["timestamp"], "timestamp") >= cutoff]
                # Do not stop on one old pinned post. All dated posts in a full page must be older.
                dated = [utc_timestamp(row["timestamp"], "timestamp") for row in page["records"] if row.get("timestamp")]
                if dated and len(dated) == len(page["records"]) and max(dated) < cutoff:
                    pending = []
            else:
                records = [row for row in records if row["tweet_id"] != target and
                           (row.get("in_reply_to_tweet_id") == target if params["scope"] == "direct"
                            else row.get("conversation_id") == target and row.get("in_reply_to_tweet_id"))]
                if any(not row.get("conversation_id") for row in page["records"]):
                    unknown = True
            state = {"pending": pending, "visited": sorted(visited), "unknown": unknown,
                     "cutoff": cutoff.isoformat() if cutoff else None,
                     "scan_started_at": state.get("scan_started_at", job["created_at"].isoformat())}
            self.observations.write_page(job, key, records, state, kind="post" if monitor else "comment",
                                         root_post_id=target if not monitor else None, monitor=monitor, source=self.source.name,
                                         root_author_id=page.get("root_author_id") if not monitor else None)
            pages += 1
        partial = bool(pending) or unknown
        if pending:
            reason = "page_budget" if pages >= max_pages else "runtime_budget"
        coverage = {"scope": params.get("scope", "thread"), "status": "partial" if partial else "exhausted_visible",
                    "pending_branches": len(pending), "platform_reported_count": None,
                    "stop_reason": reason or ("unrecognized_or_repeated_cursor" if unknown else "visible_cursors_exhausted"),
                    "checked_at": utcnow().isoformat(), "source": self.source.name}
        with self.store._connection() as conn:
            self.jobs.guard(conn, job)
            if monitor:
                conn.execute("""UPDATE sm_monitors SET last_run_at=now(),last_error=%s,
                    watermark=CASE WHEN %s THEN watermark ELSE greatest(coalesce(watermark,%s),%s) END WHERE id=%s""",
                    (Jsonb(coverage) if partial else None, partial, state.get("scan_started_at", job["created_at"]), state.get("scan_started_at", job["created_at"]), monitor["id"]))
            else:
                coverage["fetched_unique"] = conn.execute("SELECT count(*) AS count FROM sm_observations WHERE job_id=%s AND kind='comment'", (job["id"],)).fetchone()["count"]
                conn.execute("""INSERT INTO sm_coverage(root_post_id,scope,job_id,checked_at,coverage) VALUES(%s,%s,%s,now(),%s)
                    ON CONFLICT(root_post_id,scope) DO UPDATE SET job_id=excluded.job_id,checked_at=excluded.checked_at,coverage=excluded.coverage""",
                    (target, params["scope"], job["id"], Jsonb(coverage)))
        return {"progress": job["progress"], "coverage": coverage, "source": self.source.name}, "partial" if partial else "succeeded"
