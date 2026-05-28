# llm-gateway client access — reaching the private zrok share

How a client actually talks to the `llm-gateway` service, why there is no URL
to open in a browser, and the access model behind it. Companion to
[llm-gateway-routing-strategies.md](./llm-gateway-routing-strategies.md) (which
covers wiring the *backends*); this doc covers reaching the gateway from the
*client* side.

## TL;DR

The gateway runs behind a **private** zrok share, so it has no public URL. A
client reaches it by *dialing* the share through the Ziti overlay and talking
HTTP to a local port, authenticated with a per-tier API key.

Today the VM runs a persistent dial bound to its Tailscale address
(`systemd/zrok-access-llm-gateway.service`), so any tailnet peer can call the
gateway as a plain OpenAI-compatible endpoint with no zrok client at all:

```bash
curl -sS -X POST http://100.74.151.2:8800/v1/chat/completions \
  -H "Authorization: Bearer <TIER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"hi"}]}'
```

Or point any OpenAI SDK at base URL `http://100.74.151.2:8800/v1` + a tier key.

## Who can call what

Auth is the gateway's own per-key middleware (`allowed_models`), independent of
zrok. Keys live only on the VM (`/etc/llm-gateway/config.yaml` and
`~/zrok-rollout/secrets/llm-gateway-keys.env`) — never in this repo.

| Tier (key) | Allowed models       | Backend reached            | Status |
|------------|----------------------|----------------------------|--------|
| admin      | `["*"]`              | `gpt-*` → litellm; `claude-*` → Anthropic | both routes live |
| dev        | `["gpt-oss-120b"]`   | litellm → OpenRouter only  | anything else → 403 |
| partner    | `["hermes-405b"]`    | —                          | unroutable today → 400 (needs a `gpt-*` litellm alias) |

Short version: **dev = litellm `gpt-oss-120b` only; admin = everything,
including the Anthropic (`claude-*`) route.** Partner is locked out until the
alias is added.

## Why there's no URL to open

- A **private** share is deliberately *not* served by the public `:443`
  frontend — that frontend only routes **public** shares (and serves the zrok
  SPA landing page, which is what you get if you browse to the domain).
- The zrok **controller API** is loopback-only (`127.0.0.1:18080`) and is *not*
  proxied through `:443`. So an off-box machine can't even reach the control
  plane to `enable`/`access` without further exposure work.
- The gateway has **no local TCP listener** — in zrok-share mode it binds only
  to the Ziti share, so there is no `127.0.0.1:8080` to SSH-tunnel to. The only
  path in is a zrok `access` dial through the overlay.

So something must *dial* the share. The question is only *where that dialer
runs* and *how clients reach it*.

## Three ways to get a client in

| Option | Client experience | Exposure | Effort |
|--------|-------------------|----------|--------|
| **A. Tailnet re-bind** (deployed) | tailnet peers hit `http://100.74.151.2:8800/v1/...` + tier key | none public — tailnet only; data plane stays in Ziti | low — one systemd unit |
| **B. Public share** | anyone hits `https://<token>.<zone>/v1/...` + tier key | gateway becomes internet-facing (still gated by tier keys, but Ziti zero-trust dropped) | low — config flip to `mode: public` |
| **C. Expose controller API** | remote machines run their own `zrok2 enable` + `access` | control plane published behind TLS; per-client revocable Ziti identities | high — reverse proxy + token mgmt |

Option A is deployed because the clients here are on the tailnet. Option C is
the right choice only when handing access to many independent outside parties
who each need their own revocable identity.

## The deployed path: tailnet-bound systemd unit

`systemd/zrok-access-llm-gateway.service` runs, as the `mcp-gw` identity (the
Dial side; `llm-gw` owns the Bind side):

```
zrok2 access private llm-gateway --bind 100.74.151.2:8800 --headless
```

It depends on `tailscaled.service` and sets `StartLimitIntervalSec=0` +
`Restart=on-failure`, so it keeps retrying the bind until the Tailscale address
exists (handles boot ordering and tailnet flaps). The VM is the zrok client;
remote machines need nothing but an HTTP client and a tier key.

Manage: `sudo systemctl {status,restart,stop,disable} zrok-access-llm-gateway`

Tailnet ACLs must permit peers to reach `:8800` (default tailnet policy does).

## Running a real zrok client from another machine

If instead you want a remote machine to be a true zrok client (Option C), it
needs four things:

1. The `zrok2` binary, **v2.x to match the v2.0.4 controller** (a v1
   `brew install zrok` will not pair correctly). macOS ships darwin arm64/amd64
   builds; the release binary is usually named `zrok`, not `zrok2`.
2. A **reachable controller API** — currently loopback-only, so this requires
   exposing `127.0.0.1:18080` (over Tailscale, or publicly behind TLS) first.
3. A one-time account/enable token → `zrok enable <token>`.
4. A gateway tier key for the actual API calls.

Then:

```bash
zrok config set apiEndpoint <reachable-controller-url>
zrok enable <account-token>
zrok access private llm-gateway --bind 127.0.0.1:8800
# point the SDK at http://127.0.0.1:8800/v1 + tier key
```

## zrok vs OpenZiti vs Tailscale (clearing up the dependency)

- **zrok does not require Tailscale.** Tailscale is incidental to this VM — it
  was already running and is a convenient private transport to reach the
  loopback-bound controller/share without new public surface. A standard zrok
  deployment (including hosted `zrok.io`) exposes the controller API publicly
  behind TLS and clients connect over the open internet — no Tailscale anywhere.
- **zrok's real network is OpenZiti.** The zero-trust data plane that carries
  the private share rides the (external) Ziti overlay. That is zrok's actual
  dependency.
- **zrok is an orchestration layer over the Ziti management API.** `zrok enable`
  mints and enrolls a Ziti identity for you; `zrok share`/`access` auto-create
  the Ziti service + bind/dial policies. The zrok client is effectively a Ziti
  tunneler (dial a dark service, re-expose it on a local port) with a control
  plane (accounts, tokens, shares) bolted on — so you skip the manual
  identity/service/policy setup that raw Ziti would require.
