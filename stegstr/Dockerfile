# Stegstr — API + web UI
FROM python:3.13-slim

WORKDIR /app

# system deps for opencv (headless wheels still need libGL on slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY stegstr ./stegstr

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[web,mcp]"

# runtime state (SQLite + carrier store) — mount a volume here
ENV STEGSTR_DB=/data/stegstr.db \
    STEGSTR_DATA=/data

RUN mkdir -p /data

EXPOSE 8000

# `serve` = web UI + JSON API; add `--host 0.0.0.0` via CMD defaults already
CMD ["stegstr", "serve", "--host", "0.0.0.0", "--port", "8000"]
