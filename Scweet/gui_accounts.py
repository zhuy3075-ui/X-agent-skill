"""Desktop token storage and conservative login checks, without secret logs."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import select

from .auth import normalize_account_record
from .config import ScweetConfig
from .repos import AccountsRepo
from .schema import AccountTable
from .storage import session_scope


@dataclass(frozen=True)
class TokenCheck:
    status: str
    detail: str
    cookies: dict[str, str] = field(default_factory=dict, repr=False)


def probe_token(
    token: str, proxy: Any, config: ScweetConfig, *, session_factory: Callable | None = None,
) -> TokenCheck:
    """Check a fresh home-page session. An HTTP error is not proof of rejection."""
    from .account_session import fill_proxy_session_placeholder
    from .http_utils import (
        X_PAGE_FULL, X_PAGE_LOGIN, apply_proxies_to_session, classify_x_page,
        normalize_http_proxies,
    )

    if session_factory is None:
        from curl_cffi.requests import Session
        session_factory = Session
    session = None
    try:
        session = session_factory(impersonate=config.api_http_impersonate)
        session.cookies.set('auth_token', token, domain='.x.com')
        proxies = normalize_http_proxies(fill_proxy_session_placeholder(proxy, {'username': 'guicheck'}))
        if proxies and not apply_proxies_to_session(session, proxies):
            return TokenCheck('暂时无法确认', '无法使用指定代理，请检查代理设置。')
        response = session.get('https://x.com/home', timeout=config.gui_token_check_timeout_s,
                               allow_redirects=True)
        status = int(response.status_code)
        if status != 200:
            reason = {429: '请求受限，请稍后重试。', 407: '代理认证失败，请检查代理。'}.get(
                status, '服务未正常响应，请检查网络或稍后重试。')
            return TokenCheck('暂时无法确认', f'响应状态 {status}：{reason}')
        final_url = urlparse(str(response.url))
        if final_url.hostname not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
            return TokenCheck('暂时无法确认', '跳转到了非预期站点，请检查网络或代理。')
        if final_url.path.startswith('/account/access'):
            return TokenCheck('需要解锁', '请在浏览器登录账号并完成安全验证。')
        page = classify_x_page(response.text, response.url)
        if page == X_PAGE_LOGIN:
            return TokenCheck('登录失效', '登录已被拒绝，请重新登录并添加新令牌。')
        cookies = session.cookies.get_dict()
        if page == X_PAGE_FULL and cookies.get('ct0'):
            cookies['auth_token'] = token
            return TokenCheck('有效', '登录页面检查通过；具体采集接口仍可能限流。', cookies)
        return TokenCheck('暂时无法确认', '页面不完整或缺少会话信息，请稍后重试。')
    except Exception:
        # Transport errors may contain request cookies or proxy passwords.
        return TokenCheck('暂时无法确认', '连接失败，请检查网络和代理后重试。')
    finally:
        if session is not None:
            session.close()


class GuiAccounts:
    """Keep tokens in the existing account database; expose only safe summaries."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        config = ScweetConfig()
        self.repo = AccountsRepo(db_path, require_auth_material=True,
                                 daily_pages_limit=config.daily_requests_limit,
                                 daily_tweets_limit=config.daily_tweets_limit)
        self._checks: dict[int, dict[str, str]] = {}

    def add_tokens(self, text: str) -> int:
        tokens = list(dict.fromkeys(part for part in re.split(r'[\s,，;；]+', text.strip()) if part))
        if not tokens:
            raise ValueError('请先输入一个或多个登录令牌。')
        added = 0
        for token in tokens:
            if self.repo.get_by_auth_token(token):
                continue
            record = normalize_account_record({'auth_token': token, 'status': 0})
            record['cooldown_reason'] = 'unusable:gui_pending'
            self.repo.upsert_account(record)
            added += 1
        return added

    def list_accounts(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.execute(select(AccountTable.id).where(
                AccountTable.auth_token.isnot(None), AccountTable.auth_token != '',
            ).order_by(AccountTable.id)).all()
        return [dict(id=row.id, label=f'账号 {row.id}',
                     **self._checks.get(row.id, {'status': '待检测', 'checked_at': '', 'detail': '尚未检测'}))
                for row in rows]

    def has_auth_material(self) -> bool:
        """Let the runner handle cooldown waits; only reject unprepared accounts here."""
        with session_scope(self.db_path) as session:
            return session.execute(select(AccountTable.id).where(
                AccountTable.auth_token.isnot(None), AccountTable.auth_token != '',
                AccountTable.csrf.isnot(None), AccountTable.csrf != '',
            ).limit(1)).first() is not None

    def check_all(
        self, proxy: Any, config: ScweetConfig, *, probe: Callable = probe_token,
        stop: threading.Event | None = None, on_result: Callable | None = None,
    ) -> list[dict]:
        with session_scope(self.db_path) as session:
            records = [(row.id, row.auth_token, row.proxy_json) for row in session.execute(
                select(AccountTable).where(AccountTable.auth_token.isnot(None),
                                           AccountTable.auth_token != '').order_by(AccountTable.id)
            ).scalars()]
        for account_id, token, stored_proxy in records:
            if stop is not None and stop.is_set():
                break
            # A separate process can be using the same database.
            with session_scope(self.db_path) as session:
                row = session.get(AccountTable, account_id)
                leased = row is None or bool(row.lease_id and (row.lease_expires_at or 0) > time.time())
            if leased:
                result = TokenCheck('暂时无法确认', '账号正在使用，检测已顺延。')
            else:
                account_proxy = proxy
                if stored_proxy:
                    try:
                        account_proxy = json.loads(stored_proxy)
                    except (ValueError, TypeError):
                        account_proxy = stored_proxy
                try:
                    result = probe(token, account_proxy, config)
                    if result.status == '有效' and result.cookies.get('ct0'):
                        self._save_cookies(account_id, token, result.cookies)
                except Exception:
                    result = TokenCheck('暂时无法确认', '检测或状态保存失败，请检查连接和状态库后重试。')
            self._checks[account_id] = dict(status=result.status, detail=result.detail,
                                           checked_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            if on_result is not None:
                on_result(self.list_accounts())
        return self.list_accounts()

    def _save_cookies(self, account_id: int, token: str, cookies: dict) -> None:
        with session_scope(self.db_path) as session:
            row = session.get(AccountTable, account_id)
            if row is None or row.auth_token != token:
                return
            if row.lease_id and (row.lease_expires_at or 0) > time.time():
                return
            row.csrf = cookies['ct0']
            row.cookies_json = json.dumps(cookies)
            if row.cooldown_reason == 'unusable:gui_pending':
                row.status = 1
                row.cooldown_reason = None
