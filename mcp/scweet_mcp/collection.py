"""One bounded author research run, with page-atomic posts/comments checkpoints."""
import hashlib
import time

from psycopg.types.json import Jsonb
from scweet_mcp.jobs import JobQueue, identifier, utcnow
from scweet_mcp.observations import Observations, post_type
from scweet_mcp.reports import Reports
from scweet_mcp.store import StoreError, utc_timestamp


def submit_collection(store, config, request_id):
    params = config.model_dump()
    params["account_id"] = str(identifier(params["account_id"]))
    with store._connection() as conn:
        account = conn.execute("SELECT enabled FROM sm_accounts WHERE id=%s", (identifier(params["account_id"]),)).fetchone()
        if not account:
            raise StoreError("NOT_FOUND: account_id is not registered. Call accounts_register first.")
        if not account["enabled"]:
            raise StoreError("ACCOUNT_DISABLED: Enable the selected account before collection.")
        return JobQueue(store).enqueue("collect_author", params, request_id, conn)


def collect_author(store, source, job):
    params = job["params"]
    jobs, observations = JobQueue(store), Observations(store)
    started = time.monotonic() - job["progress"].get("active_seconds", 0)
    max_pages = min(params["max_pages"], store.config.mcp_job_max_pages)
    runtime = min(params["max_runtime_seconds"], store.config.mcp_job_max_runtime_s)
    source.preflight(params["account_id"])
    state = dict(job["checkpoint"])
    if not state:
        state = {"stage": "posts", "author_id": source.resolve(params["target"], params["account_id"]),
                 "pending": [None], "visited": [], "post_ids": [], "reasons": [], "comment_coverage": {},
                 "comment_index": 0, "posts_complete": False}
    from scweet_mcp.archives import Archives
    with store._connection() as conn:
        jobs.guard(conn, job)
        Archives(store).operation(conn, state["author_id"], job["id"],
                                  params["target"] if not params["target"].isdigit() else None)

    def save():
        with store._connection() as conn:
            jobs.checkpoint(conn, job, state, job["progress"])

    def reason(value):
        if value not in state["reasons"]:
            state["reasons"].append(value)

    def budget():
        with store._connection() as conn:
            jobs.guard(conn, job)
        if job["progress"]["pages"] >= max_pages:
            reason("page_budget")
            return False
        if time.monotonic() - started >= runtime:
            reason("runtime_budget")
            return False
        return True

    def page(operation, target):
        cursor = state["pending"][0]
        key = hashlib.sha256(f"{operation}:{target}:{cursor or 'initial'}".encode()).hexdigest()
        if key in state["visited"]:
            state["pending"].pop(0)
            reason("repeated_cursor")
            return None, key
        response = source.page(operation, target, cursor, account_id=params["account_id"])
        if response.get("account_id"):
            used = job["progress"].setdefault("accounts_used", [])
            if response["account_id"] not in used:
                used.append(response["account_id"])
        state["pending"].pop(0)
        state["visited"].append(key)
        if response["unknown"]:
            reason(response.get("stop_reason") or "unrecognized_page")
        for following in response["cursors"]:
            digest = hashlib.sha256(f"{operation}:{target}:{following}".encode()).hexdigest()
            if digest in state["visited"]:
                reason("repeated_cursor")
                response["unknown"] = True
            elif following not in state["pending"]:
                state["pending"].append(following)
        return response, key

    since, until = utc_timestamp(params["since"], "since"), utc_timestamp(params["until"], "until")
    while state["stage"] == "posts" and state["pending"] and budget():
        response, key = page("timeline", state["author_id"])
        if response is None:
            save()
            continue
        records = [r for r in response["records"] if r.get("author_id") == state["author_id"]
                   and post_type(r) in params["post_types"] and r.get("timestamp")
                   and since <= utc_timestamp(r["timestamp"], "timestamp") < until]
        for row in records:
            if row["tweet_id"] not in state["post_ids"]:
                state["post_ids"].append(row["tweet_id"])
        dated = [utc_timestamp(r["timestamp"], "timestamp") for r in response["records"] if r.get("timestamp")]
        if dated and len(dated) == len(response["records"]) and max(dated) < since:
            state["pending"] = []
        if not state["pending"]:
            state["posts_complete"] = not state["reasons"]
        reached = len(state["post_ids"]) >= params["max_posts"]
        if reached and state["pending"]:
            reason("post_target_reached")
        if reached or not state["pending"]:
            state.update(stage="comments", pending=[None], visited=[])
        observations.write_page(job, key, records, state, source=source.name)

    selected = state["post_ids"][:params["max_comment_posts"]] if params["include_comments"] else []
    if params["include_comments"] and len(selected) < len(state["post_ids"]):
        reason("comment_post_budget")
    if state["stage"] == "comments":
        while state["comment_index"] < len(selected) and budget():
            target = selected[state["comment_index"]]
            try:
                response, key = page("comments", target)
            except StoreError as exc:
                code = str(exc).split(":", 1)[0]
                if code not in {"SOURCE_FORBIDDEN", "TARGET_UNAVAILABLE"}:
                    raise
                # A target-specific refusal must not stop collection of independent posts.
                state["comment_coverage"][target] = {"status": "partial", "unknown": True, "error_code": code}
                reason("comment_target_unavailable")
                state["comment_index"] += 1
                state.update(pending=[None], visited=[])
                save()
                continue
            if response is None:
                records = []
            else:
                records = [r for r in response["records"] if r["tweet_id"] != target and
                           (r.get("in_reply_to_tweet_id") == target if params["comment_scope"] == "direct"
                            else r.get("conversation_id") == target and r.get("in_reply_to_tweet_id"))]
            coverage = state["comment_coverage"].setdefault(target, {"status": "in_progress", "unknown": False})
            coverage["unknown"] |= response is None or response["unknown"]
            if not state["pending"]:
                coverage["status"] = "partial" if coverage["unknown"] else "exhausted_visible"
                state["comment_index"] += 1
                state.update(pending=[None], visited=[])
            observations.write_page(job, key, records, state, kind="comment", root_post_id=target, source=source.name,
                                    root_author_id=state["author_id"])
        if state["comment_index"] == len(selected):
            state["stage"] = "analysis"
    if state["stage"] != "analysis":
        reason("collection_incomplete")
    save()

    with store._connection() as conn:
        jobs.guard(conn, job)
        counts = conn.execute("""SELECT kind,count(DISTINCT entity_id) AS count FROM sm_observations
            WHERE job_id=%s GROUP BY kind""", (job["id"],)).fetchall()
        counts = {row["kind"]: row["count"] for row in counts}
        for target in selected:
            item = state["comment_coverage"].get(target, {"status": "partial"})
            coverage = {"scope": params["comment_scope"], "status": item["status"] if item["status"] != "in_progress" else "partial",
                        "checked_at": utcnow().isoformat(), "source": source.name,
                        "stop_reason": item.get("error_code") or ("visible_cursors_exhausted" if item["status"] == "exhausted_visible" else "bounded_or_unrecognized_scan"),
                        "pending_branches": len(state["pending"]) if state["stage"] == "comments" else 0,
                        "platform_reported_count": None,
                        "fetched_unique": conn.execute("""SELECT count(DISTINCT o.entity_id) AS n FROM sm_observations o
                            JOIN sm_comments c ON c.comment_id=o.entity_id WHERE o.job_id=%s AND o.kind='comment'
                            AND c.root_post_id=%s""", (job["id"], target)).fetchone()["n"]}
            conn.execute("""INSERT INTO sm_coverage(root_post_id,scope,job_id,checked_at,coverage) VALUES(%s,%s,%s,now(),%s)
                ON CONFLICT(root_post_id,scope) DO UPDATE SET job_id=excluded.job_id,checked_at=excluded.checked_at,coverage=excluded.coverage""",
                         (target, params["comment_scope"], job["id"], Jsonb(coverage)))
            state["comment_coverage"][target] = coverage
    report_job = {**job, "kind": "analyze_author", "params": {
        "author_id": state["author_id"], "since": params["since"], "until": params["until"],
        "timezone": params["timezone"], "post_types": params["post_types"], "include_comments": params["include_comments"]}}
    report = Reports(store).analyze(report_job)
    partial = bool(state["reasons"])
    result = {"author_id": state["author_id"], "account_id": params["account_id"], "source": source.name,
              "counts": {"posts": counts.get("post", 0), "comments": counts.get("comment", 0)},
              "progress": job["progress"], "effective_limits": {"max_pages": max_pages, "max_runtime_seconds": runtime,
              "max_posts": params["max_posts"], "max_comment_posts": params["max_comment_posts"]},
              "coverage": {"posts_complete_visible": state["posts_complete"], "comments": state["comment_coverage"],
                           "comments_requested": params["include_comments"], "reasons": state["reasons"]},
              "warnings": [] if counts.get("post") else ["No visible posts matched the selected author, dates and post types."],
              "report": report, "report_basis": "all_stored_records_in_requested_scope"}
    return result, "partial" if partial else "succeeded"
