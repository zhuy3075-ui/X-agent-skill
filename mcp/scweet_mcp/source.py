"""Isolate the private X adapter. Fixture mode never makes network requests."""
import asyncio
import json
import re
import time
import logging
from pathlib import Path
from urllib.parse import unquote

from Scweet.api_engine import ApiEngine
from Scweet.account_session import AccountSessionBuilder, fill_proxy_session_placeholder
from Scweet.gui_accounts import probe_token
from Scweet.manifest import ManifestProvider
from Scweet.transaction import TransactionIdProvider
from scweet_mcp.accounts import Accounts
from scweet_mcp.store import StoreError
from scweet_mcp.jobs import identifier
from scweet_mcp.source_errors import SourceFailure, AccountWait

logger = logging.getLogger("scweet_mcp.source")


def login_error(health):
    causes = {
        "invalid": "TOKEN_INVALID: X rejected the login. Replace the token and check the account again.",
        "locked": "ACCOUNT_LOCKED: X requires an account unlock. Complete the browser security check first.",
        "duplicate": "DUPLICATE_ACCOUNT: This credential identifies an existing account. Use that account reference.",
    }
    return StoreError(causes.get(health, "LOGIN_UNCONFIRMED: Login or identity could not be confirmed. Check the network, proxy and session; retry the check."))


def normalize(record):
    item = record.model_dump(mode="json")
    raw = item.get("raw") or {}
    raw = raw.get("tweet", raw)
    legacy = raw.get("legacy") or {}
    user = (raw.get("core") or {}).get("user_results", {}).get("result", {})
    item["author_id"] = user.get("rest_id") or legacy.get("user_id_str")
    item["conversation_id"] = legacy.get("conversation_id_str")
    metrics = {}
    for name, key in (("likes", "favorite_count"), ("comments", "reply_count"), ("reposts", "retweet_count"), ("quotes", "quote_count")):
        value = legacy.get(key)
        metrics[name] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    value = (raw.get("views") or {}).get("count")
    metrics["views"] = int(value) if str(value).isdigit() and int(value) <= 9223372036854775807 else None
    item["_metrics"] = metrics
    return item


def continuations(payload):
    cursors, unknown = [], False
    def visit(node):
        nonlocal unknown
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if "cursorType" in node:
                kind, value = str(node["cursorType"]).lower(), node.get("value")
                if kind in {"bottom", "showmorethreads"} and isinstance(value, str) and value:
                    if value not in cursors:
                        cursors.append(value)
                elif kind != "top":
                    unknown = True
            for value in node.values():
                visit(value)
    visit(payload)
    return cursors, unknown


class BudgetEngine(ApiEngine):
    async def _graphql_get(self, **kwargs):
        self.guard_error = None
        result = await super()._graphql_get(**kwargs)
        if self.guard_error is not None:
            raise self.guard_error
        return result

    async def _session_get(self, session, url, **kwargs):
        # This hook runs for every wire attempt, including the engine's 404 retry.
        try:
            self.request_id = self.accounts.reserve(self.account)
        except StoreError as exc:
            self.guard_error = exc
            raise
        return await super()._session_get(session, url, **kwargs)

    async def _build_transaction_id(self, **kwargs):
        value = await super()._build_transaction_id(**kwargs)
        if not value:
            self.guard_error = StoreError("SIGNATURE_UNAVAILABLE: Rebuild the X request signature before retrying.")
            raise self.guard_error
        return value


