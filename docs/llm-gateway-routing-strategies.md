# llm-gateway routing strategies

How to wire one or more LLM backends (real Anthropic, real OpenAI, a
custom OpenAI-compatible endpoint like litellm) into the `llm-gateway`
service deployed in Phase 7. Four practical patterns, with tradeoffs
and the config for each.

The example custom endpoint used throughout is litellm fronting
`gpt-oss-120b` via OpenRouter:

```
http://158.178.246.59:11434     # litellm
                ↓
        OpenRouter API
                ↓
        gpt-oss-120b model
```

Substitute your own endpoint, key, and model as appropriate.

---

## How the gateway routes requests

Before picking a strategy, internalise what the gateway actually does
at runtime. From `providers/router.go:32` in `openziti/llm-gateway`:

```
model name starts with gpt-, o1-, o3-   →  open_ai provider slot
model name starts with claude-          →  anthropic provider slot
anything else                           →  local provider slot
```

There is **one slot per provider type** and **no cross-provider
failover**. If a `claude-*` request fails at the anthropic provider,
the gateway returns the error — it does not retry against `open_ai` or
`local`. Cross-provider fallback must be done outside the gateway.

Auth behaviour by slot:

| Slot | Outbound auth header | Wire format | Path appended |
|---|---|---|---|
| `open_ai` | `Authorization: Bearer <api_key>` | OpenAI chat-completions | `/v1/chat/completions` |
| `anthropic` | `x-api-key: <api_key>` + `anthropic-version: 2023-06-01` | Anthropic Messages | `/v1/messages` |
| `local` | **none** (no auth header sent) | OpenAI chat-completions | `/v1/chat/completions` |

The `local` provider sending no auth header is the single biggest
trap. An OpenAI-compatible backend that requires a bearer token
(litellm with `master_key`, vLLM with API keys, etc.) cannot be placed
in the `local` slot without either dropping its auth or putting a
header-injection sidecar in front.

Files referenced below:
- `/etc/llm-gateway/config.yaml` (chmod 640 root:llm-gw) — provider config
- `/etc/llm-gateway/env` (chmod 600 root:llm-gw) — API key env vars
- Restart after every change: `sudo systemctl restart llm-gateway`

---

## Strategy 1 — Static split by model family

Each backend handles its native model family. No fallback, no
overlap. The simplest pattern.

```yaml
# /etc/llm-gateway/config.yaml
listen: "127.0.0.1:8080"
zrok:
  share:
    enabled: true
    mode: private
    token: "llm-gateway"

providers:
  open_ai:
    api_key: "${OPENAI_API_KEY}"
    # base_url omitted → defaults to https://api.openai.com
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    # base_url omitted → defaults to https://api.anthropic.com
  local:
    base_url: "http://158.178.246.59:11434"   # litellm
```

```
# /etc/llm-gateway/env
OPENAI_API_KEY=sk-<real-openai-key>
ANTHROPIC_API_KEY=sk-ant-<real-anthropic-key>
```

Routing:
- `gpt-*` / `o1-*` / `o3-*` → real OpenAI
- `claude-*` → real Anthropic
- `gpt-oss-120b`, `llama-3.3-70b`, anything else → litellm → OpenRouter → gpt-oss-120b

**Use when:** you have clear separation by model family, no need for
failover, and want each backend used for what it's natively best at.

**Pitfall:** the `local` slot sends no `Authorization` header. Your
litellm must accept unauthenticated requests from this VM, or you must
add a header-injection sidecar on `127.0.0.1`. One-liner pattern using
socat:

```bash
# inject Authorization: Bearer <litellm-key> on 127.0.0.1:11434
# (run as a systemd unit; gateway points local.base_url at 127.0.0.1:11434)
exec mitmdump --mode reverse:http://158.178.246.59:11434 \
              --set Authorization="Bearer <your-litellm-key>" \
              --listen-port 11434
```

