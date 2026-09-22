"""Pure helpers for turning a clawproxy event into a signed request to a Hermes webhook route.

Kept free of Hermes imports so the signing and header rules can be tested on their own.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Dict, Mapping, Optional

# Hermes checks these provider signature schemes *before* its generic HMAC V2 check, so any that
# survive the relay make Hermes validate the provider's signature against the route secret and
# reject the request. They are meaningless after the hop anyway: clawproxy verified nothing, and
# the bridge re-signs with the route secret.
_SIGNATURE_HEADERS = {
    "x-hub-signature",
    "x-hub-signature-256",
    "x-gitlab-token",
    "linear-signature",
    "svix-id",
    "svix-timestamp",
    "svix-signature",
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
    "x-webhook-signature",
    "x-webhook-signature-v2",
    "x-webhook-timestamp",
}

# Per-connection headers that must not be replayed onto a new request.
_HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "proxy-authorization",
    "proxy-authenticate",
}


def sign_v2(body: bytes, secret: str, timestamp: str) -> str:
    """Hermes generic HMAC V2: hex HMAC-SHA256 of ``"<timestamp>.<body>"``."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def prepare_headers(
    original: Mapping[str, str],
    *,
    event_id: str,
    body: bytes,
    secret: str,
    now: Optional[float] = None,
) -> Dict[str, str]:
    """Headers for forwarding one clawproxy event to a Hermes webhook route.

    Keeps the provider's headers (event type, delivery id, content type) so route filters and
    prompt templates see what the provider sent; drops signature and hop-by-hop headers; signs
    with Hermes's replay-safe generic V2 scheme; and sets ``X-Request-ID`` to the clawproxy event
    id so Hermes's idempotency check drops a redelivered event.
    """
    headers: Dict[str, str] = {}
    for name, value in original.items():
        lowered = name.lower()
        if lowered in _SIGNATURE_HEADERS or lowered in _HOP_BY_HOP_HEADERS:
            continue
        headers[name] = value

    timestamp = str(int(now if now is not None else time.time()))
    headers["X-Webhook-Timestamp"] = timestamp
    headers["X-Webhook-Signature-V2"] = sign_v2(body, secret, timestamp)
    if not any(k.lower() == "x-request-id" for k in headers):
        headers["X-Request-ID"] = event_id
    return headers
