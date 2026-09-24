# Build stage: uv installs its own Python and all dependencies into /app/.venv.
# Debian rather than Alpine: rasterio, xgboost, pyogrio, polars and scikit-learn
# have no musl (Alpine) wheels and would have to be compiled from source.
FROM ghcr.io/astral-sh/uv:bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed

RUN uv python install 3.12

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: uv's Python and the finished venv only, no uv or build tools.
FROM debian:bookworm-slim

# xgboost needs OpenMP, rasterio needs expat
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /python /python
COPY --from=build /app/.venv /app/.venv

WORKDIR /app
ENV SOPPKART_DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "soppkart.api:app", "--host", "0.0.0.0", "--port", "8000"]