(Or use Caddy/nginx with a `proxy_set_header Authorization "Bearer ..."`
directive.)

---

## Strategy 2 — All-through-litellm with provider fallback

The gateway provider slots all point at the same litellm host. litellm
owns the primary→fallback chain.

```yaml
# /etc/llm-gateway/config.yaml — all three slots aimed at litellm
providers:
  open_ai:
    api_key: "${LITELLM_KEY}"
    base_url: "http://158.178.246.59:11434"
  anthropic:
    api_key: "${LITELLM_KEY}"
    base_url: "http://158.178.246.59:11434"
  local:
    base_url: "http://158.178.246.59:11434"   # only reachable for non-gpt/non-claude names
```

```
# /etc/llm-gateway/env
LITELLM_KEY=<your-litellm-key>
```

On the litellm host (separate from this VM), configure `model_list`
with primary entries and a `fallbacks` map:

```yaml
# litellm config.yaml
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY

  - model_name: claude-3-5-sonnet-20241022
    litellm_params:
      model: anthropic/claude-3-5-sonnet-20241022
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: gpt-oss-120b
    litellm_params:
      model: openrouter/openai/gpt-oss-120b
      api_key: os.environ/OPENROUTER_API_KEY

litellm_settings:
  fallbacks:
    - gpt-4o:                       [gpt-oss-120b]
    - claude-3-5-sonnet-20241022:   [gpt-oss-120b]
  num_retries: 2
  request_timeout: 60
```

Request flow when a client asks for `claude-3-5-sonnet-20241022`:

```
client → llm-gateway anthropic slot (POST /v1/messages, x-api-key litellm-key)
        → litellm receives Anthropic-format payload
        → litellm tries anthropic/claude-3-5-sonnet-20241022 (real Anthropic)
        → on 429/5xx/timeout, litellm falls back to openrouter/openai/gpt-oss-120b
        → response translated back to Anthropic format
        → returned to client
```

**Use when:** spend control, retries, and graceful degradation matter
more than peak feature parity. Centralises observability — one place
to inspect logs, set budgets, rotate keys.

**Pitfall:** every request adds one network hop and you lose access to
features the gateway translates natively but litellm doesn't (e.g.
gateway's hardcoded `anthropic-version: 2023-06-01` becomes
litellm's choice instead). Verify litellm's Anthropic endpoint accepts
the same payload shape — your existing litellm responds correctly on
`POST /v1/messages` per Phase-8-era probing, so this works today.

---

## Strategy 3 — Multi-tenant access control by API key

The gateway has built-in API-key gating with per-key model
whitelists (`gateway/config.go:75`). Add it on top of any other
strategy.

```yaml
# /etc/llm-gateway/config.yaml — add api_keys block to Strategy 1 or 2
providers:
  open_ai:    { api_key: "${OPENAI_API_KEY}" }
  anthropic:  { api_key: "${ANTHROPIC_API_KEY}" }
  local:      { base_url: "http://158.178.246.59:11434" }

api_keys:
  enabled: true
  keys:
    - name: "internal-prod"
      key: "${PROD_KEY}"
      allowed_models:
        - gpt-4o
        - claude-3-5-sonnet-20241022
        - gpt-oss-120b
    - name: "dev-free-tier"
      key: "${DEV_KEY}"
      allowed_models:
        - gpt-oss-120b                # dev users limited to the free model
    - name: "external-partner"
      key: "${PARTNER_KEY}"
      allowed_models:
        - gpt-4o                      # partner locked to one paid model
```

Generate keys with `llm-gateway genkey`. Add them to `env` as
`PROD_KEY=...`, `DEV_KEY=...`, etc.

Clients now must supply their key as `Authorization: Bearer <key>`.
The gateway validates against the keys list and enforces the
`allowed_models` whitelist before routing.

**Use when:** multiple consumers share the zrok share — e.g. one
internal-tooling key, one CI/automation key, one external partner
key. Hard budget guard without a separate billing system.

