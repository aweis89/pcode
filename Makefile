.DEFAULT_GOAL := help
.PHONY: help install update uninstall run test lint fmt brew-install brew-update brew-uninstall

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install `pcode` on PATH (editable: source edits are live)
	uv tool install --editable . --reinstall

update: ## Rebuild the tool env after dependency changes
	uv tool install --editable . --reinstall

uninstall: ## Remove the `pcode` command
	uv tool uninstall pcode

run: ## Run from source without installing (make run ARGS="--demo")
	uv run pcode $(ARGS)

test: ## Run the test suite
	uv run pytest

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply formatting and autofixes
	uv run ruff check --fix .
	uv run ruff format .

brew-install: ## Alternative: install the frozen HEAD build via Homebrew
	brew tap aweis89/pcode https://github.com/aweis89/pcode.git
	brew install --HEAD aweis89/pcode/pcode

brew-update: ## Upgrade the Homebrew HEAD build
	brew update
	brew upgrade --fetch-HEAD aweis89/pcode/pcode

brew-uninstall: ## Remove the Homebrew build and tap
	brew uninstall pcode
	brew untap aweis89/pcode
