# Same entrypoint as native dev: python -m argus.ingest --config ...
# GPU access comes from the NVIDIA Container Toolkit at run time.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# The ORT 1.30 wheel is built for CUDA 13.0 + cuDNN 9. The NVIDIA container
# runtime supplies the driver, but it does not supply these user-space libraries.
# Keep the compiler/toolkit out; the application only needs the runtime pieces.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /tmp/cuda-keyring.deb \
       https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends cuda-libraries-13-0 libcudnn9-cuda-13 \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/* /tmp/cuda-keyring.deb

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
