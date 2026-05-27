# ziti-sync-daemon awareness note

## Identity in question
- name: `ziti-sync-daemon`
- id: `8.4DcE9ecA`
- isAdmin: **true**
- authPolicyId: `default`
- roleAttribute: `sync-reader`

## Implication for zrok rollout

This identity has admin scope, so every Ziti object we create in Phase 5 (zrok-dynamic-proxy-controller service & identity, zrok-public-frontend identity, zrok-dpc-* policies, and every per-share identity zrok creates afterward) **will be visible to whatever process holds this credential**.

## Action

Notify the operator of `ziti-sync-daemon` before Phase 5 that new identities will start appearing under the `zrok-` prefix and tagged `owner=zrok-self-hosted-sg`. If the sync target is a backup/HA peer this is desired. If it's an external system that re-publishes Ziti state somewhere, confirm the destination is acceptable.

No code changes required on this VM.
