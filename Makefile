.PHONY: help build up down test test-router test-operator test-all smoke-load lint clean

ROUTER_DIR  := router
OPERATOR_DIR := operator
COMPOSE     := docker compose

help:
	@echo "KubeServe — available targets:"
	@echo "  build          Build all Docker images"
	@echo "  up             Start all services (router + backends + redis)"
	@echo "  up-monitoring  Start with Prometheus + Grafana"
	@echo "  down           Stop all services"
	@echo "  test           Run all unit tests (router + operator)"
	@echo "  test-router    Run router unit tests"
	@echo "  test-operator  Run operator unit tests"
	@echo "  smoke          Run k6 smoke test against running stack"
	@echo "  load           Run k6 full load test"
	@echo "  lint           Run linters"
	@echo "  clean          Remove containers and test artifacts"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d redis router backend-claude backend-gpt4 backend-gemini
	@echo "Router available at http://localhost:8080"
	@echo "Health check: curl http://localhost:8080/health"

up-monitoring:
	$(COMPOSE) --profile monitoring up -d
	@echo "Prometheus: http://localhost:9090"
	@echo "Grafana:    http://localhost:3000"

down:
	$(COMPOSE) down

test-router:
	cd $(ROUTER_DIR) && /Users/apple/Desktop/Projects/KubeServe/.venv/bin/pip install -q -r requirements-test.txt && \
	/Users/apple/Desktop/Projects/KubeServe/.venv/bin/pytest tests/ -v --tb=short

test-operator:
	cd $(OPERATOR_DIR) && /Users/apple/Desktop/Projects/KubeServe/.venv/bin/pip install -q -r requirements-test.txt && \
	/Users/apple/Desktop/Projects/KubeServe/.venv/bin/pytest tests/ -v --tb=short

test: test-router test-operator

smoke:
	k6 run --env ROUTER_URL=http://localhost:8080 k6/smoke-test.js

load:
	k6 run --env ROUTER_URL=http://localhost:8080 k6/load-test.js

lint:
	cd $(ROUTER_DIR) && python -m py_compile main.py circuit_breaker.py load_balancer.py metrics.py
	cd $(OPERATOR_DIR) && python -m py_compile main.py
	cd mock-backend && python -m py_compile main.py

clean:
	$(COMPOSE) down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
