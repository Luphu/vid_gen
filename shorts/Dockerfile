# syntax=docker/dockerfile:1.7
# Multi-arch Debian base; built natively for linux/arm64 (Raspberry Pi 5) in CI.
# Debian ffmpeg: software libx264/aac/drawtext. No hardware encoder is assumed (Pi 5 has none for H.264).
ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

FROM base AS test
RUN pip install ".[dev]"
COPY tests ./tests
COPY examples ./examples
CMD ["sh", "-c", "ruff check src tests && pytest -q"]

FROM base AS runtime
RUN useradd --uid 10001 --create-home --home-dir /home/app app \
 && rm -rf /app/src /app/pyproject.toml
USER 10001
WORKDIR /work
ENV HOME=/home/app WORK_DIR=/work/tmp APP_ENV=prod
ENTRYPOINT ["shorts"]
CMD ["--help"]
