from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from scweet_mcp.jobs import JobQueue, decode_cursor, identifier, page_result, utcnow, wire
from scweet_mcp.store import StoreError, utc_timestamp


METRICS = ("likes", "comments", "reposts", "views", "quotes")


def content_hash(record):
    content = {key: record.get(key) for key in ("text", "embedded_text", "media", "in_reply_to_tweet_id", "tweet_url")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def post_type(record):
    if record.get("is_retweet"):
        return "repost"
    if record.get("in_reply_to_tweet_id"):
        return "reply"
    return "quote" if record.get("is_quote") else "original"


class Observations:
    def __init__(self, store):
        self.store, self.jobs = store, JobQueue(store)

    def event(self, conn, event_type, monitor, post_id, key, payload):
        event_id = uuid4()
        row = conn.execute("""INSERT INTO public.sm_events(id,dedupe_key,type,monitor_id,post_id,payload)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO NOTHING RETURNING id
        """, (event_id, key, event_type, monitor["id"] if monitor else None, post_id, Jsonb(wire(payload)))).fetchone()
        if row and monitor and self.store.config.mcp_notifications_enabled and monitor["config"].get("report_mode") != "on_demand":
            for destination in monitor["config"].get("destination_refs", []):
                conn.execute("INSERT INTO public.sm_deliveries(event_id,destination_ref) VALUES(%s,%s) ON CONFLICT DO NOTHING", (event_id, destination))
        return str(event_id) if row else None

    def write_page(self, job, page_key, records, checkpoint, *, kind="post", root_post_id=None,
                   monitor=None, source="platform", observed_at=None, root_author_id=None,
                   missing_post_ids=None):
        observed_at = observed_at or utcnow()
        # Reuse the established credential and size validation before opening a transaction.
        rows = self.store.prepare(records) if records else []
        by_id = {record["tweet_id"]: record for record in records}
        counts = {"inserted": 0, "updated": 0}
        with self.store._connection() as conn:
            self.jobs.guard(conn, job)
            from scweet_mcp.archives import Archives
            archives = Archives(self.store)
            if kind == "comment" and root_author_id:
                archives.bind_post(conn, root_author_id, root_post_id, job["id"])
            for prepared in rows:
                record = by_id[prepared[0]]
                if kind == "post" and str(record.get("author_id", "")).isdigit():
                    archives.bind_post(conn, record["author_id"], record["tweet_id"], job["id"],
                                       (record.get("user") or {}).get("screen_name"))
                unique = conn.execute("""INSERT INTO public.sm_observations(job_id,observation_key,entity_id,kind)
                    VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING entity_id
                """, (job["id"], page_key, record["tweet_id"], kind)).fetchone()
                if not unique:
                    continue
                # A per-entity lock also protects the first insert and out-of-order observations.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (kind+":"+record["tweet_id"],))
                digest = content_hash(record)
                observed = self._save_record(conn, record, kind, root_post_id, observed_at, digest)
                counts["inserted" if observed["new"] else "updated"] += 1
                conn.execute("""INSERT INTO public.sm_versions(entity_id,kind,content_hash,observed_at,payload)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (record["tweet_id"], kind, digest, observed_at, Jsonb(record)))
                metrics = {key: (record.get("_metrics") or {}).get(key) for key in METRICS}
                missing = [key for key, value in metrics.items() if value is None]
                snapshot_id = uuid4()
                conn.execute("""INSERT INTO public.sm_snapshots(snapshot_id,entity_id,kind,observed_at,
                    scheduled_at,job_id,observation_key,metrics,missing_fields,source,likes,comments,reposts,views,quotes)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (snapshot_id, record["tweet_id"], kind, observed_at, job["created_at"], job["id"],
                      page_key, Jsonb(metrics), Jsonb(missing), source, *[metrics[key] for key in METRICS]))
                if kind == "post":
                    if observed["current"]:
                        conn.execute("""INSERT INTO public.scweet_mcp_tweets(tweet_id,timestamp,author,lang,text,payload)
                            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(tweet_id) DO UPDATE SET
                            timestamp=excluded.timestamp,author=excluded.author,lang=excluded.lang,text=excluded.text,payload=excluded.payload
                        """, prepared)
                    active_monitor = monitor or conn.execute("SELECT * FROM public.sm_monitors WHERE author_id=%s AND enabled", (record.get("author_id"),)).fetchone()
                    if active_monitor and observed["current"]:
                        previous = observed["previous"]
                        if previous and previous["content_hash"] != digest:
                            self.event(conn, "content_changed", active_monitor, record["tweet_id"],
                                f"content:{job['id']}:{page_key}:{record['tweet_id']}",
                                {"previous_hash": previous["content_hash"], "current_hash": digest,
                                 "previous_text": previous["payload"].get("text"), "current_text": record.get("text")})
                        self._visible(conn, job, page_key, record["tweet_id"], active_monitor, observed_at)
                        self._next_slots(conn, record, active_monitor, observed_at)
                        if (observed["new"] and active_monitor["watermark"] is not None
                                and active_monitor["config"]["alerts"]["new_posts"]
                                and prepared[1] and prepared[1] >= active_monitor["created_at"]):
                            self.event(conn, "new_post", active_monitor, record["tweet_id"],
                                       f"post:{active_monitor['id']}:{record['tweet_id']}", {"post_id": record["tweet_id"], "snapshot_id": str(snapshot_id)})
                        self._alerts(conn, active_monitor, record["tweet_id"], metrics, observed_at, snapshot_id)
            for post_id in missing_post_ids or []:
                self._missing(conn, job, page_key, post_id, observed_at)
            progress = dict(job["progress"])
            progress["pages"] += 1
            progress["fetched"] += len(records)
            for key in counts:
                progress[key] += counts[key]
            self.jobs.checkpoint(conn, job, checkpoint, progress)
        return counts

    def _save_record(self, conn, record, kind, root_post_id, observed_at, digest):
        entity_id = record["tweet_id"]
        timestamp = utc_timestamp(record["timestamp"], "timestamp") if record.get("timestamp") else None
        if kind == "post":
            previous = conn.execute("SELECT last_observed_at,content_hash,payload FROM public.sm_posts WHERE post_id=%s", (entity_id,)).fetchone()
            conn.execute("""INSERT INTO public.sm_posts(post_id,author_id,published_at,post_type,last_observed_at,payload,content_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(post_id) DO UPDATE SET author_id=excluded.author_id,
                published_at=excluded.published_at,post_type=excluded.post_type,last_observed_at=excluded.last_observed_at,
                payload=excluded.payload,content_hash=excluded.content_hash
                WHERE sm_posts.last_observed_at<=excluded.last_observed_at
            """, (entity_id, record.get("author_id"), timestamp, post_type(record), observed_at, Jsonb(record), digest))
        else:
            previous = conn.execute("SELECT last_observed_at FROM public.sm_comments WHERE comment_id=%s", (entity_id,)).fetchone()
            conn.execute("""INSERT INTO public.sm_comments(comment_id,root_post_id,parent_comment_id,author_id,published_at,
                last_observed_at,payload,content_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(comment_id) DO UPDATE SET last_observed_at=excluded.last_observed_at,payload=excluded.payload,
                content_hash=excluded.content_hash,parent_comment_id=excluded.parent_comment_id
                WHERE sm_comments.last_observed_at<=excluded.last_observed_at
            """, (entity_id, root_post_id, record.get("in_reply_to_tweet_id"), record.get("author_id"), timestamp,
                  observed_at, Jsonb(record), digest))
        return {"new": previous is None, "current": previous is None or previous["last_observed_at"] <= observed_at,
                "previous": previous}

    def _visible(self, conn, job, page_key, post_id, monitor, now):
        row = conn.execute("SELECT availability,availability_checked_at FROM sm_posts WHERE post_id=%s FOR UPDATE", (post_id,)).fetchone()
        if row["availability_checked_at"] and row["availability_checked_at"] > now:
            return
        conn.execute("UPDATE sm_posts SET availability='visible',availability_checked_at=%s,missing_count=0 WHERE post_id=%s", (now, post_id))
        if row["availability"] != "visible":
            self.event(conn, "availability_changed", monitor, post_id, f"state:{job['id']}:{page_key}:{post_id}",
                       {"previous": row["availability"], "current": "visible", "reason": "lookup_or_timeline_returned_post"})

    def _missing(self, conn, job, page_key, post_id, now):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("post:"+post_id,))
        row = conn.execute("SELECT * FROM sm_posts WHERE post_id=%s FOR UPDATE", (post_id,)).fetchone()
        if not row:
            return
        monitor = conn.execute("SELECT * FROM sm_monitors WHERE author_id=%s AND enabled", (row["author_id"],)).fetchone()
        if not monitor or (row["availability_checked_at"] and row["availability_checked_at"] > now):
            return
        if row["missing_count"] and row["availability_checked_at"] and (now-row["availability_checked_at"]).total_seconds() < self.store.config.mcp_missing_check_interval_s:
            return
        unique = conn.execute("""INSERT INTO sm_observations(job_id,observation_key,entity_id,kind)
            VALUES(%s,%s,%s,'availability') ON CONFLICT DO NOTHING RETURNING entity_id""",
            (job["id"], page_key, post_id)).fetchone()
        if not unique:
            return
        count = row["missing_count"]+1
        current = "suspected_deleted" if count >= self.store.config.mcp_missing_confirmations else "unavailable"
        conn.execute("UPDATE sm_posts SET availability=%s,availability_checked_at=%s,missing_count=%s WHERE post_id=%s", (current, now, count, post_id))
        if current != row["availability"]:
            self.event(conn, "availability_changed", monitor, post_id, f"state:{job['id']}:{page_key}:{post_id}",
                       {"previous": row["availability"], "current": current, "missing_count": count,
                        "reason": "recognized_lookup_missing", "deletion_confirmed": False})

    def changes(self, author_id, since=None, until=None, post_id=None, limit=50, cursor=None):
        filters = [author_id, since, until, post_id]
        clauses = ["m.author_id=%s", "e.type IN ('new_post','content_changed','availability_changed')"]
        values = [author_id]
        start = utc_timestamp(since, "since") if since else None
        end = utc_timestamp(until, "until") if until else None
        if start and end and start >= end:
            raise StoreError("INVALID_INPUT: since must precede until.")
        for op, value in ((">=", start), ("<", end)):
            if value:
                clauses.append("e.observed_at"+op+"%s")
                values.append(value)
        if post_id:
            clauses.append("e.post_id=%s")
            values.append(post_id)
        if cursor:
            last = decode_cursor(cursor, filters)
            if len(last) != 2:
                raise StoreError("INVALID_INPUT: Use the returned post-change cursor.")
            clauses.append("(e.observed_at,e.id)>(%s::timestamptz,%s::uuid)")
            values.extend(last)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT e.id AS event_id,e.type,e.post_id,e.observed_at,e.payload FROM sm_events e JOIN sm_monitors m ON m.id=e.monitor_id WHERE "+
                " AND ".join(clauses)+" ORDER BY e.observed_at,e.id LIMIT %s", values+[limit+1]).fetchall()
        return page_result(rows, limit, filters, ["observed_at", "event_id"])

    def _next_slots(self, conn, record, monitor, now):
        if not record.get("timestamp"):
            return
        age = max(0, (now-utc_timestamp(record["timestamp"], "timestamp")).total_seconds()/3600)
        interval = next((item["interval_seconds"] for item in monitor["config"]["metric_schedule"]
                         if age <= item["max_post_age_hours"]), None)
        comment_interval = monitor["config"].get("comments_interval_seconds")
        conn.execute("""UPDATE public.sm_posts SET next_metric_at=%s,
            next_comments_at=coalesce(next_comments_at,%s) WHERE post_id=%s""",
                     (now+timedelta(seconds=interval) if interval else None,
                      now+timedelta(seconds=comment_interval) if comment_interval and interval else None, record["tweet_id"]))

    def _alerts(self, conn, monitor, post_id, metrics, now, snapshot_id):
        for rule in monitor["config"]["alerts"]["rules"]:
            metric, window = rule["metric"], rule["window_seconds"]
            current = metrics.get(metric)
            if current is None:
                continue
            baseline = conn.execute("""SELECT snapshot_id,metrics FROM public.sm_snapshots
                WHERE entity_id=%s AND kind='post' AND observed_at<=%s AND observed_at>=%s
                ORDER BY observed_at DESC LIMIT 1
            """, (post_id, now-timedelta(seconds=window), now-timedelta(seconds=window*2))).fetchone()
            if not baseline or baseline["metrics"].get(metric) is None:
                continue
            old = baseline["metrics"][metric]
            delta = current-old
            directed = delta if rule["direction"] == "increase" else -delta
            if directed < rule["min_delta"] or (old > 0 and rule["min_ratio"] is not None and directed/old < rule["min_ratio"]):
                continue
            rule_id = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:20]
            recent = conn.execute("""SELECT 1 FROM public.sm_events WHERE monitor_id=%s AND post_id=%s
                AND payload->>'rule_id'=%s AND observed_at>%s LIMIT 1
            """, (monitor["id"], post_id, rule_id, now-timedelta(seconds=rule["cooldown_seconds"]))).fetchone()
            if not recent:
                self.event(conn, "metric_change", monitor, post_id, f"metric:{monitor['id']}:{snapshot_id}:{rule_id}",
                           {"rule_id": rule_id, "metric": metric, "previous": old, "current": current, "delta": delta,
                            "baseline_snapshot_id": str(baseline["snapshot_id"]), "current_snapshot_id": str(snapshot_id)})

    def snapshots(self, post_id, since=None, until=None, metrics=None, limit=50, cursor=None):
        filters = [post_id, since, until, metrics]
        clauses, values = ["entity_id=%s", "kind='post'"], [post_id]
        for column, value in ((">=", since), ("<", until)):
            if value:
                clauses.append("observed_at"+column+"%s")
                values.append(utc_timestamp(value, "time range"))
        if since and until and utc_timestamp(since, "since") >= utc_timestamp(until, "until"):
            raise StoreError("INVALID_INPUT: since must precede until.")
        if cursor:
            last = decode_cursor(cursor, filters)
            if len(last) != 2:
                raise StoreError("INVALID_INPUT: Invalid snapshot cursor.")
            clauses.append("(observed_at,snapshot_id)<(%s::timestamptz,%s::uuid)")
            values.extend(last)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT snapshot_id,entity_id AS post_id,observed_at,scheduled_at,metrics,missing_fields,job_id AS collection_run_id,source FROM public.sm_snapshots WHERE "
                                + " AND ".join(clauses)+" ORDER BY observed_at DESC,snapshot_id DESC LIMIT %s", [*values, limit+1]).fetchall()
        for row in rows:
            row["quality"] = "partial" if row["missing_fields"] else "complete"
            if metrics:
                row["metrics"] = {key: row["metrics"].get(key) for key in metrics}
        return page_result(rows, limit, filters, ["observed_at", "snapshot_id"])

    def comments(self, post_id, scope="thread", parent_comment_id=None, published_since=None,
                 first_seen_since=None, author_id=None, limit=50, cursor=None):
        filters = [post_id, scope, parent_comment_id, published_since, first_seen_since, author_id]
        clauses, params = ["root_post_id=%s"], [post_id]
        if scope == "direct":
            clauses.append("parent_comment_id=%s")
            params.append(post_id)
        for column, value in (("parent_comment_id", parent_comment_id), ("author_id", author_id)):
            if value:
                clauses.append(column+"=%s")
                params.append(value)
        for column, value in (("published_at", published_since), ("first_seen_at", first_seen_since)):
            if value:
                clauses.append(column+">=%s")
                params.append(utc_timestamp(value, column))
        if cursor:
            last = decode_cursor(cursor, filters)
            if len(last) != 2:
                raise StoreError("INVALID_INPUT: Invalid comment cursor.")
            clauses.append("(first_seen_at,comment_id)>(%s::timestamptz,%s)")
            params.extend(last)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM public.sm_comments WHERE "+" AND ".join(clauses)+" ORDER BY first_seen_at,comment_id LIMIT %s", [*params, limit+1]).fetchall()
            coverage = self.coverage(conn, post_id, scope)
        page = page_result(rows, limit, filters, ["first_seen_at", "comment_id"])
        page["records"] = [comment_record(row) for row in page["records"]]
        page["coverage"] = coverage
        return page

    def coverage(self, conn, post_id, scope):
        row = conn.execute("SELECT coverage FROM public.sm_coverage WHERE root_post_id=%s AND scope=%s", (post_id, scope)).fetchone()
        return wire(row["coverage"]) if row else {"scope": scope, "status": "unknown", "fetched_unique": 0,
            "platform_reported_count": None, "pending_branches": 0, "stop_reason": "not_collected", "checked_at": utcnow().isoformat()}

    def events(self, consumer_ref, types=None, limit=50, cursor=None):
        filters = [consumer_ref, types]
        clauses = ["NOT EXISTS (SELECT 1 FROM public.sm_event_acks a WHERE a.event_id=e.id AND a.consumer_ref=%s)"]
        params = [consumer_ref]
        if types:
            clauses.append("type=ANY(%s)")
            params.append(types)
        if cursor:
            last = decode_cursor(cursor, filters)
            if len(last) != 2:
                raise StoreError("INVALID_INPUT: Invalid event cursor.")
            clauses.append("(observed_at,id)>(%s::timestamptz,%s::uuid)")
            params.extend(last)
        with self.store._connection() as conn:
            rows = conn.execute("SELECT e.id AS event_id,e.type,e.monitor_id,e.post_id,e.observed_at,e.payload FROM public.sm_events e WHERE "+" AND ".join(clauses)+" ORDER BY observed_at,id LIMIT %s", [*params, limit+1]).fetchall()
        return page_result(rows, limit, filters, ["observed_at", "event_id"])

    def acknowledge(self, consumer_ref, event_ids):
        acknowledged, already = [], []
        with self.store._connection() as conn:
            for event_id in event_ids:
                event_id = identifier(event_id)
                if not conn.execute("SELECT 1 FROM public.sm_events WHERE id=%s", (event_id,)).fetchone():
                    raise StoreError("NOT_FOUND: Event does not exist.")
                row = conn.execute("INSERT INTO public.sm_event_acks(consumer_ref,event_id) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event_id", (consumer_ref, event_id)).fetchone()
                (acknowledged if row else already).append(str(event_id))
        return {"acknowledged_ids": acknowledged, "already_acknowledged_ids": already}


def comment_record(row):
    record = row["payload"]
    metrics = record.get("_metrics", {})
    return wire({"comment_id": row["comment_id"], "root_post_id": row["root_post_id"],
                 "parent_comment_id": row["parent_comment_id"], "author": {
                     "id": row["author_id"], "handle": (record.get("user") or {}).get("screen_name"),
                     "display_name": (record.get("user") or {}).get("name")},
                 "text": record.get("text"), "published_at": row["published_at"],
                 "first_seen_at": row["first_seen_at"], "last_observed_at": row["last_observed_at"],
                 "likes": metrics.get("likes"), "reply_count": metrics.get("comments"),
                 "availability": row["availability"], "raw": record.get("raw")})
