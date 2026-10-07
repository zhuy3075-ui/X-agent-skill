"""Reproducible author evidence and observed metric growth."""
from collections import Counter
from statistics import median

from scweet_mcp.jobs import wire, page_result, decode_cursor
from scweet_mcp.store import StoreError, utc_timestamp

METRICS = ("likes", "comments", "reposts", "views", "quotes")


def features(row):
    payload = row["payload"]
    text = payload.get("text") or ""
    return {"length": len(text), "question": "?" in text or "？" in text,
            "link": bool(payload.get("urls")), "media": any((payload.get("media") or {}).values()),
            "post_type": row["post_type"]}


def author_evidence(rows, metric="likes", minimum=10, ratio=2.0, top=20):
    from scweet_mcp.reports import tokens
    known = [row for row in rows if (row["payload"].get("_metrics") or {}).get(metric) is not None]
    values = [(row["payload"]["_metrics"][metric], row) for row in known]
    baseline = median([value for value, _ in values]) if values else None
    threshold = max(minimum, baseline * ratio) if baseline is not None else None
    selected = [(value, row) for value, row in sorted(values, key=lambda pair: (-pair[0], pair[1]["post_id"]))
                if value >= threshold][:top]
    def describe(group):
        items = [features(row) for row in group]
        return {"count": len(items), "median_length": median([r["length"] for r in items]) if items else None,
                **{name + "_rate": sum(r[name] for r in items)/len(items) if items else None for name in ("question", "link", "media")}}
    terms = Counter(term for _, row in selected for term in set(tokens(row["payload"].get("text") or "")))
    engagement = {}
    for name in METRICS:
        observed = [(row["payload"].get("_metrics") or {}).get(name) for row in rows]
        numbers = [n for n in observed if n is not None]
        engagement[name] = {"known_count": len(numbers), "missing_count": len(rows)-len(numbers),
                            "median": median(numbers) if numbers else None, "max": max(numbers) if numbers else None}
    return {"interaction": engagement,
            "viral_materials": {"method": "sample_median_multiplier_v1", "metric": metric, "absolute_floor": minimum,
                "multiplier": ratio, "baseline_median": baseline, "threshold": threshold,
                "known_count": len(known), "missing_count": len(rows)-len(known),
                "examples": [{"post_id": row["post_id"], "url": f"https://x.com/i/status/{row['post_id']}",
                    "metric_value": value, "ratio_to_median": value/baseline if baseline else None,
                    "observed_at": wire(row["last_observed_at"]), "features": features(row),
                    "text": (row["payload"].get("text") or "")[:500]} for value, row in selected],
                "feature_comparison": {"selected": describe([row for _, row in selected]), "sample": describe(rows)},
                "topic_evidence": [{"term": term, "post_count": count} for term, count in terms.most_common(top)],
                "limitations": ["Relative performance in the stored sample, not a universal viral label.",
                    "Latest counts differ in post age and observation time; features do not prove causation."]}}


class Research:
    def __init__(self, store):
        self.store = store

    def growth(self, post_id, since, until, bucket="day"):
        start, end = utc_timestamp(since, "since"), utc_timestamp(until, "until")
        if start >= end or (end-start).total_seconds() > (366 if bucket == "day" else 31)*86400:
            raise StoreError("INVALID_INPUT: since must precede until; use at most 366 days or 31 days of hourly buckets.")
        if bucket not in {"hour", "day"}:
            raise StoreError("INVALID_INPUT: bucket must be hour or day.")
        columns = ",".join(f"max({name}) FILTER (WHERE first_rank=1) AS first_{name}, "
                           f"max({name}) FILTER (WHERE last_rank=1) AS last_{name}" for name in METRICS)
        with self.store._connection(read_only=True) as conn:
            rows = conn.execute(f"""WITH selected AS (
                SELECT *,date_trunc(%s,observed_at AT TIME ZONE 'UTC') AS bucket FROM sm_snapshots
                WHERE entity_id=%s AND kind='post' AND observed_at>=%s AND observed_at<%s), ranked AS (
                SELECT *,row_number() OVER(PARTITION BY bucket ORDER BY observed_at,snapshot_id) first_rank,
                row_number() OVER(PARTITION BY bucket ORDER BY observed_at DESC,snapshot_id DESC) last_rank FROM selected)
                SELECT bucket,min(observed_at) AS first_at,max(observed_at) AS last_at,count(*) AS observations,
                {columns} FROM ranked GROUP BY bucket ORDER BY bucket""", (bucket, post_id, start, end)).fetchall()
        points, previous = [], None
        for row in rows:
            first = {name: row["first_"+name] for name in METRICS}
            last = {name: row["last_"+name] for name in METRICS}
            base = previous if previous else first
            enough = previous is not None or row["observations"] > 1
            changes = {name: {"delta": last[name]-base[name] if enough and last[name] is not None and base[name] is not None else None,
                             "ratio": (last[name]-base[name])/base[name] if enough and last[name] is not None and base[name] else None}
                       for name in METRICS}
            points.append({"bucket": row["bucket"].isoformat()+"Z", "first_at": row["first_at"], "last_at": row["last_at"],
                           "observations": row["observations"], "metrics": last, "growth": changes,
                           "baseline": "previous_bucket_last" if previous else "first_observation_in_bucket"})
            previous = last
        return wire({"post_id": post_id, "since": since, "until": until, "bucket": bucket, "timezone": "UTC",
                     "observation_count": sum(row["observations"] for row in rows), "points": points,
                     "limitations": ["Observed counters only; missing buckets are not zero activity.",
                         "One observation has no growth. Missing metrics and zero-baseline ratios remain null. Negative deltas are retained."]})

    def analyses(self, author_id, limit=20, cursor=None):
        filters = {"author_id": author_id}
        values, terms = [author_id], ["result->>'author_id'=%s"]
        if cursor:
            parts = decode_cursor(cursor, filters)
            if len(parts) != 2:
                raise StoreError("INVALID_INPUT: Use the returned analysis cursor.")
            from scweet_mcp.jobs import identifier
            terms.append("(created_at,job_id)<(%s,%s)")
            values.extend([utc_timestamp(parts[0], "cursor time"), identifier(parts[1])])
        with self.store._connection(read_only=True) as conn:
            rows = conn.execute("""SELECT job_id,kind,created_at,result->'scope' AS scope,
                result->'population_count' AS population_count,result->'sample_count' AS sample_count,
                result->'keywords' AS keywords,jsonb_array_length(result->'viral_materials'->'examples') AS material_count
                FROM sm_analysis_runs WHERE """ + " AND ".join(terms) + " ORDER BY created_at DESC,job_id DESC LIMIT %s",
                                values+[limit+1]).fetchall()
        return page_result(rows, limit, filters, ["created_at", "job_id"])
