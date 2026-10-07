"""Bounded analysis and streaming exports from a consistent database snapshot."""
import base64
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import median
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from scweet_mcp.jobs import JobQueue, identifier, utcnow, wire
from scweet_mcp.observations import Observations, comment_record
from scweet_mcp.store import StoreError, utc_timestamp

EXPORT_FIELDS = ("comment_id", "root_post_id", "parent_comment_id", "author_id", "author_handle",
                 "author_name", "text", "published_at", "first_seen_at", "last_observed_at", "likes", "reply_count", "availability")


def csv_value(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


def tokens(text):
    # Bigrams make Chinese output explicit and reproducible without a model dependency.
    words = re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]+", text.lower())
    stop = {"the", "and", "that", "this", "with", "for", "you", "are", "from", "have"}
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            yield from (word[i:i+2] for i in range(len(word)-1))
        elif word not in stop:
            yield word


class Reports:
    def __init__(self, store):
        self.store, self.jobs = store, JobQueue(store)

    def export(self, job):
        params = job["params"]
        fields = params.get("fields") or list(EXPORT_FIELDS)
        if any(field not in EXPORT_FIELDS for field in fields):
            raise StoreError("INVALID_INPUT: Use the documented export fields.")
        artifact_id = uuid4()
        root = Path(self.store.config.mcp_artifact_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / (str(artifact_id) + "." + params["format"])
        count = 0
        try:
            with self.store._connection(repeatable_read=True, read_only=True) as conn:
                coverage = Observations(self.store).coverage(conn, params["post_id"], params["scope"])
                if params.get("require_complete", True) and coverage["status"] != "exhausted_visible":
                    raise StoreError("INCOMPLETE_COVERAGE: Sync all visible branches, or set require_complete=false to export a partial data set.")
                with path.open("w", encoding="utf-8", newline="") as output, conn.cursor(name="comment_export") as cursor:
                    writer = csv.DictWriter(output, fieldnames=fields) if params["format"] == "csv" else None
                    if writer:
                        writer.writeheader()
                    query = "SELECT * FROM sm_comments WHERE root_post_id=%s"
                    values = [params["post_id"]]
                    if params["scope"] == "direct":
                        query += " AND parent_comment_id=%s"
                        values.append(params["post_id"])
                    cursor.execute(query + " ORDER BY first_seen_at,comment_id", values)
                    for row in cursor:
                        record = comment_record(wire(row))
                        record.update(author_id=record["author"]["id"], author_handle=record["author"]["handle"], author_name=record["author"]["display_name"])
                        selected = {field: record.get(field) for field in fields}
                        if writer:
                            writer.writerow({key: csv_value(value) for key, value in selected.items()})
                        else:
                            output.write(json.dumps(selected, ensure_ascii=False) + "\n")
                        count += 1
                        if count % 500 == 0:
                            with self.store._connection() as control:
                                self.jobs.guard(control, job)
                # Export transaction is read-only apart from its row lock. It must not block heartbeat.
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(block)
            manifest = {"artifact_id": str(artifact_id), "resource_uri": f"scweet://exports/{artifact_id}/0",
                        "format": params["format"], "fields": fields, "row_count": count,
                        "byte_count": path.stat().st_size, "sha256": digest.hexdigest(), "coverage": coverage,
                        "created_at": utcnow().isoformat(), "csv_formula_escaped": params["format"] == "csv"}
            with self.store._connection() as conn:
                self.jobs.guard(conn, job)
                conn.execute("INSERT INTO sm_exports(id,job_id,relative_path,manifest) VALUES(%s,%s,%s,%s)",
                             (artifact_id, job["id"], path.name, Jsonb(manifest)))
            return manifest
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def read_chunk(self, artifact_id, offset=0):
        if offset < 0:
            raise StoreError("INVALID_INPUT: offset must be nonnegative.")
        with self.store._connection() as conn:
            row = conn.execute("SELECT relative_path,manifest FROM sm_exports WHERE id=%s", (identifier(artifact_id),)).fetchone()
        if not row:
            raise StoreError("NOT_FOUND: Export artifact does not exist.")
        root = Path(self.store.config.mcp_artifact_dir).resolve()
        path = (root / row["relative_path"]).resolve()
        if path.parent != root or path.is_symlink():
            raise StoreError("INVALID_ARTIFACT: Export path is outside the artifact directory.")
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                block = stream.read(self.store.config.mcp_resource_chunk_bytes)
        except OSError:
            raise StoreError("ARTIFACT_UNAVAILABLE: Restore the export file or submit a new export.") from None
        next_offset = offset + len(block)
        return {"manifest": row["manifest"], "offset": offset, "encoding": "base64", "content": base64.b64encode(block).decode(),
                "next_uri": f"scweet://exports/{artifact_id}/{next_offset}" if next_offset < row["manifest"]["byte_count"] else None}

    def analyze(self, job):
        params, kind = job["params"], job["kind"]
        zone = ZoneInfo(params.get("timezone", "UTC"))
        limit = min(params.get("sample_limit", self.store.config.mcp_analysis_max_records), self.store.config.mcp_analysis_max_records)
        terms, values = [], []
        if kind == "analyze_comments":
            table, key, target = "sm_comments", "root_post_id", params["post_id"]
        else:
            table, key, target = "sm_posts", "author_id", params["author_id"]
        terms.append(key + "=%s")
        values.append(target)
        for field, operator in (("since", ">="), ("until", "<")):
            if params.get(field):
                terms.append("published_at" + operator + "%s")
                values.append(utc_timestamp(params[field], field))
        if kind == "analyze_author" and params.get("post_types"):
            terms.append("post_type=ANY(%s)")
            values.append(params["post_types"])
        where = " AND ".join(terms)
        with self.store._connection(repeatable_read=True, read_only=True) as conn:
            total = conn.execute(f"SELECT count(*) AS count FROM {table} WHERE " + where, values).fetchone()["count"]
            # Time-stratified selection avoids a latest-only sample; no unbounded payload materialization.
            rows = conn.execute(f"""SELECT * FROM (SELECT *,row_number() OVER(ORDER BY published_at, {('comment_id' if kind == 'analyze_comments' else 'post_id')}) AS rn
                FROM {table} WHERE {where}) numbered WHERE mod(rn-1,%s)=0 ORDER BY rn LIMIT %s""",
                values + [max(1, (total + limit - 1)//limit), limit]).fetchall()
            coverage = Observations(self.store).coverage(conn, target, "thread") if kind == "analyze_comments" else None
        keywords, active, trend, hours, weekdays, sentiment = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
        lengths, times, evidence = [], [], []
        flags = Counter()
        for row in rows:
            text = row["payload"].get("text", "")
            keywords.update(set(tokens(text)))
            active[row.get("author_id") or "unknown"] += 1
            lengths.append(len(text))
            flags["question_posts"] += int("?" in text or "？" in text)
            flags["link_posts"] += int(bool(row["payload"].get("urls")))
            flags["media_posts"] += int(any((row["payload"].get("media") or {}).values()))
            timestamp = row["published_at"]
            if timestamp:
                local = timestamp.astimezone(zone)
                trend[local.strftime("%Y-%m-%dT%H:00%z" if params.get("bucket") == "hour" else "%Y-%m-%d")] += 1
                hours[str(local.hour)] += 1
                weekdays[str(local.weekday())] += 1
                times.append(timestamp)
            if len(evidence) < params.get("top_k", 20):
                evidence.append({"id": row.get("comment_id", row.get("post_id")), "text": text[:500]})
            if params.get("include_sentiment"):
                lowered = text.lower()
                positive = any(word in lowered for word in ("great", "love", "thanks", "喜欢", "谢谢", "优秀"))
                negative = any(word in lowered for word in ("hate", "terrible", "bad", "讨厌", "失望", "垃圾"))
                sentiment["positive" if positive and not negative else "negative" if negative and not positive else "uncertain"] += 1
        times.sort()
        gaps = [(right-left).total_seconds() for left, right in zip(times, times[1:])]
        top = params.get("top_k", 20)
        result = {"analysis_version": "descriptive-v1", "population_count": total, "sample_count": len(rows),
                  "sampling": "time_stratified" if total > limit else "all_stored", "timezone": str(zone),
                  "coverage": coverage, "trend": dict(sorted(trend.items())), "trend_basis": "sample_publish_time",
                  "keywords": [{"term": term, "document_count": count} for term, count in keywords.most_common(top)],
                  "tokenizer": "english_words_chinese_bigrams_v1", "active_authors": active.most_common(top),
                  "sentiment": {"method": "lexicon_v1", "counts": dict(sentiment), "limitation": "No sarcasm or context model; uncertain is not neutral."} if params.get("include_sentiment") else None,
                  "style": {"median_length": median(lengths) if lengths else None, "median_gap_seconds": median(gaps) if gaps else None,
                            "local_hours": dict(hours), "local_weekdays": dict(weekdays), **flags}, "evidence": evidence,
                  "limitations": ["Describes stored visible records; it does not prove platform-wide completeness.", "Sample trends and gaps are not population estimates when sampling is active."]}
        if kind == "analyze_author" and params.get("include_comments"):
            with self.store._connection() as conn:
                demand_terms = ["p.author_id=%s"]
                demand_values = [target]
                for field, operator in (("since", ">="), ("until", "<")):
                    if params.get(field):
                        demand_terms.append("p.published_at"+operator+"%s")
                        demand_values.append(utc_timestamp(params[field], field))
                if params.get("post_types"):
                    demand_terms.append("p.post_type=ANY(%s)")
                    demand_values.append(params["post_types"])
                comments = conn.execute("""SELECT c.comment_id,c.payload FROM sm_comments c JOIN sm_posts p ON p.post_id=c.root_post_id
                    WHERE """ + " AND ".join(demand_terms) + " ORDER BY c.published_at DESC LIMIT %s", demand_values + [limit]).fetchall()
            demand = Counter(term for row in comments for term in set(tokens(row["payload"].get("text", ""))))
            result["comment_demand"] = {"sample_count": len(comments), "sampling": "latest_stored",
                "keywords": demand.most_common(top), "evidence": [{"comment_id": row["comment_id"], "text": row["payload"].get("text", "")[:500]} for row in comments[:top]],
                "interpretation": "The skill must infer demand from cited comments; these counts are not intent labels."}
        if kind == "analyze_author":
            from scweet_mcp.research import author_evidence
            result.update(author_id=target, scope={key: params.get(key) for key in ("since", "until", "post_types")})
            result.update(author_evidence(rows, params.get("viral_metric", "likes"), params.get("viral_min_value", 10),
                                          params.get("viral_ratio", 2.0)))
            result["analysis_version"] = "descriptive-v2"
        with self.store._connection() as conn:
            self.jobs.guard(conn, job)
            conn.execute("INSERT INTO sm_analysis_runs(job_id,kind,result) VALUES(%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET result=excluded.result",
                         (job["id"], kind, Jsonb(wire(result))))
        return result
