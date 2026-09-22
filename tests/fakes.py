"""In-process fakes: a clawproxy server (WebSocket + pull/ack) and a Hermes webhook receiver."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer


class FakeClawproxy:
    def __init__(self) -> None:
        self.auth_tokens: List[str] = []
        self.acks: List[List[str]] = []
        self.http_acks: List[List[str]] = []
        self.pull_requests: List[Dict[str, Any]] = []
        self.events_after_auth: List[Dict[str, Any]] = []
        self.pull_events: List[Dict[str, Any]] = []
        self.reject_auth = False
        self.ws_available = True
        self.close_after_auth = False
        self.connections = 0
        self.server: TestServer | None = None

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if not self.ws_available:
            return web.Response(status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "auth":
                self.auth_tokens.append(data["token"])
                if self.reject_auth:
                    await ws.send_json({"type": "auth_error", "error": "Invalid or inactive node token"})
                    await ws.close(code=4001)
                    break
                await ws.send_json({"type": "auth_ok", "nodeId": "node-1"})
                if self.close_after_auth:
                    await ws.close(code=1001)
                    break
                for event in self.events_after_auth:
                    await ws.send_json({"type": "event", **event})
            elif data.get("type") == "ack":
                self.acks.append(data["eventIds"])
                await ws.send_json({"type": "ack_ok", "acked": len(data["eventIds"]), "eventIds": data["eventIds"]})
        return ws

    async def _pull(self, request: web.Request) -> web.Response:
        self.pull_requests.append({
            "method": request.method,
            "auth": request.headers.get("Authorization"),
            "body": await request.json(),
        })
        events, self.pull_events = self.pull_events, []
        return web.json_response({"ok": True, "nodeId": "node-1", "events": events})

    async def _ack(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.http_acks.append(body["eventIds"])
        return web.json_response({"ok": True})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/api/nodes/ws", self._ws)
        app.router.add_post("/api/nodes/pull", self._pull)
        app.router.add_post("/api/nodes/ack", self._ack)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


class FakeHermesWebhook:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status = 202
        self.server: TestServer | None = None

    async def _hook(self, request: web.Request) -> web.Response:
        self.requests.append({
            "route": request.match_info["route_name"],
            "headers": dict(request.headers),
            "body": await request.read(),
        })
        return web.json_response({"status": "accepted"}, status=self.status)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/webhooks/{route_name}", self._hook)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)
