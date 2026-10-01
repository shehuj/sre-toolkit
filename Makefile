# No virtualenv, no install step, no dependencies: the core package is stdlib-only,
# so every target below runs against the source tree as-is.
PY ?= python3
export PYTHONPATH := src:.

.PHONY: help test demo lint fixture install install-aws install-all clean cost

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

test: ## Run the test suite (no dependencies required)
	$(PY) -m unittest discover -s tests -v

demo: ## Full investigation on the bundled incident — costs nothing
	-$(PY) -m sre_toolkit --demo incident investigate

cost: ## Show the price table the toolkit meters against
	$(PY) -m sre_toolkit cost prices

fixture: ## Regenerate the demo incident fixture
	$(PY) scripts/make_demo_fixture.py

lint: ## Lint with ruff (same pinned version CI uses)
	@command -v ruff >/dev/null 2>&1 && ruff check src tests scripts || \
		echo "ruff not installed — run: pip install -e '.[dev]'  (pins ruff==0.5.7, as CI does)"

install: ## Install the CLI (core only, zero dependencies)
	$(PY) -m pip install -e .

install-aws: ## Install with boto3 for the AWS collectors
	$(PY) -m pip install -e '.[aws]'

install-all: ## Install every optional extra (AWS, Kubernetes, DNS, Bedrock)
	$(PY) -m pip install -e '.[all]'

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
