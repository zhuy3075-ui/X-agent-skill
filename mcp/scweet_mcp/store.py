from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from Scweet.config import ScweetConfig


logger = logging.getLogger("scweet_mcp")


class StoreError(ValueError):
    """An input or database failure with a safe, actionable message."""


def utc_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"INVALID_INPUT: {field} must be a timestamp string with a timezone.")
    try:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise StoreError(
            f"INVALID_INPUT: {field} must use ISO 8601 with a timezone or the Scweet timestamp format."
        ) from None


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 50:
        raise StoreError("INVALID_INPUT: JSON nesting exceeds 50 levels. Reduce the record depth.")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise StoreError("INVALID_INPUT: JSON object keys must be strings.")
            if key.casefold().replace("-", "_") in {
                "auth_token", "authorization", "cookie", "cookies", "password",
                "ct0", "twofa", "two_fa", "totp_secret", "2fa", "2fa_secret",
            }:
                raise StoreError("INVALID_INPUT: Credential fields are not allowed. Submit tweet data only.")
            _check_json(key, depth + 1)
            _check_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json(child, depth + 1)
    elif isinstance(value, str):
        if "\x00" in value:
            raise StoreError("INVALID_INPUT: PostgreSQL JSONB cannot store a NUL character. Remove it.")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise StoreError("INVALID_INPUT: Records must contain JSON values only.")
    elif isinstance(value, float) and not math.isfinite(value):
        raise StoreError("INVALID_INPUT: JSON numbers must be finite.")


