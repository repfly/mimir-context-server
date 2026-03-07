FROM python:3.11-slim

# Install system dependencies required for parsing and git operations
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Copy project metadata and source code
COPY pyproject.toml ./
COPY mimir/ ./mimir/

# Install application
RUN pip install --no-cache-dir .

# Pre-download the standard embedding model
# This caches model weights into the Docker layer so the container runs fast and 100% offline
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-mpnet-base-v2')"

# Fast and fully offline hugging face configuration
ENV HF_HUB_OFFLINE=1
ENV PYTHONPATH=/app

# Define standard mount path for codebases
VOLUME ["/project"]
WORKDIR /project

# Using mimir CLI as the default executable
ENTRYPOINT ["mimir"]
# Executing serve (MCP server) by default
CMD ["serve", "--config", "mimir.toml"]
