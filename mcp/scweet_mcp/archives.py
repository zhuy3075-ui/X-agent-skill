"""Canonical author archives and category views; collected payloads have one source of truth."""
import re

from psycopg.types.json import Jsonb
from scweet_mcp.jobs import JobQueue, decode_cursor, identifier, page_result, wire
from scweet_mcp.store import StoreError


class Archives:
    def __init__(self, store):
        self.store = store

    def ensure(self, conn, author_id, handle=None):
        if not isinstance(author_id, str) or not re.fullmatch(r"[0-9]{1,32}", author_id):
            raise StoreError("INVALID_INPUT: Archive author_id must be a numeric X user ID.")
        if handle and not re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", handle):
            handle = None
        conn.execute("""INSERT INTO sm_authors(author_id,handle) VALUES(%s,%s)
            ON CONFLICT(author_id) DO UPDATE SET handle=coalesce(excluded.handle,sm_authors.handle),updated_at=now()""",
                     (author_id, handle.lstrip("@").lower() if handle else None))

    def operation(self, conn, author_id, job_id, handle=None):
        self.ensure(conn, author_id, handle)
        conn.execute("INSERT INTO sm_author_operations(author_id,job_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (author_id, job_id))

    def bind_post(self, conn, author_id, post_id, job_id, handle=None):
        self.operation(conn, author_id, job_id, handle)
        row = conn.execute("""INSERT INTO sm_author_posts(post_id,author_id) VALUES(%s,%s)
            ON CONFLICT(post_id) DO UPDATE SET author_id=sm_author_posts.author_id RETURNING author_id""", (post_id, author_id)).fetchone()
        if row["author_id"] != author_id:
            raise StoreError("AUTHOR_ID_CONFLICT: This post is linked to a different author. Check source identity before retrying.")

    def link_job(self, conn, job_id, params):
        author = params.get("author_id") or (params.get("config") or {}).get("author_id")
        if not author and str(params.get("target", "")).isdigit():
            author = params["target"]
        if params.get("monitor_id"):
            row = conn.execute("SELECT author_id FROM sm_monitors WHERE id=%s", (identifier(params["monitor_id"]),)).fetchone()
            author = row["author_id"] if row else author
        if author:
            self.operation(conn, author, job_id)
        post_ids = params.get("post_ids", []) + ([params["post_id"]] if params.get("post_id") else [])
        if post_ids:
            for row in conn.execute("SELECT DISTINCT author_id FROM sm_author_posts WHERE post_id=ANY(%s)", (post_ids,)).fetchall():
                self.operation(conn, row["author_id"], job_id)

    def list(self, author_id=None, handle=None, limit=50, cursor=None):
        filters = {"author_id": author_id, "handle": handle.lower().lstrip("@") if handle else None}
        terms, values = ["true"], []
        for key in ("author_id", "handle"):
            if filters[key]:
                terms.append(("lower(handle)" if key == "handle" else key)+"=%s")
                values.append(filters[key])
        if cursor:
            parts = decode_cursor(cursor, filters)
            if len(parts) != 1:
                raise StoreError("INVALID_INPUT: Use the returned archive cursor.")
            terms.append("author_id>%s")
            values.append(parts[0])
        with self.store._connection() as conn:
            rows = conn.execute("SELECT *,1 AS archive_version FROM sm_authors WHERE "+" AND ".join(terms)+
                                " ORDER BY author_id LIMIT %s", values+[limit+1]).fetchall()
        return page_result(rows, limit, filters, ["author_id"])

    def history(self, author_id, limit=50, cursor=None):
        filters, values = {"author_id": author_id}, [author_id]
        extra = ""
        if cursor:
            parts = decode_cursor(cursor, filters)
            if len(parts) != 2:
                raise StoreError("INVALID_INPUT: Use the returned history cursor.")
            extra = " AND (o.created_at,o.job_id)<(%s::timestamptz,%s::uuid)"
            values += parts
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT o.created_at,o.job_id,j.kind AS operation,j.status,j.progress,
                j.error,j.finished_at FROM sm_author_operations o JOIN sm_jobs j ON j.id=o.job_id
                WHERE o.author_id=%s"""+extra+" ORDER BY o.created_at DESC,o.job_id DESC LIMIT %s", values+[limit+1]).fetchall()
        return page_result(rows, limit, filters, ["created_at","job_id"])

    def data(self, author_id, category, limit=50, cursor=None):
        filters, values = {"author_id": author_id, "category": category}, [author_id]
        # All table/column fragments are constants; no caller-controlled SQL identifiers.
        variants = {
            "posts": ("p.published_at,p.last_observed_at,p.payload,p.availability,p.availability_checked_at,p.missing_count", "sm_posts p JOIN sm_author_posts a ON a.post_id=p.post_id", "p.post_id", "p.last_observed_at"),
            "comments": ("p.root_post_id,p.author_id AS commenter_id,p.published_at,p.payload", "sm_comments p JOIN sm_author_posts a ON a.post_id=p.root_post_id", "p.comment_id", "p.last_observed_at"),
            "interactions": ("p.entity_id,p.kind,p.metrics,p.missing_fields,p.source", "sm_snapshots p LEFT JOIN sm_comments c ON p.kind='comment' AND c.comment_id=p.entity_id JOIN sm_author_posts a ON a.post_id=CASE WHEN p.kind='comment' THEN c.root_post_id ELSE p.entity_id END", "p.snapshot_id", "p.observed_at"),
        }
        if category not in variants:
            raise StoreError("INVALID_INPUT: category must be posts, comments or interactions.")
        fields, tables, key, date = variants[category]
        extra = ""
        if cursor:
            parts = decode_cursor(cursor, filters)
            if len(parts) != 2:
                raise StoreError("INVALID_INPUT: Use the returned category cursor.")
            extra = f" AND ({date},{key})<(%s::timestamptz,%s"+("::uuid" if category=="interactions" else "")+")"
            values += parts
        with self.store._connection() as conn:
            rows = conn.execute(f"SELECT {key} AS record_id,{date} AS observed_at,{fields} FROM {tables} WHERE a.author_id=%s"+
                                extra+f" ORDER BY {date} DESC,{key} DESC LIMIT %s", values+[limit+1]).fetchall()
        return page_result(rows, limit, filters, ["observed_at","record_id"])

    def continue_comments(self, job_id, request_id, max_pages, max_runtime_seconds):
        job_id = str(identifier(job_id))
        with self.store._connection() as conn:
            previous = conn.execute("SELECT * FROM sm_jobs WHERE id=%s FOR UPDATE", (identifier(job_id),)).fetchone()
            if not previous or previous["kind"] != "sync_comments":
                raise StoreError("NOT_FOUND: Select an existing comment collection job.")
            if previous["status"] not in {"partial", "failed", "cancelled"}:
                raise StoreError("NOT_RESUMABLE: Wait for the active job, or start an incremental scan after a completed traversal.")
            following = conn.execute("SELECT id FROM sm_jobs WHERE params->>'resume_from'=%s AND request_id<>%s LIMIT 1",
                                     (job_id, request_id)).fetchone()
            if following:
                raise StoreError("CONTINUATION_EXISTS: This batch already has a continuation. Inspect author history and continue the latest batch.")
            checkpoint = previous["checkpoint"]
            if not checkpoint.get("pending"):
                if previous["progress"].get("pages", 0) == 0:
                    checkpoint = {"pending": [None], "visited": [], "unknown": False}
                else:
                    raise StoreError("NO_CONTINUATION: No pending cursor is known. Review incomplete coverage or explicitly rescan; do not loop blindly.")
            params = {**previous["params"], "resume_from": job_id, "max_pages": max_pages, "max_runtime_seconds": max_runtime_seconds}
            receipt = JobQueue(self.store).enqueue("sync_comments", params, request_id, conn)
            conn.execute("""UPDATE sm_jobs SET checkpoint=%s WHERE id=%s AND status='queued' AND attempts=0
                AND checkpoint='{}'::jsonb""", (Jsonb(checkpoint), identifier(receipt["job_id"])))
            return receipt
