# ─── Railway-optimized image (no Whisper, no audio extraction) ──────────────
FROM python:3.12-slim AS base

# Combine env settings + apt install in one layer to keep image small.
# Only runtime deps needed: ffmpeg (video re-encode/subs) + curl (yt-dlp runtime helper).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + entrypoint
COPY . .

# Guarantee LF line endings for the shell entrypoint (file may have CRLF on Windows).
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

# Railway injects PORT automatically. Default to 8000 for local runs.
ENV PORT=8000 \
    BACKEND_PORT=8000
EXPOSE 8000

# Use our entrypoint so PORT expansion actually happens (sh, not a frozen string).
CMD ["./start.sh"]
