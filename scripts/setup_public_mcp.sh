#!/usr/bin/env bash
# Puts the budgeting MCP server on public HTTPS behind GitHub OAuth, so it can
# be added as a custom connector in the Claude app (web + phone).
#
# Fully independent of tradingo's mcp-gateway: its own overlay network, Swarm
# services, Docker secrets, volumes, GitHub OAuth app and Tailscale node/Funnel.
#
#   internet --Funnel--> budgeting-mcp-gateway (OAuth + tailscale, one container)
#                              |  overlay network "budgeting"
#                              v
#                        budgeting-mcp (streamable HTTP, SQLite on budgeting-db)
#
# Idempotent: every step is "create if missing, else update"; re-running is safe.
# Drives the Swarm remotely, so no local Docker daemon is needed.
#
# Usage: scripts/setup_public_mcp.sh [--rotate-github] [--skip-verify]
#   --rotate-github  replace the GitHub OAuth client id/secret (removes the
#                    gateway service first: in-use secrets can't be removed)
#   --skip-verify    skip the end-to-end checks
#
# Optional environment (all have defaults):
#   DOCKER_HOST                       default ssh://admin@nuc-05.tailfe5b8d.ts.net
#   BUDGETING_MCP_PUBLIC_URL          default https://budgeting-mcp.tailfe5b8d.ts.net
#   BUDGETING_MCP_ALLOWED_USERS       GitHub logins allowed in (default rorymcstay)
#   BUDGETING_MCP_LAN_PORT            LAN port for the *unauthenticated* backend
#                                     (default 8769; "none" unpublishes it - the
#                                     gateway reaches it over the overlay anyway)
#   BUDGETING_MCP_GITHUB_CLIENT_ID / _SECRET, BUDGETING_MCP_TS_AUTHKEY
#                                     skip the interactive prompts
#   TS_API_KEY                        create the Tailscale auth key via the API
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-ssh://admin@nuc-05.tailfe5b8d.ts.net}"
PUBLIC_URL="${BUDGETING_MCP_PUBLIC_URL:-https://budgeting-mcp.tailfe5b8d.ts.net}"
PUBLIC_URL="${PUBLIC_URL%/}"
TS_HOSTNAME="${PUBLIC_URL#https://}"
TS_HOSTNAME="${TS_HOSTNAME%%.*}"
TS_TAG="tag:budgeting-mcp"
ALLOWED_USERS="${BUDGETING_MCP_ALLOWED_USERS:-rorymcstay}"
LAN_PORT="${BUDGETING_MCP_LAN_PORT:-8769}"

NETWORK=budgeting
PLACEMENT="node.labels.budgeting-db==true" # the node holding the local budgeting-db volume
BACKEND_SVC=budgeting-mcp
BACKEND_PORT=8000
GATEWAY_SVC=budgeting-mcp-gateway
GATEWAY_VOLUME=budgeting-mcp-gateway-data
GATEWAY_PORT=8770
REGISTRY=192.168.0.50:5000
GATEWAY_IMAGE="$REGISTRY/budgeting-mcp-gateway:latest"
BACKEND_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm
SECRET_CLIENT_ID=budgeting_mcp_github_client_id
SECRET_CLIENT_SECRET=budgeting_mcp_github_client_secret
SECRET_JWT_KEY=budgeting_mcp_jwt_key
SECRET_TS_AUTHKEY=budgeting_mcp_ts_authkey

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$HERE/../deploy/public_mcp"

ROTATE_GITHUB=0
VERIFY=1
for arg in "$@"; do
    case "$arg" in
        --rotate-github) ROTATE_GITHUB=1 ;;
        --skip-verify) VERIFY=0 ;;
        -h | --help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 64 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Exit 2 = "needs a human step"; the message above it says exactly which.
needs_input() { printf '\nACTION NEEDED: %s\n' "$*" >&2; exit 2; }

service_exists() { docker service inspect "$1" >/dev/null 2>&1; }
secret_exists() { docker secret inspect "$1" >/dev/null 2>&1; }
network_exists() { docker network inspect "$1" >/dev/null 2>&1; }

# --- instructions for the things only a human can do -------------------------

github_app_instructions() {
    cat >&2 <<EOF

------------------------------------------------------------------------
GitHub OAuth App (there is no API to create one)
  1. Open https://github.com/settings/applications/new
  2. Application name:           budgeting-mcp
     Homepage URL:               $PUBLIC_URL
     Authorization callback URL: $PUBLIC_URL/
                                 ^ the ROOT, WITH the trailing slash. The gateway
                                   sends /budgeting/auth/callback and GitHub only
                                   accepts subpaths of the registered URL; anything
                                   else gives "redirect_uri is not associated with
                                   this application".
  3. Register application, then "Generate a new client secret".
  4. Re-run this script and paste the Client ID and Client secret when asked
     (or export BUDGETING_MCP_GITHUB_CLIENT_ID / BUDGETING_MCP_GITHUB_CLIENT_SECRET).
------------------------------------------------------------------------
EOF
}

