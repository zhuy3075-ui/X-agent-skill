"""Account references and shared, conservative request budgets."""
import os
import math
from uuid import uuid4

from psycopg.types.json import Jsonb

from scweet_mcp.jobs import JobQueue, identifier, wire, page_result, decode_cursor
from scweet_mcp.store import StoreError
from scweet_mcp.source_errors import AccountWait


class Accounts:
    def __init__(self, store):
        self.store = store

    def register(self, entries, request_id):
        params = {"entries": [entry.model_dump() for entry in entries]}
        with self.store._connection() as conn:
            receipt = JobQueue(self.store).enqueue("account_registration", params, request_id, conn)
            old = conn.execute("SELECT result FROM sm_jobs WHERE id=%s FOR UPDATE", (identifier(receipt["job_id"]),)).fetchone()
            if old["result"]:
                return old["result"]
            rows = []
            for entry in entries:
                rows.append(conn.execute("""INSERT INTO sm_accounts(id,credential_ref,label,proxy_ref)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(credential_ref) DO UPDATE SET label=excluded.label,
                    proxy_ref=excluded.proxy_ref,health='unchecked' RETURNING id AS account_id,credential_ref,health
                """, (uuid4(), entry.credential_ref, entry.label, entry.proxy_ref)).fetchone())
            result = wire({"accounts": rows})
            conn.execute("UPDATE sm_jobs SET status='succeeded',finished_at=now(),result=%s WHERE id=%s",
                         (Jsonb(result), identifier(receipt["job_id"])))
        return result

    def list(self, status=None, limit=50, cursor=None):
        filters, values, terms = {"status": status}, [], ["true"]
        if status:
            terms.append("health=%s")
            values.append(status)
        if cursor:
            parts = decode_cursor(cursor, filters)
            if len(parts) != 1:
                raise StoreError("INVALID_INPUT: Use the returned account cursor.")
            terms.append("id>%s")
            values.append(identifier(parts[0]))
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT id AS account_id,credential_ref,label,proxy_ref,platform_user_id,
                enabled,health,checked_at,cooldown_until,capabilities,reason_code FROM sm_accounts WHERE """
                + " AND ".join(terms) + " ORDER BY id LIMIT %s", values + [limit + 1]).fetchall()
        return page_result(rows, limit, filters, ["account_id"])

    def set_state(self, account_ids, enabled, reason=None):
        ids = [identifier(value) for value in account_ids]
        with self.store._connection() as conn:
            rows = conn.execute("SELECT id FROM sm_accounts WHERE id=ANY(%s) FOR UPDATE", (ids,)).fetchall()
            if len(rows) != len(set(ids)):
                raise StoreError("NOT_FOUND: Check account_ids before changing account state.")
            conn.execute("UPDATE sm_accounts SET enabled=%s WHERE id=ANY(%s)", (enabled, ids))
        return {"account_ids": list(map(str, ids)), "enabled": enabled}

    def lease(self, account_id=None, check=False, exclude=()):
        with self.store._connection() as conn:
            return conn.execute("""UPDATE sm_accounts SET lease_token=%s,lease_until=now()+%s*interval '1 second'
                WHERE id=(SELECT id FROM sm_accounts WHERE (%s::uuid IS NULL OR id=%s::uuid)
                AND NOT (id=ANY(%s::uuid[]))
                AND (lease_until IS NULL OR lease_until<now()) AND (%s OR (enabled AND health='valid'
                AND (cooldown_until IS NULL OR cooldown_until<=now())))
                ORDER BY (SELECT max(requested_at) FROM sm_account_requests WHERE account_id=sm_accounts.id)
                NULLS FIRST,checked_at NULLS FIRST,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""",
                (uuid4(), self.store.config.mcp_job_lease_s, account_id, account_id, list(exclude), check)).fetchone()

    def unavailable(self, account_id=None):
        with self.store._connection() as conn:
            rows = conn.execute("""SELECT health,enabled,
                greatest(0,extract(epoch FROM greatest(coalesce(cooldown_until,now()),
                coalesce(lease_until,now()))-now())) AS delay FROM sm_accounts
                WHERE (%s::uuid IS NULL OR id=%s::uuid)""", (account_id, account_id)).fetchall()
        usable = [r for r in rows if r["enabled"] and r["health"] == "valid"]
        if usable:
            delay = max(1, math.ceil(min(r["delay"] for r in usable)))
            return AccountWait("ACCOUNT_WAIT: Configured accounts are cooling or busy. Collection will resume after availability returns.",
                               retry_after=delay, details={"eligible_accounts": len(usable)})
        return StoreError("NO_VALID_ACCOUNT: No enabled checked account is available. Configure another token and run accounts_check, or resolve the account health issue.")

    def release(self, account):
        with self.store._connection() as conn:
            conn.execute("UPDATE sm_accounts SET lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s",
                         (account["id"], account["lease_token"]))

    def reserve(self, account):
        cfg = self.store.config
        delay = 0
        with self.store._connection() as conn:
            row = conn.execute("""SELECT * FROM sm_accounts WHERE id=%s AND lease_token=%s
                AND lease_until>now() AND enabled FOR UPDATE""", (account["id"], account["lease_token"])).fetchone()
            if not row:
                raise StoreError("ACCOUNT_LEASE_LOST: Account is disabled or held by another worker.")
            budget = conn.execute("""SELECT count(*) FILTER(WHERE requested_at>now()-%s*interval '1 second') AS window,
                count(*) AS daily,coalesce(sum(tweets),0) AS tweets,
                extract(epoch FROM now()-max(requested_at)) AS gap,
                extract(epoch FROM min(requested_at) FILTER(WHERE requested_at>now()-%s*interval '1 second')
                    +%s*interval '1 second'-now()) AS window_wait,
                extract(epoch FROM min(requested_at)+interval '24 hours'-now()) AS daily_wait
                FROM sm_account_requests
                WHERE account_id=%s AND requested_at>now()-interval '24 hours'""",
                (cfg.rate_limit_window_s, cfg.rate_limit_window_s, cfg.rate_limit_window_s, account["id"])).fetchone()
            if budget["window"] >= cfg.window_request_limit:
                delay = max(delay, float(budget["window_wait"] or cfg.rate_limit_window_s))
            if budget["daily"] >= cfg.daily_requests_limit or budget["tweets"] >= cfg.daily_tweets_limit:
                delay = max(delay, float(budget["daily_wait"] or 86400))
            if budget["gap"] is not None:
                delay = max(delay, cfg.min_delay_s-float(budget["gap"]))
            if row["cooldown_until"]:
                delay = max(delay, float(conn.execute("SELECT extract(epoch FROM %s-now()) AS delay", (row["cooldown_until"],)).fetchone()["delay"]))
            if delay > 0:
                delay = math.ceil(delay)
                conn.execute("UPDATE sm_accounts SET cooldown_until=now()+%s*interval '1 second' WHERE id=%s", (delay, account["id"]))
            else:
                conn.execute("UPDATE sm_accounts SET lease_until=now()+%s*interval '1 second' WHERE id=%s",
                             (cfg.mcp_job_lease_s, account["id"]))
                return conn.execute("INSERT INTO sm_account_requests(account_id) VALUES(%s) RETURNING id", (account["id"],)).fetchone()["id"]
        # Raise after commit: a rollback must not erase the cooldown.
        raise AccountWait("RATE_LIMITED: The local account budget is temporarily exhausted. Wait for quota recovery.", retry_after=delay)

    def observed(self, request_id, count):
        with self.store._connection() as conn:
            conn.execute("UPDATE sm_account_requests SET tweets=%s WHERE id=%s", (count, request_id))

    def health(self, account, health, identity=None, capabilities=None):
        with self.store._connection() as conn:
            # Serialize identity checks, including two new references to the same X account.
            if identity:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("account:" + identity,))
                other = conn.execute("SELECT id FROM sm_accounts WHERE platform_user_id=%s AND id<>%s", (identity, account["id"])).fetchone()
                if other:
                    health, identity = "duplicate", None
            conn.execute("""UPDATE sm_accounts SET health=%s,platform_user_id=coalesce(%s,platform_user_id),
                checked_at=now(),capabilities=%s,reason_code=%s WHERE id=%s AND lease_token=%s""",
                (health, identity, Jsonb(capabilities or {}), health, account["id"], account["lease_token"]))
        return health

    def cooldown(self, account, seconds):
        with self.store._connection() as conn:
            conn.execute("UPDATE sm_accounts SET cooldown_until=greatest(cooldown_until,now()+%s*interval '1 second') WHERE id=%s AND lease_token=%s",
                         (seconds, account["id"], account["lease_token"]))

    def secrets(self, account):
        from scweet_mcp.credentials import read_credentials
        values = read_credentials(self.store.config.mcp_credentials_file)
        token = values.get(account["credential_ref"], os.environ.get(account["credential_ref"]))
        proxy = values.get(account["proxy_ref"], os.environ.get(account["proxy_ref"])) if account["proxy_ref"] else None
        if not token or (account["proxy_ref"] and not proxy):
            raise StoreError("MISSING_CREDENTIAL: Set the account and proxy environment references in the worker process.")
        return token, proxy
