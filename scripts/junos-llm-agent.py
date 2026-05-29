#!/usr/bin/env python3
"""LLM agent loop — manage the JunOS SRX read-only, end to end through the stack.

Chain:
    this script
      -> llm-gateway (OpenAI-compatible; the model decides which tool to call)
      -> mcp-gateway (private zrok share, reached via the local mcp-tools dialer)
      -> Tailscale -> hermes (junos-mcp-server) -> NETCONF -> SRX

What it does: connects to the mcp-gateway (through the local dialer), lists the
gateway's tools, hands them to the llm-gateway as OpenAI function tools, then
runs the tool-call loop until the model produces a final answer.

Prereqs (on the OCI VM):
  - systemd unit `mcp-access-junos-gateway` running (dialer on 127.0.0.1:8801)
  - llm-gateway reachable (tailnet bind); export the admin-tier key:
        export LLM_GW_KEY=<admin-tier-key>     # lives in /etc/llm-gateway + secrets, NOT here

Usage:
    python3 junos-llm-agent.py "what model and junos version is the router running?"

Note: the OCI<->hermes Tailscale link is a DERP relay and can flap; the
mcp-gateway holds one backend connection per client session and does not
auto-reconnect. On `EOF`/connection errors this script restarts the dialer
(fresh gateway session -> fresh backend connection) and retries once.
"""
import json, os, sys, time, subprocess, urllib.request

MCP    = os.environ.get("MCP_URL", "http://127.0.0.1:8801/mcp/")
LLM    = os.environ.get("LLM_URL", "http://100.74.151.2:8800/v1/chat/completions")  # env-specific tailnet IP
KEY    = os.environ.get("LLM_GW_KEY") or sys.exit("set LLM_GW_KEY=<admin-tier-key>")
MODEL  = os.environ.get("LLM_MODEL", "gpt-oss-120b")
ROUTER = os.environ.get("ROUTER_NAME", "srx-im")
DIALER = "mcp-access-junos-gateway"
QUESTION = " ".join(sys.argv[1:]) or "what hardware model and Junos version is the router running?"
_sid = None

def _mp(method, params=None, notif=False, timeout=70):
    global _sid
    body = {"jsonrpc": "2.0", "method": method}
    if not notif: body["id"] = 1
    if params is not None: body["params"] = params
    req = urllib.request.Request(MCP, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if _sid: req.add_header("mcp-session-id", _sid)
    resp = urllib.request.urlopen(req, timeout=timeout)
    ns = resp.headers.get("mcp-session-id")
    if ns: _sid = ns
    raw = resp.read().decode().strip()
    if notif: return None
    if raw.startswith(("event:", "data:")) or "\ndata:" in raw:
        for ln in raw.splitlines():
            if ln.startswith("data:"): return json.loads(ln[5:].strip())
    return json.loads(raw)

def mcp_init():
    global _sid
    _sid = None
    _mp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "junos-llm-agent", "version": "1"}})
    _mp("notifications/initialized", notif=True)

def mcp_call(name, args):
    try:
        return _mp("tools/call", {"name": name, "arguments": args})
    except Exception as e:
        print(f"   (tool call failed: {type(e).__name__}; restarting dialer + retrying)")
        subprocess.run(["sudo", "systemctl", "restart", DIALER], capture_output=True)
        time.sleep(9)
        try:
            mcp_init()
            return _mp("tools/call", {"name": name, "arguments": args})
        except Exception as e2:
            return {"error": {"message": str(e2)[:140]}}

def llm(messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools,
            "tool_choice": "auto", "max_tokens": 800, "temperature": 0}
    req = urllib.request.Request(LLM, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]

def main():
    mcp_init()
    tools = _mp("tools/list", {})["result"]["tools"]
    oa = [{"type": "function", "function": {
              "name": t["name"], "description": (t.get("description") or "")[:900],
              "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
          for t in tools]
    print("tools:", [t["function"]["name"] for t in oa])
    messages = [
        {"role": "system", "content": (
            f"You manage a Juniper JunOS router via the provided tools. router_name is "
            f"'{ROUTER}'. Use junos_execute_junos_command with standard 'show' commands, "
            f"one command per call. Never use junos_gather_device_facts. Gather what you "
            f"need, then summarize.")},
        {"role": "user", "content": QUESTION},
    ]
    print("USER:", QUESTION)
    for _ in range(8):
        m = llm(messages, oa)
        a = {"role": "assistant", "content": m.get("content")}
        if m.get("tool_calls"): a["tool_calls"] = m["tool_calls"]
        messages.append(a)
        if not m.get("tool_calls"):
            print("\n=== FINAL ANSWER ===\n" + (m.get("content") or ""))
            return
        for tc in m["tool_calls"]:
            fn = tc["function"]["name"]
            args = json.loads(tc["function"].get("arguments") or "{}")
            print(f"\n[LLM -> tool] {fn}({args.get('command', args)})")
            res = mcp_call(fn, args)
            txt = ("".join(c.get("text", "") for c in res.get("result", {}).get("content", []))
                   if res and "result" in res else "ERROR: " + json.dumps(res.get("error", res))[:160])
            print("[router ->]", txt[:200].replace("\n", " | "))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": txt[:4000]})

if __name__ == "__main__":
    main()