tailscale_instructions() {
    cat >&2 <<EOF

------------------------------------------------------------------------
Tailscale policy + auth key (needed once, for the node's first join)
  1. https://login.tailscale.com/admin/acls  - in the policy file add:
       "tagOwners": { "$TS_TAG": ["autogroup:admin"] }      (merge into your existing tagOwners)
       "nodeAttrs": [ { "target": ["$TS_TAG"], "attr": ["funnel"] } ]   (merge into nodeAttrs)
  2. https://login.tailscale.com/admin/settings/keys - Generate auth key:
       Reusable: off   Ephemeral: off   Pre-approved: on   Tags: $TS_TAG
     (tagged nodes never expire). Paste it when asked, or export
     BUDGETING_MCP_TS_AUTHKEY, or export TS_API_KEY to have this script create one.
  MagicDNS and HTTPS certificates must already be enabled on the tailnet.
------------------------------------------------------------------------
EOF
}

# Read a value: env var if set, else prompt on a TTY, else stop with instructions.
# usage: ask VARNAME "prompt" secret|plain instructions_fn
ask() {
    local var="$1" prompt="$2" mode="$3" instructions="$4" value
    value="${!var:-}"
    if [ -z "$value" ]; then
        "$instructions"
        [ -t 0 ] || needs_input "$var is not set and there is no terminal to prompt on."
        if [ "$mode" = secret ]; then
            read -r -s -p "$prompt: " value; echo
        else
            read -r -p "$prompt: " value
        fi
    fi
    [ -n "$value" ] || die "empty value for $prompt"
    printf '%s' "$value"
}

# Secret values are piped in with printf '%s' so there is no trailing newline.
create_secret() {
    printf '%s' "$2" | docker secret create "$1" - >/dev/null
    note "created secret $1"
}

tailscale_create_authkey() {
    curl -fsS -u "$TS_API_KEY:" -H 'Content-Type: application/json' \
        -X POST https://api.tailscale.com/api/v2/tailnet/-/keys \
        -d "{\"description\":\"budgeting-mcp gateway\",\"expirySeconds\":3600,\"capabilities\":{\"devices\":{\"create\":{\"reusable\":false,\"ephemeral\":false,\"preauthorized\":true,\"tags\":[\"$TS_TAG\"]}}}}" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["key"], end="")'
}

# --- preflight ----------------------------------------------------------------

say "Checking the Swarm ($DOCKER_HOST)"
docker info --format '{{.Swarm.ControlAvailable}}' 2>/dev/null | grep -q true ||
    die "cannot reach a Swarm manager at DOCKER_HOST=$DOCKER_HOST"
labelled="$(docker node ls -q | xargs docker node inspect --format '{{.Description.Hostname}} {{index .Spec.Labels "budgeting-db"}}' | awk '$2=="true"{print $1}')"
[ -n "$labelled" ] ||
    die "no node is labelled budgeting-db=true. Label the node holding the budgeting-db volume: docker node update --label-add budgeting-db=true <node>"
note "budgeting-db node: $labelled"

# --- 1. overlay network -------------------------------------------------------

say "Overlay network '$NETWORK'"
if network_exists "$NETWORK"; then
    note "exists"
else
    docker network create --driver overlay --attachable "$NETWORK" >/dev/null
    note "created"
fi
NETWORK_ID="$(docker network inspect "$NETWORK" --format '{{.Id}}')"

# --- 2. secrets (created only if missing) --------------------------------------

say "Secrets"
if [ "$ROTATE_GITHUB" = 1 ]; then
    if service_exists "$GATEWAY_SVC"; then
        note "removing $GATEWAY_SVC (in-use secrets can't be removed)"
        docker service rm "$GATEWAY_SVC" >/dev/null
    fi
    for s in "$SECRET_CLIENT_ID" "$SECRET_CLIENT_SECRET"; do
        secret_exists "$s" && docker secret rm "$s" >/dev/null && note "removed $s"
    done
fi

if ! secret_exists "$SECRET_CLIENT_ID" || ! secret_exists "$SECRET_CLIENT_SECRET"; then
    # Ask for both before creating either, so a half-finished run can't leave one behind.
    client_id="$(ask BUDGETING_MCP_GITHUB_CLIENT_ID "GitHub OAuth Client ID" plain github_app_instructions)"
    client_secret="$(ask BUDGETING_MCP_GITHUB_CLIENT_SECRET "GitHub OAuth Client secret" secret github_app_instructions)"
    secret_exists "$SECRET_CLIENT_ID" || create_secret "$SECRET_CLIENT_ID" "$client_id"
    secret_exists "$SECRET_CLIENT_SECRET" || create_secret "$SECRET_CLIENT_SECRET" "$client_secret"
