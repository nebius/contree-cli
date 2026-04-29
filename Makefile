.PHONY: lint types check tests docs docs-clean

all: check tests

lint:
	uv run ruff format
	uv run ruff check --fix

types:
	uv run mypy contree_cli

check: lint types

tests: check
	uv run pytest --cov=contree_cli --cov-report=term-missing

docs:
	$(MAKE) -C docs html

docs-clean:
	$(MAKE) -C docs clean
