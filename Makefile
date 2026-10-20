PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: setup up down logs test lint check build docker-test

setup:
	$(PYTHON) scripts/init-env.py

up:
	$(COMPOSE) up --build -d --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs --tail=100 -f

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests scripts

check: lint test
	$(PYTHON) -m compileall -q src scripts

build:
	docker build --target runtime -t open-fin:local .

docker-test:
	docker build --target test -t open-fin:test .
	docker run --rm --init --network none open-fin:test