else
    note "GitHub client id/secret present"
fi

if secret_exists "$SECRET_JWT_KEY"; then
    note "JWT signing key present"
else
    create_secret "$SECRET_JWT_KEY" "$(openssl rand -hex 32)"
fi

if secret_exists "$SECRET_TS_AUTHKEY"; then
    note "Tailscale auth key present"
else
    if [ -n "${BUDGETING_MCP_TS_AUTHKEY:-}" ]; then
        authkey="$BUDGETING_MCP_TS_AUTHKEY"
    elif [ -n "${TS_API_KEY:-}" ]; then
        note "creating a pre-authorized $TS_TAG auth key via the Tailscale API"
        authkey="$(tailscale_create_authkey)" ||
            { tailscale_instructions; die "Tailscale API call failed (does the policy define $TS_TAG in tagOwners, and does TS_API_KEY have auth_keys write?)"; }
    else
        authkey="$(ask BUDGETING_MCP_TS_AUTHKEY "Tailscale auth key (tskey-auth-...)" secret tailscale_instructions)"
    fi
    create_secret "$SECRET_TS_AUTHKEY" "$authkey"
fi

# --- 3. budgeting-mcp (backend) ------------------------------------------------

say "Service $BACKEND_SVC"
published_ports="$(docker service inspect "$BACKEND_SVC" --format '{{range .Endpoint.Ports}}{{.PublishedPort}} {{end}}' 2>/dev/null || true)"
if ! service_exists "$BACKEND_SVC"; then
    publish=()
    [ "$LAN_PORT" = none ] || publish=(--publish "published=$LAN_PORT,target=$BACKEND_PORT")
    docker service create \
        --name "$BACKEND_SVC" \
        --network "$NETWORK" \
        ${publish[@]+"${publish[@]}"} \
        --constraint "$PLACEMENT" \
        --mount type=volume,source=budgeting-db,target=/data \
        --mount type=volume,source=uv-cache,target=/root/.cache/uv \
        --env BUDGETING_DB_PATH=/data/budgeting.db \
        --env MCP_TRANSPORT=streamable-http \
        --restart-condition any \
        "$BACKEND_IMAGE" \
        uv run --with git+https://github.com/rorymcstay/cashflow-forecaster.git python -m app.mcp_server >/dev/null
    note "created"
else
    update=()
    if ! docker service inspect "$BACKEND_SVC" --format '{{json .Spec.TaskTemplate.Networks}}' | grep -q "$NETWORK_ID"; then
        update+=(--network-add "$NETWORK")
    fi
    if [ "$LAN_PORT" = none ]; then
        [ -z "${published_ports// /}" ] || update+=(--publish-rm "$BACKEND_PORT")
    else
        case " $published_ports " in *" $LAN_PORT "*) ;; *) update+=(--publish-add "published=$LAN_PORT,target=$BACKEND_PORT") ;; esac
    fi
    docker service inspect "$BACKEND_SVC" --format '{{json .Spec.TaskTemplate.Placement.Constraints}}' | grep -q "$PLACEMENT" ||
        update+=(--constraint-add "$PLACEMENT")
    if [ "${#update[@]}" -gt 0 ]; then
        docker service update "${update[@]}" "$BACKEND_SVC" >/dev/null
        note "updated (${update[*]})"
    else
        note "up to date"
    fi
fi
if [ "$LAN_PORT" != none ]; then
    note "WARNING: the backend is published on :$LAN_PORT with NO authentication (all swarm nodes, LAN/tailnet only)."
    note "         Set BUDGETING_MCP_LAN_PORT=none to unpublish it; the gateway does not need it."
fi

# --- 4. gateway image + service ------------------------------------------------

say "Building $GATEWAY_IMAGE (on the manager, pushed to the local registry)"
docker build --quiet --tag "$GATEWAY_IMAGE" "$GATEWAY_DIR" >/dev/null
docker push --quiet "$GATEWAY_IMAGE" >/dev/null
note "pushed"

BACKENDS_JSON="[{\"name\":\"budgeting\",\"url\":\"http://$BACKEND_SVC:$BACKEND_PORT/mcp\"}]"
gateway_env=(
    --env "MCP_GATEWAY_PUBLIC_URL=$PUBLIC_URL"
    --env "MCP_GATEWAY_BACKENDS=$BACKENDS_JSON"
    --env "MCP_GATEWAY_ALLOWED_USERS=$ALLOWED_USERS"
    --env "MCP_GATEWAY_GITHUB_CLIENT_ID_FILE=/run/secrets/github_client_id"
    --env "MCP_GATEWAY_GITHUB_CLIENT_SECRET_FILE=/run/secrets/github_client_secret"
    --env "MCP_GATEWAY_JWT_KEY_FILE=/run/secrets/jwt_key"
    --env "TS_HOSTNAME=$TS_HOSTNAME"
    --env "TS_TAGS=$TS_TAG"
)

