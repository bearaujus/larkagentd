FROM node:24-bookworm-slim AS node_runtime
FROM node_runtime AS dashboard
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM node_runtime AS lark_cli
ARG LARK_CLI_VERSION=1.0.99
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN npm install --prefix /opt/lark-cli --omit=dev --no-audit --no-fund "@larksuite/cli@$LARK_CLI_VERSION"

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=lark_cli /opt/lark-cli /opt/lark-cli
RUN ln -s /opt/lark-cli/node_modules/.bin/lark-cli /usr/local/bin/lark-cli
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/configure_hermes.py ./scripts/configure_hermes.py
COPY deploy/hermes/config.yaml ./deploy/hermes/config.yaml
COPY --from=dashboard /build/src/larkagentd/gateway/static ./src/larkagentd/gateway/static
RUN pip install --disable-pip-version-check --no-cache-dir '.[mcp,group-brain]' \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 LOG_FORMAT=json \
    LARK_CLI_BIN=/usr/local/bin/lark-cli TOOL_SERVICE_HOST=0.0.0.0
USER app
EXPOSE 8080 8081
CMD ["larkagentd"]
