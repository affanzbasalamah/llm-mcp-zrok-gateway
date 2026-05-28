# llm-mcp-zrok-gateway

Self-hosted [zrok v2](https://zrok.io) overlay running on Oracle Cloud
Infrastructure (OCI), used as the foundation for two
[OpenZiti](https://openziti.io) gateways:

- `llm-gateway` — OpenAI-compatible proxy fronting OpenAI/Anthropic/local
  LLM providers, behind a private zrok share
- `mcp-gateway` — Model Context Protocol (MCP) server aggregator, behind
  a private zrok share

zrok runs on the OCI VM. OpenZiti is reused from an external (production)
controller — no Ziti is installed locally.

## Read this first

The full installation walkthrough is in **[INSTALLATION.md](./INSTALLATION.md)**.
It documents every step that worked, every step that didn't, and the
non-obvious fixes for each. Read end-to-end before starting; reproducing
this rollout takes ~30 minutes of hands-on time once prerequisites are
ready.

Once installed, see **[docs/llm-gateway-routing-strategies.md](./docs/llm-gateway-routing-strategies.md)**
for four practical patterns to wire one or more LLM backends (real
Anthropic, real OpenAI, a custom OpenAI-compatible endpoint like
litellm) into the `llm-gateway` service — covering static splits,
all-through-litellm with failover, multi-tenant API-key gating, and
semantic auto-routing.

## Current routing state

This deployment runs **Strategy 3** (multi-tenant API-key gating) — see
**[state/strategy3-applied.txt](./state/strategy3-applied.txt)** for the
full apply log, rollback, and smoke-test results. For how a client actually
reaches the private share (and why there's no URL to open), see
**[docs/llm-gateway-client-access.md](./docs/llm-gateway-client-access.md)**.

Provider slots in `/etc/llm-gateway/config.yaml`:

| Slot      | Upstream                              | Status |
|-----------|---------------------------------------|--------|
| open_ai   | `http://158.178.246.59:11434` (litellm → OpenRouter → `gpt-oss-120b`) | live |
| anthropic | `https://api.anthropic.com` (direct, `claude-haiku-4-5` verified)     | live |
| local     | unconfigured                          | —      |

Three API-key tiers (keys live only on the VM at `/etc/llm-gateway/config.yaml`
and `~/zrok-rollout/secrets/llm-gateway-keys.env`, never in this repo):

| Tier    | `allowed_models`     | Reachable today                                                          |
|---------|----------------------|--------------------------------------------------------------------------|
| admin   | `["*"]`              | any `gpt-*` (→ litellm), any `claude-*` (→ Anthropic)                    |
| dev     | `["gpt-oss-120b"]`   | `gpt-oss-120b` only; everything else 403                                 |
| partner | `["hermes-405b"]`    | none end-to-end — needs a `gpt-*`-prefixed litellm alias to route        |

Routing rule (model-name prefix, from `providers/router.go`):

- `gpt-*` / `o1-*` / `o3-*` → `open_ai` slot (litellm)
- `claude-*` → `anthropic` slot (api.anthropic.com)
- anything else → `local` slot (currently returns `400 provider not configured`)

## Topology

```
                          Internet
                              │
              [OCI VCN sec-list: TCP/443]
              [host iptables: TCP/443]
                              │
                       <public IP>
                              │
                          sg-a1-vm
                              │
                       *:443  zrok2-frontend  ── public TLS
                              │
                     loopback only:
                       127.0.0.1:18080  zrok2-controller API
                       127.0.0.1:5432   postgres (zrok2 db)
                       127.0.0.1:5672   rabbitmq AMQP

                  no local listener (bound to Ziti share only):
                       llm-gateway  ← share "llm-gateway"
                       mcp-gateway  ← share "mcp-gateway"

                              │ outbound → <ziti controller>:1280
                              ↓                  + :3022 router
                       (external OpenZiti)
```

## Notable design decisions

| Topic | Choice |
|---|---|
| OpenZiti | **External, reused** (controller + edge router from a separate production environment) |
| zrok metrics / InfluxDB | **Skipped** — no per-share usage analytics; controller runs without metrics block |
| TLS | Let's Encrypt wildcard, DNS-01 via Cloudflare |
| Public IP strategy | Kept ephemeral (OCI CLI cannot atomically promote → reserved without losing the address; Console can. Document calls this out) |
| Gateway share lifecycle | **Approach A**: shares pre-reserved with `zrok2 create share`, gateways set `share_token` in their config — stable across restarts, no orchestrator IPC required |
| Ziti namespacing | All zrok-managed Ziti objects tagged `owner=zrok-self-hosted-sg`, names prefixed `zrok-*` where we control them |

## Layout of this repo

```
.
├── README.md                 — this file
├── INSTALLATION.md           — the comprehensive how-to
├── .gitignore
├── docs/
│   ├── naming-map.md                       — Ziti object names + tags
│   ├── sync-daemon-awareness.md
│   ├── llm-gateway-routing-strategies.md   — 4 patterns for wiring LLM backends
│   └── llm-gateway-client-access.md        — how clients reach the private share + access matrix
├── scripts/
│   └── teardown-ziti.sh      — Layer-C rollback: deletes zrok-owned Ziti objects
├── systemd/
│   └── zrok-access-llm-gateway.service  — persistent zrok dial re-binding the
│                                          private share onto the tailnet
└── state/
    ├── phase[N]-state.txt          — per-phase artifact reports (with the user's specific OCIDs
    │                                 — useful as a worked example, sanitize before reuse)
    └── strategy3-applied.txt       — current llm-gateway routing config, smoke tests, rollback
```

## Disclaimer

State files under `state/` contain specific identifiers from one
environment (OCIDs, Ziti IDs, IP ranges, descriptions). They are
reference material, not portable templates. Replace any environment-
specific value before running these commands on a different setup.
This repo carries no warranty.
