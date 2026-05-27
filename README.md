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
│   ├── naming-map.md         — Ziti object names + tags
│   └── sync-daemon-awareness.md
├── scripts/
│   └── teardown-ziti.sh      — Layer-C rollback: deletes zrok-owned Ziti objects
└── state/
    └── phase[N]-state.txt    — per-phase artifact reports (with the user's specific OCIDs
                                 — useful as a worked example, sanitize before reuse)
```

## Disclaimer

State files under `state/` contain specific identifiers from one
environment (OCIDs, Ziti IDs, IP ranges, descriptions). They are
reference material, not portable templates. Replace any environment-
specific value before running these commands on a different setup.
This repo carries no warranty.
