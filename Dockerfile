FROM python:3.11-slim

# Install system dependencies: git for repo operations, build-essential for native extensions
RUN apt-get update && \
    apt-get install -y --no-install-recommends git build-essential && \
    rm -rf /var/lib/apt/lists/*

# Fix for "dubious ownership" error in mounted volumes
RUN git config --global --add safe.directory '*'

WORKDIR /app

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Copy project metadata and source code
COPY pyproject.toml ./
COPY mimir/ ./mimir/

# Install application and all dependencies
RUN pip install --no-cache-dir .

# Pre-download the standard embedding model
# This caches model weights into the Docker layer so the container runs 100% offline
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-mpnet-base-v2')"

# Remove build tools to shrink final image
RUN apt-get purge -y build-essential && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

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
