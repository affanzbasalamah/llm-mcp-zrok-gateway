# mcp-gateway → JunOS router: end-to-end management via an off-box MCP server

How the `mcp-gateway` service was wired to manage a real Juniper **SRX345-IM**
(Junos 24.2R1.17, LAN address `192.168.0.254`) with an LLM driving read-only
JunOS commands through the gateway. Companion to
[llm-gateway-client-access.md](./llm-gateway-client-access.md).

## TL;DR

```
LLM (llm-gateway, gpt-oss-120b, tool-calling)
   │  OpenAI /v1/chat/completions + tools
   ▼
mcp-gateway   ── private zrok share "mcp-gateway"  (aggregator + read-only tool gating)
   │  http backend, streamable-http + bearer token, over Tailscale
   ▼
hermes  (100.105.37.124) ── junos-mcp-server (jmcp.py) on the JunOS LAN
   │  NETCONF over SSH (key auth, read-only user "mcp")
   ▼
JunOS SRX345-IM  (192.168.0.254)
```

A small agent loop on the OCI VM lists the gateway's tools, hands them to the
`llm-gateway` as OpenAI function tools, and executes the model's tool calls back
through the gateway — so the LLM manages the router end to end.

## Why the MCP server runs on hermes, not the OCI VM

The original intent was an IPsec path (`srx-tunnel`) from the OCI VM straight to
the SRX. **That path is unusable**: the SRX sits behind a consumer modem whose
**IPsec ALG breaks NAT-T** (IKE source remapped to port 1025, no UDP-4500 float,
the IKE_AUTH reply never returns through the modem). Two SRX-side fixes were
still required and applied to even get IKE to negotiate —

```
delete security ike gateway ike-gw-oci no-nat-traversal
set security ipsec vpn vpn-oci traffic-selector ts1 local-ip 192.168.0.0/24 remote-ip 10.0.1.0/24
```

— but the **data plane still fails** through the modem's ALG, and that modem
cannot be reconfigured. So we abandoned IPsec for this and used **Tailscale**:
WireGuard does its own NAT traversal and is unaffected by the IPsec ALG. The MCP
server runs on **hermes**, a tailnet host that sits on the JunOS LAN and reaches
`192.168.0.254` directly; the OCI `mcp-gateway` reaches hermes over the tailnet.

## Components

### hermes (the MCP server host)

- `Juniper/junos-mcp-server` (`jmcp.py`), installed under `~/junos-mcp-server`
  in a **uv** venv (`~/.local/bin/uv`; the system lacked `python3.12-venv` and
  there is no sudo, so `uv` manages the env).
- Runs **streamable-http**, bound to the **tailscale IP** only:
  `jmcp.py -f devices.json -t streamable-http -H 100.105.37.124 -p 30030`
- **Token auth** required (streamable-http refuses to start unauthenticated):
  token id `gateway`, stored in `~/junos-mcp-server/.tokens` (chmod 600).
- `~/junos-mcp-server/devices.json` (chmod 600) — device `srx-im` →
  `192.168.0.254:22`, user `mcp`, `auth.type: ssh_key`,
  key `~/.ssh/junos_mcp_ed25519`. The router has the matching public key under
  `set system login user mcp authentication ssh-ed25519 …` (read-only class).
- Persisted as a **systemd user service** (`systemd/jmcp.service`, installed at
  `~/.config/systemd/user/jmcp.service`); needs `loginctl enable-linger affan`
  for boot survival.

### OCI VM (the gateway host)

- `/etc/llm-gateway`-style backend added to `/etc/mcp-gateway/config.yaml`
  (640 root:mcp-gw) — a single `http` backend:
  - `endpoint: http://100.105.37.124:30030/mcp/`, `protocol: streamable`,
    `allow_insecure: true`, `headers.Authorization: "Bearer <jmcp-token>"`.
  - `tools.mode: allow` with the **six read-only tools** only (fail-safe):
    `get_router_list`, `gather_device_facts`, `get_junos_config`,
    `junos_config_diff`, `execute_junos_command`, `execute_junos_command_batch`.
    `load_and_commit_config` and `add_device` are **excluded** (not exposed).
  - Backup: `/etc/mcp-gateway/config.yaml.bak.20260529T052442Z`.
- The aggregator namespaces tools as `junos_<name>` (separator `_`), so clients
  see e.g. `junos_execute_junos_command`.
- A persistent dialer re-exposes the share locally:
  `systemd/mcp-access-junos-gateway.service` runs, as `llm-gw`,
  `mcp-tools http mcp-gateway --bind 127.0.0.1:8801 --json-response`
  (bound to loopback — the MCP share has no client auth of its own, so it is
  **not** exposed to the tailnet).

## Verified (no-LLM, through the gateway)

- `tools/list` → exactly the 6 `junos_*` read-only tools (write/mgmt filtered).
- `junos_get_router_list` → the `srx-im` device entry.
- `junos_execute_junos_command {command:"show version", router_name:"srx-im"}`
  → live SRX output (`SRX345-IM`, `srx345-dual-ac`, Junos `24.2R1.17`).

## Verified (LLM in the loop)

- `llm-gateway` (`gpt-oss-120b`) supports OpenAI tool-calling
  (`finish_reason: tool_calls`).
- Given "what model and Junos version is the router running?", the model
  autonomously selected `junos_execute_junos_command("show version",
  router_name="srx-im")` — correct tool, args, and device.

## Known limitation — the OCI ⇄ hermes relay flaps

Both ends are behind NAT (OCI 1:1 NAT; hermes behind the SRX), so Tailscale
cannot build a **direct** link and falls back to a **DERP relay** (`sin`). That
relay **flaps**. Consequences:

- The `mcp-gateway` opens **one backend connection per client session** and does
  **not** auto-reconnect. A relay flap kills the connection; calls then fail with
  `connection closed: EOF` or `context deadline exceeded` until the dialing
  client (`mcp-access-junos-gateway` / `mcp-tools http`) is **restarted**
  (`sudo systemctl restart mcp-access-junos-gateway`).
- Single quick calls (`execute_junos_command`) are reliable in a healthy window.
  `gather_device_facts` makes many NETCONF round-trips and is **too slow** over
  the relay — prefer `execute_junos_command` with explicit `show` commands.

Mitigations to consider: keep hermes from idling; pursue a direct Tailscale path;
or run jmcp as its own zrok share and use the gateway's `zrok` transport instead
of http-over-Tailscale.

## Read-only vs config-change

Phase 1 is **read-only** (the allow-list above; router user `mcp` is `read-only`
class). To enable config changes later: add `load_and_commit_config` to the
allow-list, move the `mcp` user to a class permitting configure/commit, and use
JunOS `commit confirmed` as a safety net.

## Secrets (never in this repo)

- jmcp bearer token — hermes `~/junos-mcp-server/.tokens` + the OCI
  `/etc/mcp-gateway/config.yaml` only.
- hermes SSH key for SRX user `mcp` — hermes `~/.ssh/junos_mcp_ed25519` only;
  public half installed on the SRX.
- `devices.json` — hermes only.

## Operate

```bash
# OCI VM
sudo systemctl status  mcp-access-junos-gateway
sudo systemctl restart mcp-access-junos-gateway      # after a relay flap
sudo journalctl -u mcp-gateway -f | grep -iE "junos|backend|tool call"

# hermes
systemctl --user status jmcp
systemctl --user restart jmcp
```
