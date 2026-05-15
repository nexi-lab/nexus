.PHONY: dev build test deploy lint

# Fast local development with incremental rebuilds
dev:
	mkdocs serve --dirty

# Production build with strict validation
build:
	mkdocs build --strict

# Run all doc quality checks
test:
	mkdocs build --strict
	pytest tests/test_docs.py -v
	typos docs/

# Lint code examples only (fast)
lint:
	pytest tests/test_docs.py -v --co

# Deploy versioned docs via mike
deploy:
	mike deploy --push --update-aliases $(VERSION) latest
	mike set-default --push latest
