# loupe-web: the full Loupe web app, one folder per account on the /data volume.
FROM python:3.12.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md requirements-web.txt ./
# pyproject hard-depends on macOS-only packages, so the project installs with --no-deps
# and its Linux dependencies come from requirements-web.txt, constrained by uv.lock.
RUN uv export --frozen --no-hashes --no-emit-project --no-dev --extra secondpass > /tmp/constraints.txt \
    && uv pip install --system --no-cache \
        --index-strategy unsafe-best-match \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --constraint /tmp/constraints.txt \
        --requirement requirements-web.txt
COPY src ./src
RUN uv pip install --system --no-cache --no-deps .

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/data/hf \
    LOUPE_WEB_ROOT=/data
CMD ["python", "-m", "loupe.hosted"]
