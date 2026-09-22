"""clawproxy bridge for Hermes Agent.

Holds a clawproxy node's WebSocket inside the Hermes gateway and hands each webhook event to
Hermes's own webhook platform at ``/webhooks/<routeSlug>``, so every existing route feature
(prompt templates, deliver, skills, toolsets) applies unchanged. clawproxy route slugs must match
Hermes webhook route names. Inbound only: replies go wherever the webhook route delivers them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .bridge import prepare_headers

try:  # Profile-aware secret lookup used by the bundled plugins; fall back to plain env vars.
    from gateway.platforms._shared import get_scoped_secret as _get_secret
except Exception:  # pragma: no cover - depends on the Hermes version
    def _get_secret(name: str, default: str = "") -> str:
        return os.environ.get(name, default)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

PLATFORM_NAME = "clawproxy"
DEFAULT_SERVER = "https://clawproxy.io"
DEFAULT_HERMES_WEBHOOK_URL = "http://127.0.0.1:8644"
DEFAULT_RECONNECT_BACKOFF = [1, 2, 5, 10, 30, 60]
DEFAULT_MAX_EVENTS = 10
FORWARD_TIMEOUT_SECONDS = 30

# extra key -> env var. config.yaml ``platforms.clawproxy.extra`` wins over the env var.
_SETTINGS = {
    "token": "CLAWPROXY_NODE_TOKEN",
    "hermes_webhook_secret": "CLAWPROXY_HERMES_WEBHOOK_SECRET",
    "server": "CLAWPROXY_SERVER_URL",
    "hermes_webhook_url": "CLAWPROXY_HERMES_WEBHOOK_URL",
}


class _FatalError(Exception):
    """Unrecoverable (bad token): stop the reconnect loop."""


def _setting(extra: Dict[str, Any], key: str, default: str = "") -> str:
    return str(extra.get(key) or _get_secret(_SETTINGS[key], default) or default).strip()


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(_get_secret("CLAWPROXY_NODE_TOKEN", "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(_setting(extra, "token") and _setting(extra, "hermes_webhook_secret"))


def is_connected(config) -> bool:
    return validate_config(config)


class ClawproxyAdapter(BasePlatformAdapter):
    """Inbound-only bridge: clawproxy node connection -> Hermes webhook routes."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._token = _setting(extra, "token")
        self._secret = _setting(extra, "hermes_webhook_secret")
        server = _setting(extra, "server", DEFAULT_SERVER).rstrip("/")
        self._ws_url = server.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/nodes/ws"
        self._pull_url = server + "/api/nodes/pull"
        self._ack_url = server + "/api/nodes/ack"
        self._hermes_url = _setting(extra, "hermes_webhook_url", DEFAULT_HERMES_WEBHOOK_URL).rstrip("/")
        self._backoff: List[float] = list(extra.get("reconnect_backoff") or DEFAULT_RECONNECT_BACKOFF)
        self._max_events = int(extra.get("max_events") or DEFAULT_MAX_EVENTS)
        self._session: Optional["aiohttp.ClientSession"] = None
        self._task: Optional[asyncio.Task] = None

    # -- Lifecycle --------------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp is not installed", self.name)
            return False
        if not (self._token and self._secret):
            logger.warning("[%s] CLAWPROXY_NODE_TOKEN and CLAWPROXY_HERMES_WEBHOOK_SECRET are required", self.name)
            return False
        self._session = aiohttp.ClientSession()
        self._mark_connected()
        self._task = asyncio.create_task(self._run())
        logger.info("[%s] Bridging %s -> %s/webhooks/<route>", self.name, self._ws_url, self._hermes_url)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("[%s] Disconnected", self.name)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        return SendResult(success=False, error="clawproxy is inbound only; replies go to the webhook route's deliver target")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {}

    # -- Connection loop --------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                if await self._run_websocket():
                    attempt = 0  # authenticated at least once: start backoff over
            except asyncio.CancelledError:
                return
            except _FatalError as exc:
                self._set_fatal_error("clawproxy_unauthorized", str(exc), retryable=False)
                return
            except Exception as exc:
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
            if not self._running:
                return
            try:
                await self._pull_once()  # deliver anything queued while the socket is down
            except asyncio.CancelledError:
                return
            except _FatalError as exc:
                self._set_fatal_error("clawproxy_unauthorized", str(exc), retryable=False)
                return
            except Exception as exc:
                logger.warning("[%s] HTTP pull failed: %s", self.name, exc)
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _run_websocket(self) -> bool:
        """Run one WebSocket session. Returns True if it authenticated."""
        authenticated = False
        async with self._session.ws_connect(self._ws_url, autoping=True) as ws:
            await ws.send_json({"type": "auth", "token": self._token})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                kind = data.get("type")
                if kind == "auth_ok":
                    authenticated = True
                    logger.info("[%s] Authenticated as node %s", self.name, data.get("nodeId"))
                elif kind == "auth_error":
                    raise _FatalError(f"clawproxy rejected the node token: {data.get('error')}")
                elif kind == "event":
                    if await self._forward(data):
                        await ws.send_json({"type": "ack", "eventIds": [data["id"]]})
        return authenticated

    async def _pull_once(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.post(self._pull_url, json={"maxEvents": self._max_events}, headers=headers) as resp:
            if resp.status == 401:
                raise _FatalError("clawproxy rejected the node token on pull")
            if resp.status >= 300:
                logger.warning("[%s] Pull returned HTTP %s", self.name, resp.status)
                return
            payload = await resp.json()
        acked = [event["id"] for event in payload.get("events") or [] if await self._forward(event)]
        if acked:
            async with self._session.post(self._ack_url, json={"eventIds": acked}, headers=headers) as resp:
                if resp.status >= 300:
                    logger.warning("[%s] Ack returned HTTP %s", self.name, resp.status)

    # -- Forwarding -------------------------------------------------------------------------

    async def _forward(self, event: Dict[str, Any]) -> bool:
        """POST one event to its Hermes webhook route. True only when Hermes accepted it."""
        route = event.get("routeSlug") or ""
        body = event.get("body")
        body_bytes = (body if isinstance(body, str) else json.dumps(body)).encode()
        headers = prepare_headers(event.get("headers") or {}, event_id=str(event.get("id", "")),
                                  body=body_bytes, secret=self._secret)
        url = f"{self._hermes_url}/webhooks/{route}"
        try:
            timeout = aiohttp.ClientTimeout(total=FORWARD_TIMEOUT_SECONDS)
            async with self._session.post(url, data=body_bytes, headers=headers, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    logger.info("[%s] Delivered event %s to route %s (HTTP %s)", self.name, event.get("id"), route, resp.status)
                    return True
                logger.warning("[%s] Hermes route %s rejected event %s: HTTP %s %s", self.name, route,
                               event.get("id"), resp.status, (await resp.text())[:200])
        except Exception as exc:
            logger.warning("[%s] Forward to route %s failed: %s", self.name, route, exc)
        return False


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars so env-only setups turn the platform on."""
    token = _get_secret("CLAWPROXY_NODE_TOKEN", "").strip()
    if not token:
        return None
    seed: Dict[str, Any] = {"token": token}
    for key in ("hermes_webhook_secret", "server", "hermes_webhook_url"):
        value = _get_secret(_SETTINGS[key], "").strip()
        if value:
            seed[key] = value
    return seed


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="clawproxy",
        adapter_factory=lambda cfg: ClawproxyAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CLAWPROXY_NODE_TOKEN", "CLAWPROXY_HERMES_WEBHOOK_SECRET"],
        install_hint="aiohttp ships with Hermes; set CLAWPROXY_NODE_TOKEN and CLAWPROXY_HERMES_WEBHOOK_SECRET",
        env_enablement_fn=_env_enablement,
        emoji="🪝",
        pii_safe=True,
        platform_hint=(
            "Events from clawproxy arrive through your webhook routes. "
            "This platform is inbound only and cannot send replies."
        ),
    )
