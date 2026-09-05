# Parsing worker image.
#
# Only this image carries the parser and its model runtime. The API and the outbox dispatcher
# never parse, so they stay on the lean backend image and are not slowed down or enlarged by
# torch, ONNX Runtime and the Docling model stack.
#
# Model weights are downloaded on first use and cached under /home/medrag/.cache, which compose
# backs with a named volume so the download happens once rather than per container lifetime. The
# cache directory is created in the image so the volume inherits its ownership on first mount.
# The first parse therefore needs outbound network access to the model host.
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
    && uv sync --frozen --no-dev --extra parsing \
    && useradd --uid 10001 --create-home medrag \
    && mkdir -p /home/medrag/.cache/huggingface \
    && chown -R 10001:10001 /home/medrag
# Thread count is pinned by the parser configuration, not by the environment, so that the same
# document produces the same layout prediction here and on a developer machine.
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/home/medrag/.cache/huggingface
USER 10001
CMD ["celery", "-A", "workers.bootstrap:app", "worker", \
     "--loglevel=WARNING", "--queues=ingestion", "--concurrency=1"]
