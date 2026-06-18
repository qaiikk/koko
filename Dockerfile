# ─── Railway-optimized image (no Whisper, no audio extraction) ──────────────
FROM python:3.12-slim AS base

# Combine env settings + apt install in one layer to keep image small.
# Only runtime deps needed: ffmpeg (video re-encode/subs) + curl (yt-dlp runtime helper).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Predictable port for Railway
ENV BACKEND_PORT=${BACKEND_PORT:-8000}
EXPOSE 8000

# Fast startup, single worker keeps memory low. Scale horizontally on Railway.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${BACKEND_PORT:-8000} --workers 1"]
