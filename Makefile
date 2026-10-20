SHELL=/bin/bash
.DEFAULT_GOAL := _help

MAKEFLAGS += --no-print-directory

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Format and lint
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


.PHONY: format
format:	##H Format code
	prettier --prose-wrap always --print-width 80 --write $$(git ls-files '*.md' '*.y*ml' '*.json')
	black $$(git ls-files '*.py')
	isort $$(git ls-files '*.py')
	ruff format $$(git ls-files '*.py')
	ruff check --fix $$(git ls-files '*.py')
	pre-commit run --all-files


.PHONY: lint
lint:	##H Run python lints
	markdownlint $$(git ls-files '*.md')
	ruff check $$(git ls-files '*.py')
	mypy $$(git ls-files '*.py')
	flake8 --max-line-length 88 $$(git ls-files '*.py')
	pylint $$(git ls-files '*.py')


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Help, clean & extras
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.PHONY: _help
_help:
	@grep -E '^[a-zA-Z_/%-]+:.*?##H' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?##H "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'


.PHONY: clean
clean:	##H Clean build artifacts
	find . -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache | xargs -r rm -rf

.PHONY: extras/cloc
extras/cloc:	##H Count lines of code
	cloc --git HEAD