say "Service $GATEWAY_SVC"
if ! service_exists "$GATEWAY_SVC"; then
    docker service create \
        --name "$GATEWAY_SVC" \
        --network "$NETWORK" \
        --replicas 1 \
        --constraint "$PLACEMENT" \
        --init \
        --mount "type=volume,source=$GATEWAY_VOLUME,target=/data" \
        --secret "source=$SECRET_CLIENT_ID,target=github_client_id" \
        --secret "source=$SECRET_CLIENT_SECRET,target=github_client_secret" \
        --secret "source=$SECRET_JWT_KEY,target=jwt_key" \
        --secret "source=$SECRET_TS_AUTHKEY,target=ts_authkey" \
        "${gateway_env[@]}" \
        --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:$GATEWAY_PORT/healthz', timeout=3)\"" \
        --health-interval 15s --health-timeout 5s --health-retries 3 --health-start-period 30s \
        --update-order stop-first \
        --restart-condition any \
        "$GATEWAY_IMAGE" >/dev/null
    note "created"
else
    # The tag is reused, so --force is what makes Swarm pick up the rebuilt image.
    docker service update --force --image "$GATEWAY_IMAGE" "${gateway_env[@]/--env/--env-add}" "$GATEWAY_SVC" >/dev/null
    note "updated and restarted on the new image"
fi

# --- 5. verification -----------------------------------------------------------

if [ "$VERIFY" = 0 ]; then
    say "Skipping verification (--skip-verify)"
else
    say "Verifying (the node joins the tailnet and Funnel provisions a cert; this can take a few minutes)"
    healthy=0
    for _ in $(seq 1 60); do
        if curl -fsS -m 5 "$PUBLIC_URL/healthz" >/dev/null 2>&1; then healthy=1; break; fi
        sleep 5
    done
    if [ "$healthy" = 0 ]; then
        docker service logs --tail 40 "$GATEWAY_SVC" >&2 || true
        cat >&2 <<EOF

$PUBLIC_URL/healthz never answered. Usual causes, in order:
  - the auth key was rejected/expired, or the tag is not in tagOwners  (see the logs above)
  - the policy lacks {"target": ["$TS_TAG"], "attr": ["funnel"]} in nodeAttrs
  - MagicDNS / HTTPS certificates are not enabled on the tailnet
  - the node (check https://login.tailscale.com/admin/machines) is named $TS_HOSTNAME-1 because an old
    machine of that name still exists - delete the stale one and re-run
EOF
        tailscale_instructions
        exit 1
    fi
    note "healthz ok"

    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST "$PUBLIC_URL/budgeting/mcp")"
    [ "$code" = 401 ] || die "unauthenticated POST to $PUBLIC_URL/budgeting/mcp returned $code, expected 401"
    note "unauthenticated MCP request -> 401"

    PUBLIC_URL="$PUBLIC_URL" python3 - <<'EOF' || die "OAuth discovery document is wrong"
import json, os, urllib.request
base = os.environ["PUBLIC_URL"]
doc = json.load(urllib.request.urlopen(f"{base}/.well-known/oauth-protected-resource/budgeting/mcp", timeout=10))
want = [f"{base}/budgeting"]
assert doc.get("authorization_servers") == want, f"authorization_servers={doc.get('authorization_servers')} want {want}"
EOF
    note "oauth-protected-resource -> authorization_servers [$PUBLIC_URL/budgeting]"

    # Unauthenticated client straight at the backend over the overlay network, as the gateway sees it.
    tools="$(docker run --rm --network "$NETWORK" --entrypoint python "$GATEWAY_IMAGE" -c '
import asyncio
from fastmcp.client import Client
async def main():
    async with Client("http://'"$BACKEND_SVC:$BACKEND_PORT"'/mcp") as c:
        print(len(await c.list_tools()))
asyncio.run(main())
')" || die "could not list tools from http://$BACKEND_SVC:$BACKEND_PORT/mcp over the overlay network"
    [ "${tools:-0}" -gt 0 ] || die "backend reports 0 tools (Host-header/DNS-rebinding rejection?)"
    note "backend lists $tools tools over the overlay network"
fi

cat <<EOF

Done. Connector URL:

    $PUBLIC_URL/budgeting/mcp

In the Claude app: Settings -> Connectors -> Add custom connector, paste the URL
above, leave the OAuth client fields empty, then sign in with GitHub as one of:
$ALLOWED_USERS
EOF
