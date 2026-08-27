# Running outside Docker. Requires uv (https://astral.sh/uv).
# uv fetches its own CPython 3.14 -- no system Python needed.

DATA_DIR ?= ./data
PORT     ?= 8080

.PHONY: dev test spike up down logs fmt clean

## Run the app locally with reload, on http://127.0.0.1:$(PORT)
dev:
	DATA_DIR=$(DATA_DIR) uv run uvicorn app.main:app --reload --port $(PORT)

## Run the test suite. Never touches the real Cronometer API.
test:
	uv run pytest -q

## Food-resolution spike. Needs CRONOMETER_USERNAME/PASSWORD in .env.
## Pass the exact name of a custom food you created by hand in the UI:
##   make spike FOOD="Teszt Halaszle Spike 42"
spike:
	uv run python spike_food_search.py "$(FOOD)"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	rm -rf .venv .pytest_cache data
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
