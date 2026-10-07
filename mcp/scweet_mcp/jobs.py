from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from scweet_mcp.store import StoreError, _check_json


def utcnow():
    return datetime.now(timezone.utc)


def wire(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, UUID):
            return str(item)
        raise TypeError("Unsupported result value")
    return json.loads(json.dumps(value, default=encode))


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        raise StoreError("INVALID_INPUT: Use the UUID returned by the service.") from None


def encode_cursor(filters, values):
    return base64.urlsafe_b64encode(json.dumps({
        "q": hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest(),
        "v": wire(values),
    }).encode()).decode()


def decode_cursor(cursor, filters):
    try:
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError()
        parsed = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        expected = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        if parsed["q"] != expected or not isinstance(parsed["v"], list):
            raise ValueError()
        return parsed["v"]
    except (ValueError, KeyError, TypeError):
        raise StoreError("INVALID_INPUT: Use the returned cursor with the same filters.") from None


class LeaseLost(StoreError):
    pass


class JobCancelled(StoreError):
    pass


class JobQueue:
    def __init__(self, store):
        self.store = store

    def enqueue(self, kind, params, request_id, conn=None):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise StoreError("INVALID_INPUT: request_id must contain 1 to 200 characters.")
        _check_json(params)
        if conn is None:
            with self.store._connection() as connection:
                return self.enqueue(kind, params, request_id, connection)
        row = conn.execute("""
            INSERT INTO public.sm_jobs(id,request_id,kind,params) VALUES(%s,%s,%s,%s)
            ON CONFLICT(request_id) DO NOTHING RETURNING id,status,created_at
        """, (uuid4(), request_id, kind, Jsonb(params))).fetchone()
        if row is None:
            previous = conn.execute("SELECT * FROM public.sm_jobs WHERE request_id=%s", (request_id,)).fetchone()
            if previous["kind"] != kind or previous["params"] != params:
                raise StoreError("CONFLICT: request_id already identifies different arguments. Use a new request_id.")
            row = previous
        from scweet_mcp.archives import Archives
        Archives(self.store).link_job(conn, row["id"], params)
        return wire({"job_id": row["id"], "status": row["status"], "submitted_at": row["created_at"]})

    def get(self, job_id):
        with self.store._connection() as conn:
            row = conn.execute("""SELECT id AS job_id,kind,status,created_at AS submitted_at,
                started_at,finished_at,progress,result,error,attempts,cancel_requested
                FROM public.sm_jobs WHERE id=%s""", (identifier(job_id),)).fetchone()
        if not row:
            raise StoreError("NOT_FOUND: Job does not exist. Check job_id.")
        return wire(row)

    def cancel(self, job_id):
        with self.store._connection() as conn:
            row = conn.execute("""UPDATE public.sm_jobs SET cancel_requested=true,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END
                WHERE id=%s RETURNING id AS job_id,status,cancel_requested AS cancellation_requested
            """, (identifier(job_id),)).fetchone()
        if not row:
            raise StoreError("NOT_FOUND: Job does not exist. Check job_id.")
        return wire(row)

    def claim(self):
        config = self.store.config
        with self.store._connection() as conn:
            conn.execute("""UPDATE public.sm_jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                finished_at=now(),error='{"code":"LEASE_EXPIRED","message":"Worker attempts exhausted. Submit a new job.","retryable":true}'
                WHERE status='running' AND lease_until<now() AND (attempts>=%s OR cancel_requested)
            """, (config.mcp_job_max_attempts,))
            return conn.execute("""UPDATE public.sm_jobs SET status='running',attempts=attempts+1,
                lease_token=%s,lease_until=now()+%s*interval '1 second',started_at=coalesce(started_at,now())
                WHERE id=(SELECT id FROM public.sm_jobs WHERE NOT cancel_requested AND attempts<%s
                AND ((status='queued' AND available_at<=now()) OR (status='running' AND lease_until<now()))
                ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *
            """, (uuid4(), config.mcp_job_lease_s, config.mcp_job_max_attempts)).fetchone()

    def guard(self, conn, job):
        row = conn.execute("""SELECT cancel_requested FROM public.sm_jobs
            WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now() FOR UPDATE
        """, (job["id"], job["lease_token"])).fetchone()
        if row is None:
            raise LeaseLost("LEASE_LOST: Another worker owns this job.")
        if row["cancel_requested"]:
            raise JobCancelled("CANCELLED: The caller cancelled this job.")

    def heartbeat(self, job):
        with self.store._connection() as conn:
            return conn.execute("""UPDATE public.sm_jobs SET lease_until=now()+%s*interval '1 second'
                WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()
            """, (self.store.config.mcp_job_lease_s, job["id"], job["lease_token"])).rowcount == 1

    def checkpoint(self, conn, job, checkpoint, progress):
        self.guard(conn, job)
        conn.execute("UPDATE public.sm_jobs SET checkpoint=%s,progress=%s WHERE id=%s",
                     (Jsonb(wire(checkpoint)), Jsonb(progress), job["id"]))
        job["checkpoint"], job["progress"] = checkpoint, progress

    def finish(self, job, result=None, status="succeeded", error=None, retry=False,
               retry_after=None, wait_for_account=False):
        cfg = self.store.config
        delay = min(cfg.mcp_retry_max_delay_s, cfg.mcp_retry_delay_s * 2 ** max(0, job["attempts"]-1))
        if retry_after is not None:
            delay = max(delay if not wait_for_account else 1, retry_after)
        if wait_for_account:
            remaining = cfg.mcp_account_wait_max_s - (utcnow()-job["started_at"]).total_seconds()
            if remaining <= 0:
                wait_for_account, retry = False, False
                error = {**(error or {}), "code": "ACCOUNT_WAIT_EXHAUSTED",
                         "message": "Account availability did not recover within the waiting budget. Saved data is retained; add a checked account or resume later."}
            else:
                delay = min(delay, remaining)
        with self.store._connection() as conn:
            owned = conn.execute("""SELECT cancel_requested FROM public.sm_jobs WHERE id=%s AND lease_token=%s
                AND status='running' AND lease_until>now() FOR UPDATE""", (job["id"], job["lease_token"])).fetchone()
            if not owned:
                raise LeaseLost("LEASE_LOST: Do not finish another worker's job.")
            if owned["cancel_requested"]:
                status, retry = "cancelled", False
            elif retry and (wait_for_account or job["attempts"] < cfg.mcp_job_max_attempts):
                status = "queued"
            if error:
                error = {**error, "will_retry": status == "queued", "retry_after_seconds": delay if status == "queued" else None}
            conn.execute("""UPDATE public.sm_jobs SET status=%s,result=%s,error=%s,
                finished_at=CASE WHEN %s='queued' THEN NULL ELSE now() END,lease_token=NULL,lease_until=NULL,
                available_at=now()+%s*interval '1 second',
                attempts=attempts-%s,progress=%s WHERE id=%s
            """, (status, Jsonb(wire(result)) if result is not None else None,
                  Jsonb(error) if error else None, status, delay,
                  int(wait_for_account and status == "queued"), Jsonb(job["progress"]), job["id"]))


def page_result(rows, limit, filters, keys):
    more = len(rows) > limit
    kept = rows[:limit]
    cursor = encode_cursor(filters, [kept[-1][key] for key in keys]) if more else None
    return {"records": wire(kept), "count": len(kept), "has_more": more, "next_cursor": cursor}
