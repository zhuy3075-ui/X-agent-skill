"""Durable signed webhook delivery to destinations configured by the operator."""
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from scweet_mcp.jobs import wire


def deliver_one(store):
    lease = uuid4()
    with store._connection() as conn:
        row = conn.execute("""UPDATE sm_deliveries SET status='sending',lease_token=%s,
            lease_until=now()+%s*interval '1 second',attempts=attempts+1 WHERE (event_id,destination_ref)=(
            SELECT event_id,destination_ref FROM sm_deliveries WHERE
            (status='pending' AND next_attempt_at<=now()) OR (status='sending' AND lease_until<now())
            ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""",
            (lease, store.config.mcp_notification_timeout_s + 30)).fetchone()
        if not row:
            return False
        event = conn.execute("SELECT id AS event_id,type,monitor_id,post_id,observed_at,payload FROM sm_events WHERE id=%s", (row["event_id"],)).fetchone()
    success, error = False, None
    try:
        destination = store.config.mcp_notification_destinations[row["destination_ref"]]
        url, secret = destination["url"], os.environ[destination["secret_ref"]]
        parsed = urlparse(url)
        if parsed.username or parsed.password or not parsed.hostname or not secret:
            raise ValueError()
        if not store.config.mcp_allow_local_webhooks:
            if parsed.scheme != "https":
                raise ValueError()
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError()
        elif parsed.scheme not in {"http", "https"}:
            raise ValueError()
        body = json.dumps(wire(event), sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(), timestamp.encode()+b"."+body, hashlib.sha256).hexdigest()
        response = httpx.post(url, content=body, headers={"Content-Type": "application/json", "X-Scweet-Timestamp": timestamp,
            "X-Scweet-Signature": "sha256="+signature, "Idempotency-Key": str(row["event_id"])},
            timeout=store.config.mcp_notification_timeout_s, follow_redirects=False, trust_env=False)
        success = 200 <= response.status_code < 300
        error = None if success else "HTTP_REJECTED"
    except Exception:
        error = "DELIVERY_UNAVAILABLE"
    status = "delivered" if success else "dead_letter" if row["attempts"] >= store.config.mcp_notification_max_attempts else "pending"
    with store._connection() as conn:
        conn.execute("""UPDATE sm_deliveries SET status=%s,last_error=%s,lease_token=NULL,lease_until=NULL,
            next_attempt_at=now()+%s*interval '1 second' WHERE event_id=%s AND destination_ref=%s AND lease_token=%s""",
            (status, error, min(3600, store.config.mcp_retry_delay_s*2**min(row["attempts"], 10)), row["event_id"], row["destination_ref"], lease))
    return True
