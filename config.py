import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENROUTER_API_KEY", os.getenv("OPENAI_API_KEY", ""))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))

# Path to a YouTube cookies.txt file (Netscape format).
# Set this as a Railway env var pointing to a persistent file, or upload via /api/cookies.
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", "./cookies.txt"))

# When True, all previously generated job outputs are deleted automatically
# before a new video is processed. Keeps disk usage low on Railway.
CLEANUP_OLD_JOBS = os.getenv("CLEANUP_OLD_JOBS", "true").lower() in ("1", "true", "yes", "on")
