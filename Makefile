.PHONY: help install dev test lint format clean run docker-build docker-up docker-down

help:
	@echo "Nexus Development Commands"
	@echo "=========================="
	@echo "make install     - Install dependencies with uv"
	@echo "make dev         - Install development dependencies"
	@echo "make test        - Run tests"
	@echo "make lint        - Run linters"
	@echo "make format      - Format code"
	@echo "make clean       - Clean build artifacts"
	@echo "make run         - Run server in development mode"
	@echo "make docker-build - Build Docker image"
	@echo "make docker-up   - Start Docker containers"
	@echo "make docker-down - Stop Docker containers"

install:
	uv venv
	uv pip install -e .

dev:
	uv pip install -e ".[dev,test]"

test:
	pytest -v

test-cov:
	pytest --cov=nexus --cov-report=html --cov-report=term

lint:
	ruff check .
	mypy src/nexus

format:
	ruff format .
	ruff check --fix .

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

run:
	uvicorn nexus.api.main:app --reload --host 0.0.0.0 --port 8080

docker-build:
	docker build -t nexus:latest .

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

init:
	uv venv
	uv pip install -e ".[dev,test]"
	@echo "\n✓ Setup complete!"
	@echo "Activate virtual environment with: source .venv/bin/activate"
