# Single stage, slim base. 3.14 rather than 3.12 because cronometer-api-mcp's
# client.py uses PEP 758 unparenthesized `except A, B:`, which is a SyntaxError
# on 3.13 and below. See the README.
FROM python:3.14-slim

# Hungarian text end to end: UTF-8 locale in the container, not just in Python.
# tzdata is installed at the OS level so the TZ env var resolves to a real zone
# — diary entries are stamped in the account timezone and the date we write has
# to be the date we meant.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.12.6

WORKDIR /srv

# Dependencies first, so editing app code does not re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Non-root. The uid is fixed so the named volume's ownership is stable across
# rebuilds.
RUN useradd --create-home --uid 10001 cronohelper \
    && mkdir -p /data \
    && chown -R cronohelper:cronohelper /data /srv

USER cronohelper

ENV PATH="/srv/.venv/bin:$PATH"

EXPOSE 8080

# Uses the app's own health endpoint, which deliberately does not touch
# Cronometer — a third party being down must not mark this container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
