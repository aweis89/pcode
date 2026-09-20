.DEFAULT_GOAL := help
.PHONY: help install update uninstall run test test-tmux test-all lint fmt cache-report harness-src worktree worktree-merge worktree-remove worktrees brew-install brew-update brew-uninstall

HARNESS_DIR := tmp/pydantic-ai-harness
HARNESS_URL := https://github.com/pydantic/pydantic-ai-harness.git

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install `pcode` on PATH (editable: source edits are live)
	uv tool install --editable . --reinstall

update: ## Rebuild the tool env after dependency changes
	uv tool install --editable . --reinstall

uninstall: ## Remove the `pcode` command
	uv tool uninstall pcode

run: ## Run from source without installing (make run ARGS="--theme-preview")
	uv run pcode $(ARGS)

test: ## Run the fast suite in parallel (real-tmux regressions skipped)
	uv run pytest -n auto

test-tmux: ## Run only the real-tmux regressions (serial: they time out under load)
	uv run pytest --tmux -m tmux

test-all: test test-tmux ## Run everything: fast suite in parallel, then tmux serially

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply formatting and autofixes
	uv run ruff check --fix .
	uv run ruff format .

cache-report: ## Report prompt-cache behavior from saved sessions (SESSION=latest|all|<id>)
	uv run python scripts/cache_report.py $(or $(SESSION),latest) $(ARGS)

harness-src: ## Check out upstream Harness source at the pinned SHA under tmp/
	@sha=$$(sed -n 's/.*pydantic-ai-harness\.git@\([0-9a-f]\{40\}\).*/\1/p' pyproject.toml); \
	if [ -z "$$sha" ]; then echo 'no pinned Harness SHA in pyproject.toml' >&2; exit 1; fi; \
	if [ ! -d $(HARNESS_DIR)/.git ]; then git clone --quiet $(HARNESS_URL) $(HARNESS_DIR); fi; \
	git -C $(HARNESS_DIR) cat-file -e "$$sha^{commit}" 2>/dev/null || git -C $(HARNESS_DIR) fetch --quiet origin; \
	git -C $(HARNESS_DIR) checkout --quiet --detach "$$sha"; \
	echo "$(HARNESS_DIR) @ $$sha"

worktree: ## Create an isolated worktree under .worktrees/ (make worktree NAME=fix-foo [BASE=ref]); `pcode --worktree` does this per session
	@uv run python -m pcode.worktree new $(NAME) $(if $(BASE),--base $(BASE))

worktree-merge: ## Merge a worktree's branch back into the mainline (make worktree-merge NAME=fix-foo)
	@uv run python -m pcode.worktree merge $(NAME)

worktree-remove: ## Delete a worktree, keeping its branch (make worktree-remove NAME=fix-foo)
	@uv run python -m pcode.worktree remove $(NAME)

worktrees: ## List worktrees
	@uv run python -m pcode.worktree list

brew-install: ## Alternative: install the frozen HEAD build via Homebrew
	brew tap aweis89/pcode https://github.com/aweis89/pcode.git
	brew install --HEAD aweis89/pcode/pcode

brew-update: ## Upgrade the Homebrew HEAD build
	brew update
	brew upgrade --fetch-HEAD aweis89/pcode/pcode

brew-uninstall: ## Remove the Homebrew build and tap
	brew uninstall pcode
	brew untap aweis89/pcode
