from __future__ import annotations

from uuid import uuid4

from psycopg.types.json import Jsonb

from scweet_mcp.contracts import MonitorConfig
from scweet_mcp.jobs import JobQueue, decode_cursor, identifier, page_result, wire
from scweet_mcp.store import StoreError


class Monitors:
    def __init__(self, store):
        self.store, self.jobs = store, JobQueue(store)

    def upsert(self, config: MonitorConfig, request_id, monitor_id=None, expected_version=None):
        payload = config.model_dump(mode="json")
        if set(config.destination_refs) - set(self.store.config.mcp_notification_destinations):
            raise StoreError("INVALID_INPUT: destination_refs must name operator-configured destinations.")
        with self.store._connection() as conn:
            # Reuse the durable idempotency registry; control jobs are completed in this transaction.
            receipt = self.jobs.enqueue("monitor_config", {"config": payload, "monitor_id": monitor_id,
                                        "expected_version": expected_version}, request_id, conn)
            control = conn.execute("SELECT result FROM public.sm_jobs WHERE id=%s FOR UPDATE", (receipt["job_id"],)).fetchone()
            if control["result"]:
                return wire(control["result"])
            if monitor_id:
                if expected_version is None:
                    raise StoreError("INVALID_INPUT: expected_version is required for updates.")
                row = conn.execute("""UPDATE public.sm_monitors SET config=%s,enabled=%s,version=version+1,
                    next_run_at=now() WHERE id=%s AND version=%s AND author_id=%s RETURNING *
                """, (Jsonb(payload), config.enabled, identifier(monitor_id), expected_version, config.author_id)).fetchone()
                if not row:
                    raise StoreError("CONFLICT: Monitor version or author differs. Read monitors_list and retry.")
            else:
                row = conn.execute("""INSERT INTO public.sm_monitors(id,author_id,config,enabled)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(author_id) DO NOTHING RETURNING *
                """, (uuid4(), config.author_id, Jsonb(payload), config.enabled)).fetchone()
                if not row:
                    raise StoreError("CONFLICT: This author already has a monitor. Update its monitor_id.")
            result = wire({"monitor_id": row["id"], "version": row["version"], "enabled": row["enabled"],
                           "next_run_at": row["next_run_at"], "estimated_requests_per_day": None,
                           "warnings": ["Cost depends on pages and active posts; each run is capped by configured budgets."]})
            conn.execute("UPDATE public.sm_jobs SET status='succeeded',finished_at=now(),result=%s WHERE id=%s",
                         (Jsonb(result), receipt["job_id"]))
            return result

    def list(self, author_id=None, enabled=None, limit=50, cursor=None):
        filters = [author_id, enabled]
        clauses, params = ["true"], []
        if author_id is not None:
            clauses.append("author_id=%s")
            params.append(author_id)
        if enabled is not None:
            clauses.append("enabled=%s")
            params.append(enabled)
        if cursor:
            values = decode_cursor(cursor, filters)
            if len(values) != 2:
                raise StoreError("INVALID_INPUT: Invalid monitor cursor.")
            clauses.append("(created_at,id)>(%s::timestamptz,%s::uuid)")
            params.extend(values)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT *,GREATEST(0,EXTRACT(EPOCH FROM now()-next_run_at)) AS lag_seconds FROM public.sm_monitors WHERE "
                                + " AND ".join(clauses) + " ORDER BY created_at,id LIMIT %s", [*params, limit+1]).fetchall()
        for row in rows:
            row["lag_seconds"] = float(row["lag_seconds"])
        return page_result(rows, limit, filters, ["created_at", "id"])

    def set_state(self, monitor_id, enabled, expected_version):
        with self.store._connection() as conn:
            row = conn.execute("""UPDATE public.sm_monitors SET enabled=%s,
                config=jsonb_set(config,'{enabled}',%s),version=version+1,
                next_run_at=CASE WHEN %s THEN now() ELSE next_run_at END
                WHERE id=%s AND version=%s RETURNING id AS monitor_id,enabled,version,next_run_at
            """, (enabled, Jsonb(enabled), enabled, identifier(monitor_id), expected_version)).fetchone()
        if not row:
            raise StoreError("CONFLICT: Monitor not found or changed. Refresh its version.")
        return wire(row)

    def run_now(self, monitor_id, request_id):
        with self.store._connection() as conn:
            row = conn.execute("SELECT id FROM public.sm_monitors WHERE id=%s", (identifier(monitor_id),)).fetchone()
            if not row:
                raise StoreError("NOT_FOUND: Monitor does not exist.")
            return self.jobs.enqueue("monitor", {"monitor_id": str(row["id"])}, request_id, conn)

    def schedule(self):
        """Coalesce missed hourly slots; enqueue bounded discovery and lookup work."""
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM public.sm_monitors WHERE enabled AND next_run_at<=now() ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 50").fetchall()
            for row in rows:
                config = row["config"]
                monitor_id = str(row["id"])
                slot = f"schedule:{monitor_id}:{row['version']}:{row['next_run_at'].isoformat()}"
                active = conn.execute("SELECT 1 FROM public.sm_jobs WHERE kind='monitor' AND params->>'monitor_id'=%s AND status IN ('queued','running')", (monitor_id,)).fetchone()
                if not active:
                    self.jobs.enqueue("monitor", {"monitor_id": monitor_id}, slot, conn)
                horizon = config["metric_schedule"][-1]["max_post_age_hours"]
                # Least recently checked first. This includes old stored posts when a
                # monitor is created, and does not spend the entire pool on one author.
                lookup_active = conn.execute("SELECT 1 FROM sm_jobs WHERE kind='refresh_metrics' AND params->>'monitor_id'=%s AND status IN ('queued','running')", (monitor_id,)).fetchone()
                posts = [] if lookup_active else conn.execute("""SELECT p.post_id,p.next_comments_at FROM public.sm_posts p
                    WHERE p.author_id=%s AND p.post_type=ANY(%s)
                    AND p.published_at>=now()-%s*interval '1 hour'
                    AND (p.next_metric_at IS NULL OR p.next_metric_at<=now())
                    AND NOT EXISTS (SELECT 1 FROM sm_jobs j WHERE j.kind='refresh_metrics'
                        AND j.status IN ('queued','running') AND j.params->'post_ids' ? p.post_id)
                    ORDER BY greatest(p.availability_checked_at,p.last_lookup_scheduled_at) NULLS FIRST,p.post_id
                    FOR UPDATE OF p SKIP LOCKED LIMIT %s""",
                    (row["author_id"], config["post_types"], horizon, config.get("lookup_posts_per_run", 5))).fetchall()
                if posts:
                    self.jobs.enqueue("refresh_metrics", {"post_ids": [p["post_id"] for p in posts],
                        "monitor_id": monitor_id}, slot+":lookup", conn)
                for post in posts:
                    if config.get("comments_interval_seconds"):
                        active = conn.execute("SELECT 1 FROM sm_jobs WHERE kind='sync_comments' AND params->>'post_id'=%s AND status IN ('queued','running')", (post["post_id"],)).fetchone()
                        due = post["next_comments_at"] is None or conn.execute("SELECT %s<=now() AS due", (post["next_comments_at"],)).fetchone()["due"]
                        if due and not active:
                            self.jobs.enqueue("sync_comments", {"post_id": post["post_id"], "monitor_id": monitor_id,
                                "mode": "incremental", "scope": "thread",
                                "max_pages": self.store.config.mcp_job_max_pages,
                                "max_runtime_seconds": self.store.config.mcp_job_max_runtime_s}, slot+":comments:"+post["post_id"], conn)
                            conn.execute("UPDATE sm_posts SET next_comments_at=now()+%s*interval '1 second' WHERE post_id=%s",
                                         (config["comments_interval_seconds"], post["post_id"]))
                    # A failed lookup still keeps a future slot rather than spinning.
                    conn.execute("UPDATE sm_posts SET next_metric_at=now()+interval '1 hour',last_lookup_scheduled_at=now() WHERE post_id=%s", (post["post_id"],))
                conn.execute("UPDATE sm_monitors SET next_run_at=now()+%s*interval '1 second' WHERE id=%s",
                             (max(3600, config["discovery_interval_seconds"]), row["id"]))
            return len(rows)
