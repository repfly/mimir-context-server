# syntax=docker/dockerfile:1
FROM python:3.13-slim

# git for repo operations (indexing, temporal signals, diffs). Every Python
# dependency ships wheels, so no compiler toolchain is needed.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Fix for "dubious ownership" error in mounted volumes
RUN git config --global --add safe.directory '*'

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Third-party dependencies only, read from pyproject.toml. This layer (and the
# model download below) is reused across source-code changes.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt && \
    pip install -r /tmp/requirements.txt

# Pre-download the standard embedding model (ONNX export) so the container
# runs 100% offline. File list matches _MODEL_FILES in mimir/infra/embedders/local.py.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('sentence-transformers/all-mpnet-base-v2', allow_patterns=['onnx/model.onnx', 'onnx/model.onnx_data', 'tokenizer.json', 'config.json', 'modules.json', 'sentence_bert_config.json', '1_Pooling/config.json'])"

# The build context has no .git, so setuptools-scm cannot derive the version.
# Pass it explicitly: docker build --build-arg MIMIR_VERSION=1.2.0 .
ARG MIMIR_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MIMIR_CONTEXT_SERVER=${MIMIR_VERSION}

# Application code last — only this layer rebuilds on a code change
COPY README.md ./
COPY mimir/ ./mimir/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

# Offline hugging face mode
ENV HF_HUB_OFFLINE=1
ENV PYTHONPATH=/app

# Mount points: /project for source repos, /data for persistent index
VOLUME ["/project", "/data"]
WORKDIR /project

# Expose the HTTP server port
EXPOSE 8421

# Health check for orchestrators (K8s, ECS, Docker Compose)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8421/api/v1/health')" || exit 1

# Entrypoint handles index-then-serve workflow
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["auto"]
