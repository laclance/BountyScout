PYTHON ?= python

.PHONY: setup format lint typecheck test map map-check quality

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

format:
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m coverage run --branch -m unittest -v
	$(PYTHON) -m coverage report

map:
	$(PYTHON) scripts/generate_codebase_map.py

map-check:
	$(PYTHON) scripts/generate_codebase_map.py --check

quality: lint map-check typecheck test
	$(PYTHON) -m ruff format --check .
