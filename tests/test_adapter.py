"""Steps 2-5: the bridge adapter against fake clawproxy and Hermes servers."""
import json

import pytest

from gateway.config import PlatformConfig

from clawproxy_hermes.adapter import ClawproxyAdapter, _env_enablement, register
from clawproxy_hermes.bridge import sign_v2

from fakes import FakeClawproxy, FakeHermesWebhook, wait_for

SECRET = "a" * 64
TOKEN = "cpn_test_token"

EVENT = {
    "id": "evt-1",
    "routeId": "route-1",
    "routeSlug": "github-prs",
    "headers": {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=provider",
                "Content-Type": "application/json"},
    "body": '{"action":"opened"}',
    "contentType": "application/json",
    "receivedAt": "2026-09-22T00:00:00Z",
    "leaseExpiresAt": "2026-09-22T00:01:00Z",
    "attemptCount": 1,
}


@pytest.fixture
async def servers():
    claw, hermes = FakeClawproxy(), FakeHermesWebhook()
    claw_url, hermes_url = await claw.start(), await hermes.start()
    yield claw, claw_url, hermes, hermes_url
    await claw.stop()
    await hermes.stop()


def make_adapter(claw_url, hermes_url):
    return ClawproxyAdapter(PlatformConfig(enabled=True, extra={
        "token": TOKEN,
        "server": claw_url,
        "hermes_webhook_url": hermes_url,
        "hermes_webhook_secret": SECRET,
        "reconnect_backoff": [0.01],
        "max_events": 7,
    }))


async def test_authenticates_with_the_node_token(servers):
    claw, claw_url, hermes, hermes_url = servers
    adapter = make_adapter(claw_url, hermes_url)
    assert await adapter.connect()
    await wait_for(lambda: claw.auth_tokens)
    assert claw.auth_tokens == [TOKEN]
    await adapter.disconnect()


async def test_forwards_event_to_matching_hermes_route_signed_v2(servers):
    claw, claw_url, hermes, hermes_url = servers
    claw.events_after_auth = [EVENT]
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: hermes.requests)
    req = hermes.requests[0]
    assert req["route"] == "github-prs"
    assert req["body"] == EVENT["body"].encode()
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["x-github-event"] == "pull_request"
    assert "x-hub-signature-256" not in headers
    assert headers["x-request-id"] == "evt-1"
    expected = sign_v2(req["body"], SECRET, headers["x-webhook-timestamp"])
    assert headers["x-webhook-signature-v2"] == expected
    await adapter.disconnect()


async def test_acks_only_after_hermes_accepts(servers):
    claw, claw_url, hermes, hermes_url = servers
    claw.events_after_auth = [EVENT]
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: claw.acks)
    assert claw.acks == [["evt-1"]]
    await adapter.disconnect()


@pytest.mark.parametrize("status", [400, 401, 500, 503])
async def test_does_not_ack_when_hermes_rejects(servers, status):
    claw, claw_url, hermes, hermes_url = servers
    hermes.status = status
    claw.events_after_auth = [EVENT]
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: hermes.requests)
    await adapter.disconnect()
    assert claw.acks == []


async def test_rejected_token_is_fatal_and_stops_retrying(servers):
    claw, claw_url, hermes, hermes_url = servers
    claw.reject_auth = True
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: adapter.has_fatal_error)
    connections = claw.connections
    await __import__("asyncio").sleep(0.1)
    assert claw.connections == connections
    await adapter.disconnect()


async def test_reconnects_after_the_server_closes(servers):
    claw, claw_url, hermes, hermes_url = servers
    claw.close_after_auth = True
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: len(claw.auth_tokens) >= 2)
    await adapter.disconnect()


async def test_falls_back_to_http_pull_when_websocket_is_down(servers):
    claw, claw_url, hermes, hermes_url = servers
    claw.ws_available = False
    claw.pull_events = [EVENT]
    adapter = make_adapter(claw_url, hermes_url)
    await adapter.connect()
    await wait_for(lambda: claw.http_acks)
    assert claw.pull_requests[0]["method"] == "POST"
    assert claw.pull_requests[0]["auth"] == f"Bearer {TOKEN}"
    assert claw.pull_requests[0]["body"] == {"maxEvents": 7}
    assert hermes.requests[0]["route"] == "github-prs"
    assert claw.http_acks == [["evt-1"]]
    await adapter.disconnect()


async def test_send_reports_inbound_only():
    adapter = ClawproxyAdapter(PlatformConfig(enabled=True, extra={"token": TOKEN}))
    result = await adapter.send("chat", "hello")
    assert result.success is False
    assert "inbound" in (result.error or "")


def test_register_declares_the_platform():
    calls = {}

    class Ctx:
        def register_platform(self, **kwargs):
            calls.update(kwargs)

    register(Ctx())
    assert calls["name"] == "clawproxy"
    assert "CLAWPROXY_NODE_TOKEN" in calls["required_env"]
    assert "CLAWPROXY_HERMES_WEBHOOK_SECRET" in calls["required_env"]


def test_env_enablement(monkeypatch):
    monkeypatch.delenv("CLAWPROXY_NODE_TOKEN", raising=False)
    assert _env_enablement() is None
    monkeypatch.setenv("CLAWPROXY_NODE_TOKEN", TOKEN)
    monkeypatch.setenv("CLAWPROXY_HERMES_WEBHOOK_SECRET", SECRET)
    seed = _env_enablement()
    assert seed["token"] == TOKEN
    assert seed["hermes_webhook_secret"] == SECRET
