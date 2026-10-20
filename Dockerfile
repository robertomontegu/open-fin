FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/openfin

WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 openfin \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/openfin openfin \
    && install -d -m 0700 -o 10001 -g 10001 /data \
    && chmod -R a+rX /ms-playwright

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY third_party ./third_party
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps .

FROM base AS test
COPY tests ./tests
COPY scripts ./scripts
COPY docs ./docs
USER 10001:10001
CMD ["sh", "-c", "python -m ruff check --no-cache src tests scripts && python -m pytest -p no:cacheprovider && PYTHONPYCACHEPREFIX=/tmp/open-fin-pycache python -m compileall -q src scripts"]

FROM base AS runtime
ENV OPEN_FIN_DATABASE_PATH=/data/open-fin.db \
    OPEN_FIN_MODE=demo \
    OPEN_FIN_HEADLESS=true \
    OPEN_FIN_SOURCE_URL=https://github.com/robertomontegu/open-fin
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "open_fin.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
