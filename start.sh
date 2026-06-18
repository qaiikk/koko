#!/bin/sh
# Entrypoint for Railway / Docker.
# Picks the port from Railway's injected PORT, then BACKEND_PORT, else 8000.
# Prints a copyable public URL on startup.

# Strip CR from any CRLF this file may have picked up on Windows.
[ -n "${LF_FIX:-}" ] || true

# Self-update yt-dlp to latest nightly (bypasses YouTube bot detection)
echo "Checking for yt-dlp updates..."
python -m yt_dlp -U 2>/dev/null || yt-dlp -U 2>/dev/null || echo "yt-dlp update skipped"

PORT="${PORT:-${BACKEND_PORT:-8000}}"
HOST="${HOST:-0.0.0.0}"

# Public base URL for the frontend to talk to.
# Railway exposes the public domain in RAILWAY_PUBLIC_DOMAIN (no scheme).
PUBLIC_DOMAIN="${RAILWAY_PUBLIC_DOMAIN:-}"
if [ -z "$PUBLIC_DOMAIN" ] && [ -n "$RAILWAY_STATIC_URL" ]; then
    PUBLIC_DOMAIN="${RAILWAY_STATIC_URL#https://}"
fi
if [ -n "$PUBLIC_DOMAIN" ]; then
    PUBLIC_URL="https://${PUBLIC_DOMAIN}"
else
    PUBLIC_URL="http://localhost:${PORT}"
fi

echo "========================================================"
echo "  YouTube Shorts Trimmer backend is running."
echo "  Local : http://${HOST}:${PORT}"
echo "  Public: ${PUBLIC_URL}"
echo "  Frontend API base: ${PUBLIC_URL}/api"
echo "  Copy this link into your frontend config:"
echo "    ${PUBLIC_URL}/api"
echo "========================================================"

export BACKEND_PORT="$PORT"
exec uvicorn main:app --host "${HOST}" --port "${PORT}" --workers 1