class TweetStore:
    """PostgreSQL storage. Each operation owns one bounded transaction."""

    def __init__(self, config: ScweetConfig, database_url: Optional[str] = None):
        self.config = config
        self._database_url = database_url or os.environ.get(config.mcp_database_url_env)
        if not self._database_url or not self._database_url.strip():
            raise StoreError(
                f"CONFIG_ERROR: Set {config.mcp_database_url_env} to a PostgreSQL connection string."
            )

    @contextmanager
    def _connection(self, *, repeatable_read=False, read_only=False) -> Iterator[psycopg.Connection]:
        try:
            with psycopg.connect(
                self._database_url, connect_timeout=self.config.mcp_connect_timeout_s,
                row_factory=dict_row, application_name="scweet_mcp",
            ) as conn:
                if repeatable_read:
                    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                if read_only:
                    conn.read_only = True
                conn.execute("SELECT set_config('statement_timeout', %s, true)",
                             (str(self.config.mcp_statement_timeout_ms),))
                conn.execute("SELECT set_config('lock_timeout', %s, true)",
                             (str(self.config.mcp_lock_timeout_ms),))
                conn.execute("SET LOCAL timezone = 'UTC'")
                yield conn
        except psycopg.Error as exc:
            # PostgreSQL errors can contain credentials, SQL parameters and tweet text.
            code = exc.sqlstate or "connection"
            logger.error("PostgreSQL operation failed (SQLSTATE %s)", code)
            if code in {"55P03", "57014", "40P01", "40001"}:
                message = "Database lock, timeout or transaction conflict. Narrow the request or retry."
            elif code in {"42P01", "42703"}:
                message = "The data schema is missing or incompatible. Run server.py --init-db first."
            elif code == "42501":
                message = "Database permission denied. Grant the service role access to its data table."
            else:
                message = "Check the PostgreSQL connection, permissions and server health, then retry."
            raise StoreError(f"DATABASE_ERROR: {message}") from None

    def initialize(self) -> None:
        """Create the service table and indexes with a schema-owner role."""
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(731605921)")
            conn.execute(schema)
            conn.execute(Path(__file__).with_name("monitor_schema.sql").read_text(encoding="utf-8"))
        logger.info("PostgreSQL data schema is ready")

    def check_schema(self) -> None:
        with self._connection() as conn:
            conn.execute("SELECT tweet_id, timestamp, sort_time, author, lang, search_vector, payload FROM public.scweet_mcp_tweets LIMIT 0")
            version = conn.execute("SELECT max(version) AS version FROM public.sm_schema_version").fetchone()
            if version["version"] != 3:
                raise StoreError("CONFIG_ERROR: Monitoring schema version 3 is required. Run server.py --init-db with the schema-owner role before starting the updated service.")
            conn.execute("SELECT id,status,lease_token,checkpoint FROM public.sm_jobs LIMIT 0")

    @staticmethod
    def _string(value: Any, field: str, *, optional: bool = False) -> Optional[str]:
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or "\x00" in value:
            raise StoreError(f"INVALID_INPUT: {field} must be a nonempty string of at most 512 characters, without NUL.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise StoreError(f"INVALID_INPUT: {field} must contain valid UTF-8 text.") from None
        return value

    def prepare(self, records: list[dict[str, Any]]) -> list[tuple]:
        """Validate the entire batch before connecting or writing."""
        if not isinstance(records, list) or not 1 <= len(records) <= self.config.mcp_batch_limit:
            raise StoreError(f"INVALID_INPUT: records must hold 1 to {self.config.mcp_batch_limit} tweet objects.")
        rows = {}
        for record in records:
            if not isinstance(record, dict):
                raise StoreError("INVALID_INPUT: Each record must be a JSON object from Scweet.search().")
            _check_json(record)
            tweet_id = self._string(record.get("tweet_id"), "tweet_id")
            timestamp = record.get("timestamp")
            timestamp = utc_timestamp(timestamp, "timestamp") if timestamp is not None else None
            user = record.get("user")
            if user is not None and not isinstance(user, dict):
                raise StoreError("INVALID_INPUT: user must be a JSON object or null.")
            author = self._string((user or {}).get("screen_name"), "user.screen_name", optional=True)
            lang = self._string(record.get("lang"), "lang", optional=True)
            texts = [record.get("text"), record.get("embedded_text")]
            if any(value is not None and not isinstance(value, str) for value in texts):
                raise StoreError("INVALID_INPUT: text and embedded_text must be strings or null.")
            try:
                payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                size = len(payload.encode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise StoreError("INVALID_INPUT: Record cannot be encoded as UTF-8 JSON. Check its values.") from None
            if size > self.config.mcp_record_max_bytes:
                raise StoreError(f"INVALID_INPUT: Record exceeds {self.config.mcp_record_max_bytes} UTF-8 bytes.")
            rows[tweet_id] = (tweet_id, timestamp, author, lang, "\n".join(v for v in texts if v), Jsonb(json.loads(payload)))
        # Consistent lock order reduces deadlocks between overlapping batches.
        return [rows[key] for key in sorted(rows)]

    def write(self, records: list[dict[str, Any]]) -> dict[str, int]:
        rows = self.prepare(records)
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO public.scweet_mcp_tweets(tweet_id, timestamp, author, lang, text, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(tweet_id) DO UPDATE SET timestamp=excluded.timestamp,
                        author=excluded.author, lang=excluded.lang, text=excluded.text, payload=excluded.payload
                """, rows)
        logger.info("Stored %d distinct tweets from %d records", len(rows), len(records))
        return {"received": len(records), "stored": len(rows)}

    def get(self, tweet_id: str) -> dict[str, Any]:
        self._string(tweet_id, "tweet_id")
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM public.scweet_mcp_tweets WHERE tweet_id=%s", (tweet_id,)).fetchone()
        return {"found": row is not None, "record": row["payload"] if row else None}

    def query(
        self, *, author: Optional[str] = None, lang: Optional[str] = None,
        since: Optional[str] = None, until: Optional[str] = None,
        limit: int = 50, cursor: Optional[str] = None, text: Optional[str] = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= self.config.mcp_page_limit:
            raise StoreError(f"INVALID_INPUT: limit must be an integer from 1 to {self.config.mcp_page_limit}.")
        clauses, params = [], []
        for field, value in (("author", author), ("lang", lang)):
            if value is not None:
                self._string(value, field)
                clauses.append(f"lower({field}) = lower(%s)")
                params.append(value)
        start = utc_timestamp(since, "since") if since is not None else None
        end = utc_timestamp(until, "until") if until is not None else None
        if start is not None and end is not None and start >= end:
            raise StoreError("INVALID_INPUT: since must be earlier than until. Use a nonempty time range.")
        for operator, value in ((">=", start), ("<", end)):
            if value is not None:
                clauses.append(f"sort_time {operator} %s")
                params.append(value)
        if start is not None or end is not None:
            clauses.append("timestamp IS NOT NULL")
        if text is not None:
            self._string(text, "text")
            clauses.append("search_vector @@ phraseto_tsquery('simple', %s)")
            params.append(text)
        fingerprint = hashlib.sha256(json.dumps(
            [author, lang, since, until, text], ensure_ascii=True,
        ).encode()).hexdigest()
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ValueError("size")
                token = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if not isinstance(token, dict) or token.get("v") != 1 or token.get("q") != fingerprint:
                    raise ValueError("query")
                last_id = self._string(token["id"], "cursor ID")
                last_time = utc_timestamp(token["time"], "cursor time") if token["time"] else "-infinity"
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise StoreError("INVALID_INPUT: Invalid cursor. Use next_cursor with the same query filters.") from None
            clauses.append("(sort_time, tweet_id) < (%s::timestamptz, %s)")
            params.extend([last_time, last_id])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = "SELECT payload, timestamp, tweet_id FROM public.scweet_mcp_tweets" + where
        sql += " ORDER BY sort_time DESC, tweet_id DESC LIMIT %s"
        with self._connection() as conn:
            rows = conn.execute(sql, [*params, limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more:
            last = rows[-1]
            token = {"v": 1, "q": fingerprint, "id": last["tweet_id"],
                     "time": last["timestamp"].isoformat() if last["timestamp"] else None}
            next_cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        return {"records": [row["payload"] for row in rows], "count": len(rows),
                "has_more": more, "next_cursor": next_cursor}
