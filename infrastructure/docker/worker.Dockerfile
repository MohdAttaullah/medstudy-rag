# Ingestion worker image: parsing, chunking and embedding.
#
# Only this image carries the model runtimes. The API and the outbox dispatcher never parse or
# embed, so they stay on the lean backend image and are not enlarged by torch, ONNX Runtime, the
# Docling model stack or the MedCPT encoder.
#
# Two separate model caches, because they are provisioned differently:
#
#   /home/medrag/.cache/huggingface  parser weights, downloaded on first use (parser-models volume)
#   /home/medrag/models/embeddings   the pinned MedCPT revision (embedding-models volume)
#
# The embedding cache is provisioned deliberately by scripts/provision_embedding_model.py and the
# worker then runs with MEDRAG_EMBEDDING__OFFLINE=true, so no user request ever depends on a
# runtime download from an external model host. Both directories are created in the image so the
# named volumes inherit their ownership on first mount.
FROM python:3.12-slim
WORKDIR /app
# The OCR engine loads OpenCV, whose wheel links against these X/GL runtime libraries. They are
# absent from the slim base image, and without them OCR fails at model initialisation.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY backend ./backend
COPY workers ./workers
COPY migrations ./migrations
COPY alembic.ini ./
COPY infrastructure/monitoring/logging.json ./logging.json
RUN pip install --no-cache-dir uv==0.10.9 \
    && uv sync --frozen --no-dev --extra parsing --extra embedding \
    && useradd --uid 10001 --create-home medrag \
    && mkdir -p /home/medrag/.cache/huggingface /home/medrag/models/embeddings /home/medrag/models/reranking \
    && chown -R 10001:10001 /home/medrag
# Thread count is pinned by the parser configuration, not by the environment, so that the same
# document produces the same layout prediction here and on a developer machine.
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/home/medrag/.cache/huggingface \
    MEDRAG_EMBEDDING__MODEL_CACHE_DIR=/home/medrag/models/embeddings
USER 10001
CMD ["celery", "-A", "workers.bootstrap:app", "worker", \
     "--loglevel=WARNING", "--queues=ingestion", "--concurrency=1"]
