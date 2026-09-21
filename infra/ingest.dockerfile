# Same entrypoint as native dev: python -m argus.ingest --config ...
# GPU access comes from the NVIDIA Container Toolkit at run time.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --all-packages --group staging --no-dev

COPY config ./config
# The registry is the map from artefact name -> sha256 and licence; without it
# `models_root()` raises and every ONNX backend refuses to construct. The
# binaries themselves are never in the image (ADR-0026) -- they are mounted.
COPY models/registry.yaml ./models/registry.yaml

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "argus.ingest"]
