"""Step 1: the bridge's signatures must pass Hermes's real webhook signature check."""
from types import SimpleNamespace

import pytest
from multidict import CIMultiDict

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

from clawproxy_hermes.bridge import prepare_headers

SECRET = "f" * 64  # same shape as a Hermes webhook secret: 64 hex chars
BODY = b'{"action":"opened"}'


@pytest.fixture
def hermes_webhook():
    return WebhookAdapter(PlatformConfig(enabled=True, extra={"port": 0, "secret": SECRET, "routes": {}}))


def hermes_accepts(adapter, headers, body=BODY, route="gh"):
    request = SimpleNamespace(headers=CIMultiDict(headers), match_info={"route_name": route})
    return adapter._validate_signature(request, body, SECRET)


def test_signed_headers_pass_hermes_validation(hermes_webhook):
    headers = prepare_headers({}, event_id="evt-1", body=BODY, secret=SECRET)
    assert hermes_accepts(hermes_webhook, headers)


def test_uses_replay_safe_v2_not_legacy_v1(hermes_webhook):
    headers = prepare_headers({}, event_id="evt-1", body=BODY, secret=SECRET)
    assert "X-Webhook-Signature-V2" in headers
    assert "X-Webhook-Timestamp" in headers
    assert "X-Webhook-Signature" not in headers


def test_wrong_secret_is_rejected(hermes_webhook):
    headers = prepare_headers({}, event_id="evt-1", body=BODY, secret="0" * 64)
    assert not hermes_accepts(hermes_webhook, headers)


def test_provider_signature_headers_would_break_validation(hermes_webhook):
    """Control: why stripping is required. A leaked GitHub signature makes Hermes check it instead."""
    signed = prepare_headers({}, event_id="evt-1", body=BODY, secret=SECRET)
    leaked = {**signed, "X-Hub-Signature-256": "sha256=deadbeef"}
    assert not hermes_accepts(hermes_webhook, leaked)


@pytest.mark.parametrize("provider_header", [
    {"X-Hub-Signature-256": "sha256=deadbeef"},
    {"X-Hub-Signature": "sha1=deadbeef"},
    {"svix-id": "msg_1", "svix-timestamp": "1", "svix-signature": "v1,abc"},
    {"webhook-id": "msg_1", "webhook-timestamp": "1", "webhook-signature": "v1,abc"},
    {"Linear-Signature": "abc"},
    {"X-Gitlab-Token": "tok"},
    {"X-Webhook-Signature": "abc"},
])
def test_provider_signature_headers_are_stripped(hermes_webhook, provider_header):
    headers = prepare_headers(provider_header, event_id="evt-1", body=BODY, secret=SECRET)
    lowered = {k.lower() for k in headers}
    for name in provider_header:
        if name.lower() not in ("x-webhook-signature",):
            assert name.lower() not in lowered
    assert hermes_accepts(hermes_webhook, headers)


def test_event_type_headers_pass_through():
    original = {"X-GitHub-Event": "pull_request", "X-GitLab-Event": "Push Hook", "Content-Type": "application/json"}
    headers = prepare_headers(original, event_id="evt-1", body=BODY, secret=SECRET)
    assert headers["X-GitHub-Event"] == "pull_request"
    assert headers["X-GitLab-Event"] == "Push Hook"
    assert headers["Content-Type"] == "application/json"


def test_request_id_is_the_clawproxy_event_id_for_dedupe():
    headers = prepare_headers({}, event_id="evt-42", body=BODY, secret=SECRET)
    assert headers["X-Request-ID"] == "evt-42"


def test_hop_by_hop_headers_are_dropped():
    original = {"Host": "clawproxy.io", "Content-Length": "999", "Connection": "keep-alive",
                "Transfer-Encoding": "chunked"}
    headers = prepare_headers(original, event_id="evt-1", body=BODY, secret=SECRET)
    assert not {"host", "content-length", "connection", "transfer-encoding"} & {k.lower() for k in headers}
