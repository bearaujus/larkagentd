PYTHON ?= .venv/bin/python
VENV ?= .venv
ENV_FILE ?= .env
LOCAL_DATABASE ?= true
COMPOSE ?= $(shell if docker compose version >/dev/null 2>&1; then echo 'docker compose'; else echo 'docker-compose'; fi)
COMPOSE_PROJECT ?= larkagentd
APP = $(COMPOSE) -p "$(COMPOSE_PROJECT)" --env-file "$(ENV_FILE)" -f compose.yaml
DB = $(COMPOSE) -p "$(COMPOSE_PROJECT)" $(if $(wildcard $(ENV_FILE)),--env-file "$(ENV_FILE)") -f compose.postgres.yaml

.PHONY: setup env build run drain stop status logs spawn-db stop-db migrate test lint check ui-build ui-check ui-test

env:
	@test -e .env || (umask 077 && cp .env.example .env)

setup: env
	python3 -m venv "$(VENV)"
	"$(PYTHON)" -m pip install -e '.[dev,mcp]'

build:
	$(APP) build gateway toolservice hermes-config migrate

spawn-db:
	$(DB) up -d --wait postgres
	$(DB) exec -T postgres sh -ec 'exec psql -X -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' \
		< deploy/postgres/pgvector.sql

stop-db:
	$(DB) stop postgres

migrate:
	$(APP) run --rm --no-deps migrate

run: build
	@if [ "$(LOCAL_DATABASE)" = true ]; then $(MAKE) spawn-db; fi
	$(MAKE) drain
	$(APP) stop gateway toolservice hermes
	$(MAKE) migrate
	$(APP) up -d --force-recreate --wait gateway toolservice hermes

drain:
	@running="$$($(APP) ps --status running -q gateway)" && \
		if [ -n "$$running" ]; then $(APP) exec -T gateway python - < "src/larkagentd/gateway/drain.py"; fi

stop:
	$(APP) stop gateway toolservice hermes

status:
	$(APP) ps

logs:
	$(APP) logs -f $(SERVICE)

lint:
	"$(PYTHON)" -m ruff check src tests scripts migrations hermes_plugin
	"$(PYTHON)" -m ruff format --check src tests scripts migrations hermes_plugin

test:
	@test -n "$$TEST_DATABASE_URL" || { echo "Set TEST_DATABASE_URL to a disposable PostgreSQL database."; exit 1; }
	"$(PYTHON)" -m pytest -q

check: lint test

ui-build:
	npm --prefix frontend run build

ui-check:
	npm --prefix frontend run check

ui-test:
	npm --prefix frontend test
