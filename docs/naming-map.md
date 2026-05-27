# zrok-self-hosted-sg → external Ziti naming map

Captured 2026-05-26 against production controller `zerotrust.salamahsystems.com:1280`.

## Collision-check results (all green)

| Check | Result |
|---|---|
| ERP named `default` | 0 — safe to skip create (see ERP reuse below) |
| SERP named `default` | 0 — safe to create `zrok-default-serp` |
| Service/identity named `dynamicProxyController` | 0 — safe to create `zrok-dynamic-proxy-controller` |
| Anything `zrok-` prefixed | 0 — clean namespace |
| Identity with roleAttribute `public` | 0 — `@public` references would not collide |
| Identity with roleAttribute `all` | 0 |
| Tag `owner=zrok-self-hosted-sg` already in use | 0 |

Existing identity roleAttributes (do not collide with our additions): `admin`, `k3s-host`, `sync-reader`, `ziti-gtw`.

## Edge-router-policy decision

**Skip creating a new ERP.** The existing policy `all-endpoints-public-router` (id `2WZ7SLnitL5aHeWq1FiQdG`) grants `#all` identities access to edge router `@CW89hEGDdy` — the only edge router on this network. Every identity we create (including `zrok-dynamic-proxy-controller` and every per-share identity) will automatically get router access through it. No `zrok-default-erp` needed.

## Final naming map

| zrok docs concept | Name we use | Type | Notes |
|---|---|---|---|
| controller dynamic-proxy service | `zrok-dynamic-proxy-controller` | Ziti service | tagged `owner=zrok-self-hosted-sg` |
| controller bind identity | `zrok-dynamic-proxy-controller` | Ziti identity | tagged |
| public frontend identity | `zrok-public-frontend` | Ziti identity | tagged; gets role attribute `zrok-share` (see below) |
| service-edge-router-policy for the dpc service | `zrok-dpc-serp` | service-edge-router-policy | tagged |
| service-policy (Bind) | `zrok-dpc-bind` | service-policy | tagged |
| service-policy (Dial) | `zrok-dpc-dial` | service-policy | tagged |

**Role attribute decision:** Use `zrok-share` (not `public`) for per-share identities created by `zrok2 enable`. This keeps the zrok namespace cleanly separable in role-attribute searches even though `public` is currently unused. Where the upstream zrok docs reference `@public` in dial policies, we substitute `@zrok-share`.

## Tagging

Every Ziti object created by this rollout MUST carry:
```
--tags '{"owner":"zrok-self-hosted-sg","createdBy":"affan-zrok-rollout-2026-05"}'
```

Caveat: per-share identities created later by zrok internals (`zrok2 enable`) won't auto-carry this tag. They will be identifiable by name prefix instead.

Inventory query post-bootstrap:
```bash
ziti edge list identities 'tags.owner = "zrok-self-hosted-sg"' -j | jq '.data[].name'
ziti edge list services   'tags.owner = "zrok-self-hosted-sg"' -j | jq '.data[].name'
```
