# clawproxy for Hermes Agent

Receive webhooks from the public internet in [Hermes Agent](https://hermes-agent.nousresearch.com) without exposing Hermes, opening ports, or running a tunnel.

GitHub, Stripe, Slack or any other provider posts to a public [clawproxy](https://clawproxy.io) route. This plugin holds your clawproxy node's connection inside the Hermes gateway and hands each event to **your existing Hermes webhook route of the same name**. Prompt templates, `deliver` targets, skills and toolsets all keep working. Events usually arrive about a second after the provider sends them.

```
provider ──POST──▶ clawproxy.io/api/ingress/<you>/<route>
                        │  (stored until delivered)
                        ▼  WebSocket push (HTTP pull fallback)
          Hermes gateway: clawproxy plugin
                        │  signed POST (HMAC V2)
                        ▼
          Hermes webhook platform: /webhooks/<route>  →  your prompt, your deliver target
```

## Install

```bash
hermes plugins install ericdahl-dev/clawproxy-hermes
```

Then set two values (in `~/.hermes/.env`, or your profile's `.env`):

```bash
CLAWPROXY_NODE_TOKEN=cpn_...            # clawproxy dashboard → Nodes → Add node
CLAWPROXY_HERMES_WEBHOOK_SECRET=...     # platforms.webhook.extra.secret in ~/.hermes/config.yaml
```

Restart the gateway. `hermes gateway status` should list **clawproxy** as connected.

## The one rule: route names match

A clawproxy route named `github-prs` delivers to the Hermes webhook route `github-prs`. Create the Hermes route first (with its `prompt` and `deliver`), then a clawproxy route with the same slug, and point your provider at the clawproxy route's public URL.

Example Hermes route (`~/.hermes/config.yaml`):

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      secret: <your secret>
      routes:
        github-prs:
          events: [pull_request]
          prompt: "A GitHub pull request event arrived: {__raw__}. Summarize it in two lines."
          deliver: telegram
```

## How delivery works

- **Signing.** Provider signature headers are removed and each request is re-signed with your webhook secret using Hermes's replay-safe `X-Webhook-Signature-V2` scheme. Event-type headers such as `X-GitHub-Event` pass through, so route `events` filters still work.
- **At-least-once, no duplicates.** The plugin acknowledges an event to clawproxy only after Hermes answers 2xx. Anything else is retried by clawproxy. Each request carries the clawproxy event id as `X-Request-ID`, so Hermes drops a redelivered event instead of running it twice.
- **Resilient.** The plugin reconnects with backoff. While the WebSocket is down it pulls queued events over HTTP. A rejected token stops retrying and shows in `hermes gateway status`.
- **Inbound only.** Replies go wherever the webhook route's `deliver` sends them.

## Optional settings

| Env var | Default | |
|---|---|---|
| `CLAWPROXY_SERVER_URL` | `https://clawproxy.io` | Point at a self-hosted clawproxy |
| `CLAWPROXY_HERMES_WEBHOOK_URL` | `http://127.0.0.1:8644` | Where the Hermes webhook platform listens |

Each setting can also go under `platforms.clawproxy.extra` in `config.yaml` (`token`, `hermes_webhook_secret`, `server`, `hermes_webhook_url`), which takes precedence over the env var.

## Development

Tests run against a pinned Hermes Agent commit (`HERMES_PIN`), because Hermes isn't published as a wheel:

```bash
mkdir .hermes-src && cd .hermes-src && git init -q \
  && git remote add origin https://github.com/NousResearch/hermes-agent.git \
  && git fetch -q --depth 1 origin "$(cat ../HERMES_PIN)" && git checkout -q FETCH_HEAD && cd ..
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ./.hermes-src pytest pytest-asyncio "aiohttp==3.14.3" "websockets==15.0.1"
.venv/bin/python -m pytest -q
```