**Pitfall:** the gateway's API-key denial is "model not allowed" only;
it does not enforce per-key request rate or token budgets. For real
quota enforcement, put litellm in front (Strategy 2) and use its
virtual-key feature.

---

## Strategy 4 — Semantic auto-routing (cheapest by default)

The gateway has a three-layer semantic router (`routing/routing.go`):
keyword heuristics → embedding similarity → optional LLM classifier.
When clients send a request with no `model` field, the router picks
one based on prompt content.

```yaml
# /etc/llm-gateway/config.yaml
providers:
  open_ai:    { api_key: "${OPENAI_API_KEY}" }
  anthropic:  { api_key: "${ANTHROPIC_API_KEY}" }
  local:      { base_url: "http://158.178.246.59:11434" }

routing:
  enabled: true
  default_model: "gpt-oss-120b"          # fallback when nothing else matches

  semantic:
    enabled: true
    provider: local                       # use litellm's embedding endpoint
    model: text-embedding-3-small         # litellm must expose this alias
    cache_size: 1000

  rules:
    - models: ["claude-3-5-sonnet-20241022"]
      keywords: ["refactor", "agentic", "tool use", "long context", "codebase"]
      embedding_examples:
        - "rewrite this function to use async/await"
        - "trace where this bug originates across three files"

    - models: ["gpt-4o"]
      keywords: ["image", "vision", "screenshot", "json schema", "function calling"]
      embedding_examples:
        - "describe what's in this image"
        - "return structured JSON matching this schema"

    # everything else falls through to default_model (gpt-oss-120b)
```

Request flow when a client sends `{"messages":[...], "model": ""}`:

```
prompt = "rewrite this function to use async/await"
  → heuristics: matches "rewrite" keyword → claude-3-5-sonnet-20241022
  → routed to anthropic slot

prompt = "what's the weather today"
  → heuristics: no match
  → embeddings: no high-similarity rule
  → classifier (optional): no clear answer
  → default_model = gpt-oss-120b
  → routed to local slot
```

**Use when:** most traffic is generic and cheap to serve, only a
minority needs premium models. Maximises the free-tier
gpt-oss-120b capacity while still upgrading hard cases automatically.

**Pitfall:** the embedding lookup itself costs a round-trip. Cache
size matters. Also: clients calling with an explicit `model` field
bypass semantic routing entirely — useful escape hatch, but worth
documenting to consumers.

---

## Decision matrix

| Need | Best strategy |
|---|---|
| Just plumb three keys, minimum config | 1 |
| Need failover when a real API rate-limits or errors | 2 |
| Multiple consumers, each with different budget | 3 (on top of 1 or 2) |
| Mostly cheap traffic, occasional premium escalation | 4 |
| All of the above | 2 + 3 + 4 stacked |

For this rollout's specific consumers (internal MCP toolchain + LLM
gateway behind private zrok shares, low-volume, dev-leaning), **Strategy
2** is the recommended default: it gives the gateway the single most
useful capability it doesn't have natively (cross-provider failover)
while keeping spend visibility centralised in litellm.

---

## Applying a strategy

1. Edit `/etc/llm-gateway/config.yaml` (owned by `root:llm-gw`,
   readable by the service via group membership).
2. Edit `/etc/llm-gateway/env` to add or change API key env vars.
3. `sudo systemctl restart llm-gateway`
4. Verify:
   ```bash
   journalctl -u llm-gateway -n 50 --no-pager   # confirm provider init messages
   curl -sS http://127.0.0.1:8080/v1/models      # list models from all configured providers
   ```
5. Smoke test each route:
   ```bash
   # gpt-* path
   curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'

   # claude-* path
   curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'

   # local fallthrough
   curl -sS -X POST http://127.0.0.1:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
   ```

The gateway logs the chosen provider per request:
```
routing model 'claude-3-5-sonnet-20241022' to anthropic
```
Confirm the right slot fires before declaring done.
