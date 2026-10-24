# Gateway + Tailscale in one image, so the container is its own tailnet node
# and Funnel can target 127.0.0.1 (Swarm has no `network_mode: service:`).
FROM tailscale/tailscale:v1.90.9 AS tailscale

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY gateway.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# /data is a mounted volume holding the tailscale node state (tailscale/) and
# the client registrations/tokens (fastmcp/), so a restart neither re-joins
# the tailnet nor signs Claude out. Runs as root because Swarm creates named
# volumes root-owned; userspace networking needs no extra capabilities.
ENV FASTMCP_HOME=/data/fastmcp \
    MCP_GATEWAY_PORT=8770
WORKDIR /app

EXPOSE 8770
ENTRYPOINT ["/app/entrypoint.sh"]
