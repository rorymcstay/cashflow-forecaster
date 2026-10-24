#!/bin/sh
# Joins the tailnet as its own node, publishes the gateway over Funnel, then
# runs the gateway. If either tailscaled or the gateway exits, the container
# exits so Swarm restarts it.
#   TS_HOSTNAME      node name (default budgeting-mcp)
#   TS_TAGS          tags the node advertises (default tag:budgeting-mcp)
#   TS_AUTHKEY_FILE  pre-authorized tagged auth key; only used while the node
#                    is not yet logged in (state persists in /data/tailscale)
set -eu

TS_HOSTNAME="${TS_HOSTNAME:-budgeting-mcp}"
TS_TAGS="${TS_TAGS:-tag:budgeting-mcp}"
TS_AUTHKEY_FILE="${TS_AUTHKEY_FILE:-/run/secrets/ts_authkey}"
PORT="${MCP_GATEWAY_PORT:-8770}"
STATE_DIR=/data/tailscale

mkdir -p "$STATE_DIR" "$FASTMCP_HOME"

tailscaled --tun=userspace-networking --state="$STATE_DIR/tailscaled.state" &
TAILSCALED_PID=$!

for _ in $(seq 1 30); do
    tailscale status >/dev/null 2>&1 && break
    # status exits non-zero while logged out, but the socket answering is enough
    [ -S /var/run/tailscale/tailscaled.sock ] && break
    sleep 1
done

if ! tailscale status --json 2>/dev/null | grep -q '"BackendState": "Running"'; then
    echo "joining tailnet as $TS_HOSTNAME"
    tailscale up \
        --auth-key="file:$TS_AUTHKEY_FILE" \
        --hostname="$TS_HOSTNAME" \
        --advertise-tags="$TS_TAGS" \
        --accept-dns=false
fi

tailscale funnel --bg --https=443 "http://127.0.0.1:$PORT"

python /app/gateway.py &
GATEWAY_PID=$!

trap 'kill "$TAILSCALED_PID" "$GATEWAY_PID" 2>/dev/null || true' TERM INT

# POSIX sh has no `wait -n`: poll until either process is gone.
while kill -0 "$TAILSCALED_PID" 2>/dev/null && kill -0 "$GATEWAY_PID" 2>/dev/null; do
    sleep 2
done
echo "tailscaled or gateway exited; stopping container" >&2
kill "$TAILSCALED_PID" "$GATEWAY_PID" 2>/dev/null || true
exit 1
