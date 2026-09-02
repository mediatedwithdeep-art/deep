# Sentinel — common tasks.
#
#   make demo      one command: build, start, seed, print the login
#   make dev       run the services on the host without Docker
#   make test      the full test suite
#   make down      stop everything (keeps data)
#   make clean     stop and delete all data

SHELL := /bin/bash
COMPOSE := docker compose
PY := python3

.DEFAULT_GOAL := help

## demo: build and start the whole system, then print the sign-in details
demo:
	@echo "==> building and starting Sentinel"
	$(COMPOSE) up -d --build
	@echo "==> waiting for the API to become healthy"
	@for i in $$(seq 1 60); do \
	   curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break; \
	   sleep 2; \
	 done
	@echo
	@$(COMPOSE) logs bootstrap 2>/dev/null | grep -A4 "Demo login" || true
	@echo
	@echo "  Command centre : http://localhost:3000"
	@echo "  API docs       : http://localhost:8000/docs"
	@echo "  Sign in as     : admin  (password above; set SEED_ADMIN_PASSWORD to choose)"
	@echo
	@echo "  Follow the pipeline:  make logs"

## up: start without rebuilding
up:
	$(COMPOSE) up -d

## down: stop the stack, keeping the database
down:
	$(COMPOSE) down

## clean: stop and delete all data (destructive)
clean:
	$(COMPOSE) down -v

## logs: follow the ingestion and processing pipeline
logs:
	$(COMPOSE) logs -f ingestion processor api

## ps: what is running
ps:
	$(COMPOSE) ps

## observability: add Prometheus and Grafana
observability:
	$(COMPOSE) --profile observability up -d
	@echo "  Prometheus : http://localhost:9090"
	@echo "  Grafana    : http://localhost:3001 (admin / $${GRAFANA_PASSWORD:-sentinel})"

## media: add the media server, for viewing real camera video
media:
	$(COMPOSE) --profile media up -d

## live: switch ingestion to real cameras from config/cameras.yaml
live:
	INGEST_MODE=live $(COMPOSE) up -d --force-recreate ingestion
	@echo "ingestion restarted in LIVE mode; edit config/cameras.yaml and re-run to change cameras"

## hybrid: real cameras plus the simulated estate (recommended at a venue)
hybrid:
	INGEST_MODE=hybrid $(COMPOSE) up -d --force-recreate ingestion

# ── host-native development ─────────────────────────────────────────

## install: make every service importable outside Docker (do this first)
install:
	$(PY) -m pip install -e .
	@echo
	@echo "  installed. 'sentinel-api' now starts the API from any directory."
	@echo "  for real camera ingestion, add PyAV:  pip install -e '.[live]'"

## install-live: as above, plus PyAV for real camera ingestion
install-live:
	$(PY) -m pip install -e ".[live]"

## dev-db: start only PostgreSQL and Redis, for host-native development
dev-db:
	$(COMPOSE) up -d postgres redis

## migrate: apply database migrations
migrate:
	$(PY) database/migrate.py up

## seed: load the demo estate
seed:
	$(PY) database/seeds/seed.py

## dev-api: run the API on the host with reload (needs `make install` first)
# No PYTHONPATH here on purpose. Setting it made this one target work while
# every other way of starting the API failed, which hid the fact that the
# project was never installed. `make install` fixes it for all of them.
dev-api:
	$(PY) -m app.main --reload --port 8000

## dev-ingestion: run ingestion on the host
dev-ingestion:
	$(PY) video-ingestion/service.py

## dev-processor: run the event processor on the host
dev-processor:
	$(PY) event-processor/service.py

## dev-web: run the frontend dev server
dev-web:
	cd frontend && npm install && npm run dev

# ── quality ─────────────────────────────────────────────────────────

## test: run the full test suite
test:
	$(PY) -m pytest tests/ -q

## verify: answer the ten Phase 2B verification questions by running the system
verify:
	$(PY) scripts/verify_p0.py

## test-unit: only the tests that need no database
test-unit:
	$(PY) -m pytest tests/ -q -m "not integration"

## typecheck: TypeScript
typecheck:
	cd frontend && npx tsc --noEmit

## benchmark: measure pipeline throughput and accuracy
benchmark:
	$(PY) scripts/benchmark.py

help:
	@echo "Sentinel — Gujarat Police Unified VMS"
	@echo
	@# awk rather than `column`, which is not installed on minimal images.
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //' | \
	  awk -F': ' '{ printf "  make %-16s %s\n", $$1, $$2 }'
	@echo

.PHONY: demo up down clean logs ps observability media live hybrid \
        install install-live \
        dev-db migrate seed dev-api dev-ingestion dev-processor dev-web \
        test test-unit typecheck benchmark help
