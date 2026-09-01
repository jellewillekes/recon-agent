SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules

UV ?= uv
UV_RUN := $(UV) run

RUFF := $(UV_RUN) ruff
MYPY := $(UV_RUN) mypy
PYTEST := $(UV_RUN) pytest
PRECOMMIT := $(UV_RUN) pre-commit

MYPY_CONFIG ?= mypy.ini
MYPY_PATHS ?= src tests
PYTEST_ARGS ?= -q -m "not llm"

.PHONY: help
help:
	@echo ""
	@echo "recon-agent"
	@echo ""
	@echo "  make check          format-check + lint + type + test"
	@echo "  make format         ruff format --check"
	@echo "  make lint           ruff check"
	@echo "  make type           mypy"
	@echo "  make test           pytest, excluding the llm marker"
	@echo "  make fix            format + safe autofix"
	@echo "  make precommit      run all pre-commit hooks"
	@echo "  make install-hooks  install git hooks"
	@echo "  make clean          remove local caches"
	@echo ""

.PHONY: check format lint type test fix
check: format lint type test
	@echo "All checks passed"

format:
	@$(RUFF) format --check .

lint:
	@$(RUFF) check .

type:
	@$(MYPY) --config-file $(MYPY_CONFIG) $(MYPY_PATHS)

test:
	@$(PYTEST) $(PYTEST_ARGS)

fix:
	@$(RUFF) format .
	@$(RUFF) check --fix .

.PHONY: precommit install-hooks
precommit:
	@$(PRECOMMIT) run --all-files

install-hooks:
	@$(PRECOMMIT) install

.PHONY: clean
clean:
	@rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__ || true