class Source:
    def __init__(self, store, fixtures=None):
        self.store, self.accounts = store, Accounts(store)
        self.fixtures = Path(fixtures).resolve() if fixtures else None
        self.name = "fixture" if self.fixtures else "platform"

    def check(self, account, level="login", capabilities=None):
        if self.fixtures:
            raise StoreError("FIXTURE_MODE: Account validity requires a real login check. No account was marked valid.")
        token, proxy = self.accounts.secrets(account)
        result = probe_token(token, proxy, self.store.config)
        health = {"有效": "valid", "登录失效": "invalid", "需要解锁": "locked"}.get(result.status, "unknown")
        match = re.fullmatch(r'u=(\d+)', unquote(result.cookies.get("twid", "")).strip('"'))
        identity = match.group(1) if match else None
        if health == "valid" and identity is None:
            health = "unknown"
        caps = {name: "unknown" for name in (capabilities or ["timeline", "lookup", "comments"])}
        if health == "unknown" and account["health"] == "valid":
            self.accounts.cooldown(account, self.store.config.mcp_unknown_cooldown_s)
        else:
            health = self.accounts.health(account, health, identity, caps)
        if health == "valid" and level == "capability":
            # Probe the account's timeline; lookup/comments need an accessible post first.
            try:
                page = self._live("timeline", identity, None, account, result.cookies, proxy)
                caps["timeline"] = "available"
                post_id = next((r["tweet_id"] for r in page["records"]), None)
                for cap, operation in (("lookup", "metrics"), ("comments", "comments")):
                    if cap in caps and post_id:
                        self._live(operation, [post_id] if cap == "lookup" else post_id, None, account, result.cookies, proxy)
                        caps[cap] = "available"
            except StoreError:
                pass  # A failed endpoint does not invalidate a successful login.
            self.accounts.health(account, health, identity, caps)
        return {"account_id": str(account["id"]), "health": health, "capabilities": caps}

    def preflight(self, account_id):
        if self.fixtures:
            return {"health": "fixture", "live_login_checked": False}
        with self.store._connection() as conn:
            row = conn.execute("SELECT enabled,health FROM sm_accounts WHERE id=%s", (identifier(account_id),)).fetchone()
        if not row:
            raise StoreError("NOT_FOUND: account_id does not exist. Register a credential reference first.")
        if not row["enabled"]:
            raise StoreError("ACCOUNT_DISABLED: Enable this account before submitting collection.")
        if row["health"] == "valid":
            return {"health": "valid", "live_login_checked": False}  # page() checks the actual chosen token.
        account = self.accounts.lease(identifier(account_id), check=True)
        if not account:
            raise StoreError("ACCOUNT_BUSY: The selected account is leased. Wait for its current operation.")
        try:
            result = self.check(account)
            if result["health"] != "valid":
                if self.store.config.mcp_account_rotation:
                    alternative = self.accounts.lease(exclude=[account["id"]])
                    if alternative:
                        self.accounts.release(alternative)
                        return {"health": "pool_available", "live_login_checked": False}
                    waiting = self.accounts.unavailable()
                    if isinstance(waiting, AccountWait):
                        waiting.details["last_account_error"] = str(login_error(result["health"])).split(":", 1)[0]
                        raise waiting
                raise login_error(result["health"])
            return result
        finally:
            self.accounts.release(account)

    def resolve(self, target, account_id):
        if target.isdigit():
            return target
        if self.fixtures:
            raise StoreError("FIXTURE_MODE: Use a numeric author ID for fixture replay.")
        return self.page("resolve", target.lstrip("@"), account_id=account_id)["author_id"]

    def page(self, operation, target, cursor=None, account_id=None):
        if self.fixtures:
            names = {"timeline": "profile_with_replies_page.json", "metrics": "tweet_lookup_batch.json", "comments": "tweet_detail_page.json"}
            if cursor:
                # One captured page cannot establish that no other pages exist.
                return {"records": [], "cursors": [], "unknown": True, "stop_reason": "fixture_end"}
            payload = json.loads((self.fixtures / names[operation]).read_text(encoding="utf-8"))
            engine = ApiEngine(self.store.config, None, None)
            return self.parse(engine, operation, target, payload)
        chosen = identifier(account_id) if account_id else None
        tried, last_error = [], None
        cfg = self.store.config
        for _ in range(cfg.mcp_account_rotation_attempts if cfg.mcp_account_rotation else 1):
            account = self.accounts.lease(chosen, exclude=tried)
            if not account and chosen and cfg.mcp_account_rotation:
                account = self.accounts.lease(exclude=tried)
            if not account:
                break
            tried.append(account["id"])
            try:
                result = self._account_page(operation, target, cursor, account)
                result["account_id"] = str(account["id"])
                return result
            except StoreError as exc:
                code = str(exc).split(":", 1)[0]
                if code not in {"TOKEN_INVALID", "ACCOUNT_LOCKED", "SOURCE_AUTH_REJECTED", "RATE_LIMITED",
                                "LOGIN_UNCONFIRMED", "MISSING_CREDENTIAL", "ACCOUNT_CHANGED", "SOURCE_UNAVAILABLE"}:
                    raise
                last_error = exc
                logger.warning("Account %s could not serve %s (%s); checking pool", account["id"], operation, code)
            finally:
                self.accounts.release(account)
        unavailable = self.accounts.unavailable(None if cfg.mcp_account_rotation else chosen)
        if last_error is not None and not isinstance(last_error, AccountWait):
            code = str(last_error).split(":", 1)[0]
            if isinstance(unavailable, AccountWait) and code in {"TOKEN_INVALID", "ACCOUNT_LOCKED", "SOURCE_AUTH_REJECTED", "ACCOUNT_CHANGED", "MISSING_CREDENTIAL"}:
                unavailable.details["last_account_error"] = code
            else:
                raise last_error
        raise unavailable

    def _account_page(self, operation, target, cursor, account):
        try:
            token, proxy = self.accounts.secrets(account)
            check = probe_token(token, proxy, self.store.config)
            if check.status != "有效":
                health = {"登录失效": "invalid", "需要解锁": "locked"}.get(check.status, "unknown")
                if health != "unknown":
                    self.accounts.health(account, health)
                self.accounts.cooldown(account, self.store.config.mcp_unknown_cooldown_s)
                raise login_error(health)
            identity = re.fullmatch(r'u=(\d+)', unquote(check.cookies.get("twid", "")).strip('"'))
            if not identity or identity.group(1) != account["platform_user_id"]:
                self.accounts.health(account, "unknown")
                raise StoreError("ACCOUNT_CHANGED: Recheck the account reference after changing its credential.")
            return self._live(operation, target, cursor, account, check.cookies, proxy)
        except StoreError:
            raise

    def response_error(self, status, payload, headers, account, operation):
        """Separate request/target errors from account failures; keep only numeric diagnostics."""
        cfg = self.store.config
        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        codes = [e["code"] for e in errors if isinstance(e, dict) and isinstance(e.get("code"), int)][:10]
        details = {"http_status": status, "upstream_codes": codes, "operation": operation, "stage": "request"}
        if status == 200 and payload and isinstance(payload, dict) and not errors:
            return
        if status == 429:
            seconds = cfg.rate_limit_window_s
            try:
                reset = float({str(k).lower(): v for k,v in headers.items()}.get("x-rate-limit-reset", 0))
                if reset > time.time():
                    seconds = max(seconds, int(reset-time.time())+1)
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
            self.accounts.cooldown(account, seconds)
            raise SourceFailure("RATE_LIMITED: X request quota is exhausted. Resume after its reset time.", details=details, retry_after=seconds)
        if status in {401, 423}:
            self.accounts.health(account, "invalid" if status == 401 else "locked")
            raise SourceFailure("SOURCE_AUTH_REJECTED: X rejected this session. Check or replace its token." if status == 401
                                else "ACCOUNT_LOCKED: Complete the account security check in a browser.", details=details)
        if status in {403, 404}:
            raise SourceFailure("SOURCE_FORBIDDEN: X refused this target or request. Check visibility." if status == 403
                                else "SOURCE_NOT_FOUND: Check the target and request manifest.", details=details)
        if status == 200 and errors and isinstance(payload.get("data"), dict):
            # Some targets in a conversation can be inaccessible while other comments are valid.
            return
        if status == 200:
            raise SourceFailure("SOURCE_RESPONSE_ERROR: X returned no usable data. Check the target and response diagnostics.", details=details)
        self.accounts.cooldown(account, cfg.mcp_unknown_cooldown_s)
        raise SourceFailure("SOURCE_UNAVAILABLE: X could not serve the request. Retry with backoff.", details=details,
                            retry_after=cfg.mcp_unknown_cooldown_s)

    def _live(self, operation, target, cursor, account, cookies, proxy):
        stage = "manifest"
        async def run():
            nonlocal stage
            cfg = self.store.config
            proxy_value = fill_proxy_session_placeholder(proxy, {"username": str(account["id"])})
            Path(cfg.mcp_manifest_db_path).parent.mkdir(parents=True, exist_ok=True)
            provider = ManifestProvider(cfg.mcp_manifest_db_path, cfg.manifest_url, cfg.manifest_ttl_s)
            if cfg.manifest_scrape_on_init:
                manifest = await asyncio.to_thread(provider.scrape_from_x_sync, strict=True, cookies=cookies, proxy=proxy_value)
            elif cfg.manifest_update_on_init:
                manifest = await asyncio.to_thread(provider.refresh_sync, strict=True)
            else:
                manifest = await provider.get_manifest()
            stage = "session"
            builder = AccountSessionBuilder(api_http_mode="sync", proxy=proxy_value, impersonate=cfg.api_http_impersonate)
            session, _ = builder.build({"auth_token": cookies["auth_token"], "cookies": cookies})
            engine = BudgetEngine(cfg, None, provider, transaction_id_provider=TransactionIdProvider(
                cookies=cookies, proxy=proxy_value, impersonate=cfg.api_http_impersonate,
                init_attempts=cfg.transaction_init_attempts, init_backoff_s=cfg.transaction_init_backoff_s))
            engine.account, engine.accounts = account, self.accounts
            try:
                if operation == "timeline":
                    op = "profile_timeline_with_replies"
                    params = engine._build_profile_timeline_params(user_id=target, cursor=cursor, manifest=manifest, operation=op)
                elif operation == "resolve":
                    op, params = "user_lookup_screen_name", engine._build_user_lookup_params(target, manifest)
                elif operation == "metrics":
                    op, params = "tweet_lookup", engine._build_tweet_lookup_params(target, manifest)
                else:
                    op, params = "tweet_detail", engine._build_tweet_detail_params(target, cursor, manifest)
                stage = "request"
                payload, status, headers, _ = await engine._graphql_get(
                    url=engine._resolve_user_lookup_url(manifest) if operation == "resolve" else engine._resolve_operation_url(manifest, op), params=params,
                    timeout_s=cfg.gui_token_check_timeout_s, session=session, account_context={"id": str(account["id"])})
                self.response_error(status, payload, headers, account, operation)
                stage = "response_parse"
                if operation == "resolve":
                    user = engine._extract_user_result(payload)
                    author_id = (user or {}).get("rest_id")
                    if not isinstance(author_id, str) or not author_id.isdigit():
                        raise StoreError("TARGET_UNAVAILABLE: No accessible numeric author ID was returned. Check the handle and profile visibility.")
                    self.accounts.observed(engine.request_id, 0)
                    return {"author_id": author_id}
                page = self.parse(engine, operation, target, payload)
                if payload.get("errors"):
                    if not page["records"]:
                        raise SourceFailure("SOURCE_RESPONSE_ERROR: X returned errors without usable records. Inspect the safe diagnostics before retrying.",
                                            details={"http_status": status, "operation": operation, "stage": "response_parse"})
                    page.update(unknown=True, stop_reason="partial_response")
                self.accounts.observed(engine.request_id, len(page["records"]))
                return page
            finally:
                await builder.close(session)
        try:
            return asyncio.run(run())
        except StoreError:
            raise
        except Exception as exc:
            raise SourceFailure(f"SOURCE_UNAVAILABLE: The {stage} stage failed. Check the X connection, request manifest and worker configuration.",
                                details={"stage": stage, "exception_type": type(exc).__name__, "operation": operation}) from None

    @staticmethod
    def parse(engine, operation, target, payload):
        if operation == "timeline":
            records, _ = engine._extract_profile_tweets_and_cursor(payload)
            recognized = bool(engine._extract_profile_timeline_instructions(payload))
        elif operation == "metrics":
            records = [engine._tweet_result_to_record(raw) for raw in engine._extract_tweet_lookup_results(payload)]
            # The captured lookup contract is data.tweetResult[]. An empty valid list
            # is a visibility observation; an unknown response shape is not.
            rows = (payload.get("data") or {}).get("tweetResult")
            recognized = isinstance(rows, list)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or "result" not in row:
                    recognized = False
                    break
                result = row["result"]
                if result is None:
                    continue
                if not isinstance(result, dict) or result.get("__typename") not in {"Tweet", "TweetWithVisibilityResults"}:
                    recognized = False
                    break
                tweet = result.get("tweet") if result["__typename"] == "TweetWithVisibilityResults" else result
                if not isinstance(tweet, dict) or not str(tweet.get("rest_id", "")).isdigit():
                    recognized = False
                    break
        else:
            # Retain the focal record long enough to identify its author; callers filter replies.
            records, _ = engine._extract_conversation_tweets_and_cursor(payload, focal_tweet_id="")
            recognized = isinstance(payload.get("data", {}).get("threaded_conversation_with_injections_v2", {}).get("instructions"), list)
        cursors, unknown = continuations(payload)
        normalized = [normalize(record) for record in records]
        root_author = next((r.get("author_id") for r in normalized if r["tweet_id"] == str(target)), None) if operation == "comments" else None
        return {"records": normalized, "cursors": cursors, "root_author_id": root_author,
                "unknown": unknown or not recognized, "stop_reason": None}
