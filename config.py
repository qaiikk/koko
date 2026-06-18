import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENROUTER_API_KEY", os.getenv("OPENAI_API_KEY", ""))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))

# Raw YouTube cookie string (Netscape format content).
# If set, this takes priority over COOKIES_FILE and is written to disk automatically.
# You can paste the full contents of a cookies.txt export here as a single env var.
YOUTUBE_COOKIE = os.getenv("YOUTUBE_COOKIE", "")

# Path to a YouTube cookies.txt file (Netscape format).
# Used as fallback when YOUTUBE_COOKIE is not set, or upload via /api/cookies.
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", "./cookies.txt"))

# YouTube visitor_data + PO token for bypassing datacenter IP bot detection.
# Get visitor_data from Chrome console on youtube.com: ytcfg.get("VISITOR_DATA")
# Get PO token from: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#po-token
YOUTUBE_VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "")
YOUTUBE_PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "")

# When True, all previously generated job outputs are deleted automatically
# before a new video is processed. Keeps disk usage low on Railway.
CLEANUP_OLD_JOBS = os.getenv("CLEANUP_OLD_JOBS", "true").lower() in ("1", "true", "yes", "on")
