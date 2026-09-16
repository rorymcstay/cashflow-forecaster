docker service create \
  --name budgeting \
  --publish published=8085,target=8084 \
  --mount type=volume,source=budgeting-db,target=/data \
  --mount type=volume,source=uv-cache,target=/root/.cache/uv --env BUDGETING_DB_PATH=/data/budgeting.db \
  ghcr.io/astral-sh/uv:python3.13-bookworm \
  uv run --with git+https://github.com/rorymcstay/cashflow-forecaster.git python -m app.web.main
