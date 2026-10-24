# Runs the budgeting MCP server (app/mcp_server.py) over streamable HTTP,
# alongside the web UI from service.sh. Shares the same budgeting-db volume,
# so both services read and write the same SQLite file. A local volume lives
# on one node only, so both services are pinned to the node labelled
# budgeting-db=true — label the node that already holds the data:
#   docker node update --label-add budgeting-db=true <node>
# Run from the swarm manager (nuc-05).
# Clients connect to http://<host>:8769/mcp
docker service create \
  --name budgeting-mcp \
  --publish published=8769,target=8000 \
  --constraint node.labels.budgeting-db==true \
  --mount type=volume,source=budgeting-db,target=/data \
  --mount type=volume,source=uv-cache,target=/root/.cache/uv --env BUDGETING_DB_PATH=/data/budgeting.db \
  --env MCP_TRANSPORT=streamable-http \
  --restart-condition "any" \
  ghcr.io/astral-sh/uv:python3.13-bookworm \
  uv run --with git+https://github.com/rorymcstay/cashflow-forecaster.git python -m app.mcp_server
