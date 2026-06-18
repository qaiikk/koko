"""
YouTube Shorts Trimmer - Backend
Supports: YouTube URL download OR direct file upload.
Real-time progress via Server-Sent Events (SSE).

Transcription strategy (NO Whisper / NO local STT):
    1. User-uploaded caption file (SRT / VTT / JSON3)
    2. YouTube captions via yt-dlp (manual + auto-generated)
    3. Hard fail with a clear error if neither is available.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import uuid
import asyncio
import zipfile
from pathlib import Path
from typing import Optional, AsyncGenerator
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

import config

app = FastAPI(title="YouTube Shorts Trimmer")


@app.middleware("http")
async def normalize_path(request: Request, call_next):
    """Collapse duplicate slashes (e.g. //api/process -> /api/process).

    Some frontends build URLs like `${BASE}/api/process` where BASE ends with
    '/', producing '//api...' which FastAPI routes as 404. This makes those
    work transparently.
    """
    scope = request.scope
    path = scope["path"]
    if "//" in path:
        # Collapse runs of slashes, but keep a single leading slash.
        collapsed = "/" + "/".join(seg for seg in path.split("/") if seg)
        scope["path"] = collapsed
        # raw_path is used by routing too; keep them consistent.
        scope["raw_path"] = collapsed.encode("ascii", "ignore")
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store
jobs: dict = {}
# Event queues for SSE
events: dict[str, asyncio.Queue] = {}

OUTPUT_DIR = Path(config.OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
@app.get("/health")
async def health():
    """Liveness/readiness probe for Railway and browsers."""
    return {"status": "ok", "service": "youtube-shorts-trimmer", "endpoints": ["/api/process", "/api/jobs", "/api/styles"]}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def run_cmd(cmd: list[str], cwd: Optional[str] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, start_new_session=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd[:3])}...\n{result.stderr[:500]}")
    return result


async def send_event(job_id: str, data: dict):
    """Send SSE event to connected clients."""
    if job_id in events:
        await events[job_id].put(data)


def update_job(job_id: str, **kwargs):
    """Update job state and broadcast."""
    job = jobs[job_id]
    job.update(kwargs)
    # Queue the event (non-blocking)
    if job_id in events:
        try:
            events[job_id].put_nowait(dict(job))
        except asyncio.QueueFull:
            pass


# Cache directory for downloaded videos
CACHE_DIR = OUTPUT_DIR / "_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# URL -> cached video path mapping
url_cache: dict[str, Path] = {}


def get_video_id(url: str) -> str:
    """Extract YouTube video ID from URL."""
    m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else str(hash(url))


# ─── Old-job cleanup (keeps Railway disk usage low) ──────────────────────────

def cleanup_old_jobs(keep_job_id: Optional[str] = None):
    """Delete all previously generated job output directories before a new run.

    Skips the current job's directory and the internal _cache folder. Safe to call
    repeatedly. Failures are best-effort (logged, never fatal) so a new job is
    never blocked by a stale file lock.
    """
    if not getattr(config, "CLEANUP_OLD_JOBS", True):
        return

    if not OUTPUT_DIR.exists():
        return

    for entry in OUTPUT_DIR.iterdir():
        try:
            # Never touch the cache or the in-progress job
            if entry.name == "_cache":
                continue
            if keep_job_id and entry.name == keep_job_id:
                continue

            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.is_file():
                try:
                    entry.unlink()
                except OSError:
                    pass
        except Exception:
            # Best-effort: keep going even if one dir fails
            continue


# ─── Download / Upload ───────────────────────────────────────────────────────

def _detect_js_runtime() -> Optional[str]:
    """Find an available JS runtime for yt-dlp (deno > bun > node)."""
    import shutil as _sh
    for name in ("deno", "bun", "node", "nodejs"):
        if _sh.which(name):
            return name
    return None


# Cached at first use so we don't run `which` on every call.
_JS_RUNTIME: Optional[str] = None


def _js_runtime() -> Optional[str]:
    global _JS_RUNTIME
    if _JS_RUNTIME is None:
        _JS_RUNTIME = _detect_js_runtime()
    return _JS_RUNTIME


def yt_dlp_base_args() -> list[str]:
    """Common yt-dlp flags shared by every call.

    - Picks whichever JS runtime is installed (instead of hardcoding `node`),
      which fixes: "No supported JavaScript runtime could be found."
    - Uses alternative YouTube player clients (android/ios/web) and retries
      with linear backoff, which mitigates: "HTTP Error 429: Too Many Requests"
      from Railway's shared egress IPs hitting the web player.
    """
    args = [
        "yt-dlp",
        "--no-update",
        "--no-warnings",
        # Use non-web clients first to dodge the web 429 throttle.
        "--extractor-args", "youtube:player_client=android,ios,web_default,web",
        "--retries", "10",
        "--fragment-retries", "10",
        # Linear backoff between 5s and 30s — smooths out transient 429s.
        "--retry-sleep", "linear=5..30",
        # Realistic browser UA + lenient TLS for flaky egress IPs.
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--no-check-certificates",
    ]

    runtime = _js_runtime()
    if runtime:
        args += ["--js-runtimes", runtime]
    return args


def fetch_video_metadata(youtube_url: str) -> dict:
    """Fetch video title and thumbnail from YouTube."""
    try:
        cmd = yt_dlp_base_args() + [
            "--skip-download",
            "--print", "%(title)s\n%(thumbnail)s\n%(channel)s\n%(duration)s",
            "--no-playlist", youtube_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, start_new_session=True)
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            return {
                "title": lines[0] if len(lines) > 0 else "",
                "thumbnail": lines[1] if len(lines) > 1 else "",
                "channel": lines[2] if len(lines) > 2 else "",
                "duration": lines[3] if len(lines) > 3 else "",
            }
    except Exception:
        pass
    return {}


def download_video(youtube_url: str, job_dir: Path) -> Path:
    """Download YouTube video with caching. Returns cached file if already downloaded."""
    vid = get_video_id(youtube_url)
    cached_path = CACHE_DIR / f"{vid}.mp4"

    # Check cache first
    if cached_path.exists() and cached_path.stat().st_size > 0:
        job_video = job_dir / "source.mp4"
        if not job_video.exists():
            os.symlink(cached_path.resolve(), job_video)
        return job_video

    # Check in-memory cache
    if youtube_url in url_cache and url_cache[youtube_url].exists():
        cached = url_cache[youtube_url]
        job_video = job_dir / "source.mp4"
        if not job_video.exists():
            os.symlink(cached.resolve(), job_video)
        return job_video

    # Download to cache (retries + alt clients are baked into base args)
    cmd = yt_dlp_base_args() + [
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", str(cached_path),
        "--no-playlist",
        youtube_url,
    ]
    run_cmd(cmd, timeout=300)
    if not cached_path.exists():
        raise RuntimeError("Download failed - video file not found")

    url_cache[youtube_url] = cached_path

    job_video = job_dir / "source.mp4"
    if not job_video.exists():
        os.symlink(cached_path.resolve(), job_video)
    return job_video


def save_uploaded_file(upload: UploadFile, job_dir: Path) -> Path:
    """Save an uploaded video file."""
    video_path = job_dir / f"source{Path(upload.filename or 'video.mp4').suffix}"
    with open(video_path, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return video_path


# ─── Captions ────────────────────────────────────────────────────────────────
#
# Transcription is now caption-only. There is NO speech-to-text fallback.
# Supported sources:
#   1. User-uploaded: .srt / .vtt / .json3
#   2. YouTube (via yt-dlp): manual + auto-generated captions
#

SUPPORTED_CAPTION_EXTS = {".srt", ".vtt", ".json3"}


class CaptionError(Exception):
    """Raised when a caption file is missing, unsupported, or unparseable."""


def fetch_youtube_captions(youtube_url: str, job_dir: Path) -> Optional[list[dict]]:
    """Download manual + auto-generated captions from YouTube via yt-dlp.

    Returns parsed segments on success, or None if no captions are available.
    Never raises — callers handle the None case with a clear error.
    """
    out_base = job_dir / "captions"
    cmd = yt_dlp_base_args() + [
        "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*,en",
        "--sub-format", "json3/srv3/vtt/srt/best",
        "--skip-download",
        "-o", str(out_base),
        "--no-playlist",
        youtube_url,
    ]
    try:
        run_cmd(cmd, timeout=90)
    except Exception:
        return None

    # Prefer JSON3 (richest timing/word data), then vtt, then srt.
    patterns = [
        "captions*.json3",
        "captions*.srv3",
        "captions*.vtt",
        "captions*.srt",
        "captions*.en*.json3",
    ]
    for pattern in patterns:
        files = glob.glob(str(job_dir / pattern))
        if files:
            try:
                segs = parse_caption_file(files[0])
                if segs:
                    return segs
            except CaptionError:
                # Try the next available file instead of failing outright
                continue
    return None


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_caption_file(filepath) -> list[dict]:
    """Validate + parse a caption file (json3 / vtt / srt) into segments.

    Raises CaptionError on unsupported extensions, empty files, or unreadable
    content. Malformed individual blocks are skipped gracefully rather than
    aborting the whole file.
    """
    path = Path(filepath)
    if not path.exists():
        raise CaptionError(f"Caption file not found: {path}")
    if path.stat().st_size == 0:
        raise CaptionError(f"Caption file is empty: {path.name}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED_CAPTION_EXTS:
        raise CaptionError(
            f"Unsupported caption format '{ext}'. Supported: {sorted(SUPPORTED_CAPTION_EXTS)}"
        )

    try:
        if ext == ".json3":
            segs = parse_json3(path)
        elif ext == ".vtt":
            segs = parse_vtt(path)
        else:  # .srt
            segs = parse_srt(path)
    except CaptionError:
        raise
    except Exception as e:
        raise CaptionError(f"Failed to parse {path.name}: {e}")

    if not segs:
        raise CaptionError(f"No usable caption lines found in {path.name}")
    return segs


def parse_json3(filepath) -> list[dict]:
    """Parse YouTube JSON3 caption format. Tolerant of malformed events."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise CaptionError(f"Invalid JSON3 file: {e}")

    segments: list[dict] = []
    for ev in data.get("events", []):
        try:
            if "segs" not in ev:
                continue
            start_ms = int(ev.get("tStartMs", 0))
            duration_ms = int(ev.get("dDurationMs", 2000) or 2000)
            text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
            if not text or text == "\n":
                continue

            words: list[dict] = []
            offset_ms = 0
            for s in ev["segs"]:
                w = (s.get("utf8", "") or "").strip()
                if w:
                    words.append({
                        "word": w,
                        "start": (start_ms + offset_ms) / 1000,
                        "end": (start_ms + offset_ms + 500) / 1000,
                    })
                offset_ms += int(s.get("tOffsetMs", 0) or 0)

            segments.append({
                "id": len(segments),
                "start": start_ms / 1000,
                "end": (start_ms + duration_ms) / 1000,
                "text": text,
                "words": words,
            })
        except Exception:
            # Skip a single bad event, keep the rest of the file
            continue
    return segments


_VTT_TIME_RE = re.compile(r'(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})')


def parse_vtt(filepath) -> list[dict]:
    """Parse WebVTT caption format. Tolerant of header lines / styling tags."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        content = f.read()

    segments: list[dict] = []
    blocks = re.split(r'\n\s*\n', content)
    for block in blocks:
        m = _VTT_TIME_RE.search(block)
        if not m:
            continue
        try:
            start = _parse_timestamp(m.group(1))
            end = _parse_timestamp(m.group(2))
        except (ValueError, IndexError):
            continue

        # Drop cue header (index), timing line, and WEBVTT/NOTE metadata
        text_lines = []
        for line in block.splitlines():
            if _VTT_TIME_RE.search(line):
                continue
            if line.strip().isdigit():
                continue
            if line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            if line.startswith("STYLE") or line.startswith("REGION"):
                continue
            text_lines.append(line)

        text = re.sub(r'<[^>]+>', '', ' '.join(text_lines)).strip()
        if text:
            segments.append({
                "id": len(segments),
                "start": start,
                "end": end,
                "text": text,
                "words": [],
            })
    return segments


_SRT_TIME_RE = re.compile(r'(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})')


def parse_srt(filepath) -> list[dict]:
    """Parse SRT caption format. Tolerant of comma/dot decimal and blank lines."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        content = f.read()

    segments: list[dict] = []
    blocks = re.split(r'\n\s*\n', content)
    for block in blocks:
        m = _SRT_TIME_RE.search(block)
        if not m:
            continue
        try:
            start = _parse_timestamp(m.group(1))
            end = _parse_timestamp(m.group(2))
        except (ValueError, IndexError):
            continue

        text_lines = []
        for line in block.splitlines():
            if _SRT_TIME_RE.search(line):
                continue
            if line.strip().isdigit():
                continue
            text_lines.append(line)

        text = ' '.join(text_lines).strip()
        if text:
            segments.append({
                "id": len(segments),
                "start": start,
                "end": end,
                "text": text,
                "words": [],
            })
    return segments


def _parse_timestamp(t: str) -> float:
    """Parse HH:MM:SS.mmm, HH:MM:SS,mmm, or MM:SS.mmm into seconds."""
    t = t.replace(',', '.')
    parts = t.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        m, s = divmod(seg["start"], 60)
        lines.append(f"[{int(m):02d}:{s:05.2f}] {seg['text']}")
    return "\n".join(lines)


# ─── AI Analysis ─────────────────────────────────────────────────────────────

def analyze_transcript(transcript: str, num_clips: int, clip_duration: int) -> list[dict]:
    from openai import OpenAI
    client = OpenAI(api_key=config.OPENAI_API_KEY, base_url="https://openrouter.ai/api/v1")

    prompt = f"""You are a YouTube Shorts curator for kids/teens. Find the {num_clips} best {clip_duration}-second clips from this transcript.

Pick moments that are: exciting, funny, surprising, high-energy, kid-friendly.
Each clip must work standalone with no context needed.

Return ONLY valid JSON object with a "clips" array. Each item has:
- start_time (float, seconds)
- end_time (start_time + {clip_duration})
- title (catchy, max 60 chars)
- description (kid-friendly, 3-5 hashtags)

TRANSCRIPT:
{transcript[:15000]}"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b:free",
        messages=[
            {"role": "system", "content": "Respond with valid JSON only. No markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )

    content = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    content = re.sub(r'^```(?:json)?\s*', '', content)
    content = re.sub(r'\s*```$', '', content)

    data = json.loads(content)
    if isinstance(data, dict):
        for key in ["clips", "shorts", "results", "data"]:
            if key in data:
                data = data[key]
                break
    if isinstance(data, list):
        return data
    raise ValueError(f"Bad response format")


# ─── Caption Styles ─────────────────────────────────────────────────────────

CAPTION_STYLES = {
    "bold": {
        "name": "Bold White",
        "style": "FontName=Arial Black,FontSize=26,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderColor=&H00000000,BackColour=&H80000000,Outline=4,Shadow=3,Bold=1,Alignment=2,MarginV=100,Spacing=1",
    },
    "minimal": {
        "name": "Clean",
        "style": "FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00202020,BorderColor=&H00202020,BackColour=&H00000000,Outline=1.5,Shadow=0,Bold=0,Alignment=2,MarginV=70",
    },
    "pop": {
        "name": "Pop Yellow",
        "style": "FontName=Impact,FontSize=28,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderColor=&H00000000,BackColour=&H80000000,Outline=4,Shadow=4,Bold=1,Italic=0,Alignment=2,MarginV=100,Spacing=2",
    },
    "subtitle": {
        "name": "Subtitle Bar",
        "style": "FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderColor=&H00000000,BackColour=&HD0000000,Outline=1,Shadow=0,Bold=0,Alignment=2,MarginV=50,BorderStyle=4",
    },
    "neon": {
        "name": "Neon Glow",
        "style": "FontName=Arial Black,FontSize=24,PrimaryColour=&H0000FF80,OutlineColour=&H00004422,BorderColor=&H00004422,BackColour=&H80000000,Outline=3,Shadow=4,Bold=1,Alignment=2,MarginV=100,Spacing=1",
    },
    "fire": {
        "name": "Fire",
        "style": "FontName=Impact,FontSize=28,PrimaryColour=&H000044FF,OutlineColour=&H00000088,BorderColor=&H00000088,BackColour=&H80000000,Outline=4,Shadow=3,Bold=1,Alignment=2,MarginV=100,Spacing=2",
    },
}


# ─── Video Processing ────────────────────────────────────────────────────────

def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(segments: list[dict], start: float, end: float) -> str:
    lines, idx = [], 1
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        words = seg.get("words", [])
        if not words:
            rs = max(0, seg["start"] - start)
            re_ = min(end - start, seg["end"] - start)
            if re_ > rs:
                lines.append(f"{idx}\n{format_srt_time(rs)} --> {format_srt_time(re_)}\n{seg['text']}\n")
                idx += 1
        else:
            for i in range(0, len(words), 3):
                chunk = words[i:i + 3]
                ws = max(0, chunk[0]["start"] - start)
                we = min(end - start, chunk[-1]["end"] - start)
                if we > ws:
                    text = " ".join(w["word"] for w in chunk)
                    lines.append(f"{idx}\n{format_srt_time(ws)} --> {format_srt_time(we)}\n{text}\n")
                    idx += 1
    return "\n".join(lines)


def process_clip(video_path: Path, clip: dict, segments: list[dict], job_dir: Path, idx: int, captions_enabled: bool = True, caption_style: str = "bold") -> dict:
    """Render a clip. Returns dict with 'captioned' and 'clean' paths."""
    start, end = clip["start_time"], clip["end_time"]
    duration = end - start
    result = {}

    base_vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

    # Always render clean version (no captions)
    clean_path = job_dir / f"clip_{idx}_clean.mp4"
    cmd_clean = [
        "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
        "-i", str(video_path.resolve()),
        "-vf", base_vf,
        "-c:v", "libopenh264", "-b:v", "2500k", "-maxrate", "4000k", "-bufsize", "5000k",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(clean_path),
    ]
    run_cmd(cmd_clean, timeout=180)
    result["clean"] = str(clean_path)

    # Render captioned version if enabled
    if captions_enabled and segments:
        captioned_path = job_dir / f"clip_{idx}.mp4"
        srt_path = (job_dir / f"clip_{idx}.srt").resolve()
        srt_path.write_text(generate_srt(segments, start, end))
        escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")
        style = CAPTION_STYLES.get(caption_style, CAPTION_STYLES["bold"])["style"]
        vf_with_subs = f"{base_vf},subtitles='{escaped_srt}':force_style='{style}'"

        cmd_cap = [
            "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
            "-i", str(video_path.resolve()),
            "-vf", vf_with_subs,
            "-c:v", "libopenh264", "-b:v", "2500k", "-maxrate", "4000k", "-bufsize", "5000k",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(captioned_path),
        ]
        run_cmd(cmd_cap, timeout=180)
        result["captioned"] = str(captioned_path)

    return result


# ─── Main Pipeline ───────────────────────────────────────────────────────────

NO_CAPTIONS_ERROR = {
    "status": "error",
    "message": "No captions available. Upload a caption file or use a YouTube video with captions.",
}


def process_video_job(job_id: str, video_path: Path, num_clips: int, clip_duration: int,
                      youtube_url: Optional[str] = None,
                      caption_path: Optional[Path] = None,
                      captions_enabled: bool = True,
                      caption_style: str = "bold"):
    job_dir = video_path.parent
    try:
        # Step 0: Fetch video metadata if YouTube URL
        if youtube_url:
            meta = fetch_video_metadata(youtube_url)
            if meta:
                update_job(job_id, video_meta=meta)

        # ── Transcript: user captions → YouTube captions → FAIL ──────────
        # No Whisper. No audio extraction. No local speech-to-text.
        segments: Optional[list[dict]] = None

        # Priority 1: User-uploaded caption file
        if caption_path and caption_path.exists():
            update_job(job_id, status="transcribing", progress=10, message="Loading your caption file...")
            try:
                segments = parse_caption_file(caption_path)
                update_job(job_id, progress=20, message="Caption file loaded!")
            except CaptionError as e:
                update_job(job_id, progress=12, message=f"Caption file invalid ({e}). Trying YouTube captions...")

        # Priority 2: YouTube manual + auto captions (only fail-safe fallback)
        if not segments and youtube_url:
            update_job(job_id, status="transcribing", progress=14, message="Fetching YouTube captions...")
            segments = fetch_youtube_captions(youtube_url, job_dir)
            if segments:
                update_job(job_id, progress=20, message="YouTube captions loaded!")

        # No captions anywhere → fail the job with a clear error.
        if not segments:
            update_job(
                job_id,
                status="error",
                progress=0,
                message=NO_CAPTIONS_ERROR["message"],
                error=NO_CAPTIONS_ERROR["message"],
            )
            return

        transcript = format_transcript(segments)
        (job_dir / "transcript.txt").write_text(transcript)
        (job_dir / "segments.json").write_text(json.dumps(segments, indent=2))
        update_job(job_id, progress=30, message="Transcription done. AI analyzing...")

        # Step 3: AI Analysis
        clip_suggestions = analyze_transcript(transcript, num_clips, clip_duration)
        max_time = segments[-1]["end"] if segments else 0
        valid_clips = []
        for c in clip_suggestions:
            st, et = float(c["start_time"]), float(c["end_time"])
            st = max(0, st)
            et = min(max_time, et)
            if et - st >= 10:
                valid_clips.append({"start_time": st, "end_time": et, "title": c.get("title", f"Clip {len(valid_clips)+1}"), "description": c.get("description", "")})

        update_job(job_id, progress=40, message=f"Found {len(valid_clips)} clips. Rendering...")

        # Step 4: Render clips - emit each clip as it finishes
        job_clips = []
        for i, clip in enumerate(valid_clips):
            pct = 40 + int(55 * (i / len(valid_clips)))
            clip_info = {"index": i, "start_time": clip["start_time"], "end_time": clip["end_time"], "title": clip["title"], "description": clip["description"], "status": "rendering"}
            job_clips.append(clip_info)
            update_job(job_id, status="rendering", progress=pct, message=f"Rendering clip {i+1}/{len(valid_clips)}...", clips=list(job_clips))
            try:
                paths = process_clip(video_path, clip, segments, job_dir, i, captions_enabled, caption_style)
                clip_info["file_path"] = paths.get("captioned") or paths.get("clean")
                clip_info["file_path_clean"] = paths.get("clean")
                clip_info["file_path_captioned"] = paths.get("captioned")
                clip_info["has_both"] = "captioned" in paths and "clean" in paths
                clip_info["status"] = "done"
            except Exception as e:
                clip_info["status"] = "error"
                clip_info["error"] = str(e)
            # Emit updated clips immediately so frontend shows each clip as it finishes
            pct_done = 40 + int(55 * ((i + 1) / len(valid_clips)))
            update_job(job_id, status="rendering", progress=pct_done, message=f"Clip {i+1}/{len(valid_clips)} done.", clips=list(job_clips))

        update_job(job_id, status="done", progress=100, message=f"Done! {len(valid_clips)} clips created.", clips=job_clips)

    except Exception as e:
        update_job(job_id, status="error", progress=0, message=f"Error: {e}", error=str(e))


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs(limit: int = 20):
    """List recent jobs with summary info."""
    sorted_jobs = sorted(jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)
    return [
        {
            "job_id": j["job_id"],
            "status": j["status"],
            "progress": j.get("progress", 0),
            "message": j.get("message", ""),
            "clip_count": len(j.get("clips", [])),
            "done_count": sum(1 for c in j.get("clips", []) if c.get("status") == "done"),
            "error": j.get("error"),
        }
        for j in sorted_jobs[:limit]
    ]


@app.get("/api/download/{job_id}/all")
async def download_all_clips(job_id: str, version: str = "captioned"):
    """Download all clips for a job as a zip file."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    clips = jobs[job_id].get("clips", [])
    done_clips = [(i, c) for i, c in enumerate(clips) if c.get("status") == "done"]
    if not done_clips:
        raise HTTPException(400, "No completed clips available")

    zip_path = OUTPUT_DIR / job_id / f"all_clips_{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, clip in done_clips:
            if version == "clean" and clip.get("file_path_clean"):
                fp = clip["file_path_clean"]
            elif version == "captioned" and clip.get("file_path_captioned"):
                fp = clip["file_path_captioned"]
            else:
                fp = clip.get("file_path")
            if fp and Path(fp).exists():
                title = re.sub(r'[^\w\s-]', '', clip.get("title", f"clip_{idx}"))[:40]
                suffix = "_clean" if version == "clean" else "_captioned"
                arcname = f"{idx + 1:02d}_{title.replace(' ', '_')}{suffix}.mp4"
                zf.write(fp, arcname)

    return FileResponse(str(zip_path), media_type="application/zip", filename=f"shorts_{job_id}_{version}.zip")


@app.get("/api/styles")
async def get_styles():
    """Return available caption styles."""
    return {k: {"name": v["name"]} for k, v in CAPTION_STYLES.items()}


@app.post("/api/process")
async def start_processing(
    background_tasks: BackgroundTasks,
    youtube_url: Optional[str] = Form(None),
    num_clips: int = Form(5),
    clip_duration: int = Form(60),
    file: Optional[UploadFile] = File(None),
    caption_file: Optional[UploadFile] = File(None),
    captions_enabled: bool = Form(True),
    caption_style: str = Form("bold"),
):
    if not config.OPENAI_API_KEY:
        raise HTTPException(400, "Set OPENROUTER_API_KEY in .env")
    if not youtube_url and not file:
        raise HTTPException(400, "Provide a YouTube URL or upload a file")

    job_id = str(uuid.uuid4())[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Auto-cleanup: wipe all previously generated videos before starting a new one.
    cleanup_old_jobs(keep_job_id=job_id)

    # Save caption file if provided
    caption_path = None
    if caption_file:
        caption_path = job_dir / f"user_captions{Path(caption_file.filename or 'captions.srt').suffix}"
        with open(caption_path, "wb") as f:
            shutil.copyfileobj(caption_file.file, f)

    jobs[job_id] = {"job_id": job_id, "status": "queued", "progress": 0, "message": "Starting...", "clips": [], "error": None, "created_at": datetime.now().isoformat()}
    events[job_id] = asyncio.Queue(maxsize=50)

    if file:
        update_job(job_id, status="uploading", progress=5, message="Saving uploaded file...")
        video_path = save_uploaded_file(file, job_dir)
        background_tasks.add_task(process_video_job, job_id, video_path, num_clips, clip_duration, None, caption_path, captions_enabled, caption_style)
    else:
        # Check if video is already cached
        vid = get_video_id(youtube_url)
        is_cached = (CACHE_DIR / f"{vid}.mp4").exists()
        if is_cached:
            update_job(job_id, status="downloading", progress=5, message="Using cached video (no download needed)...")
        else:
            update_job(job_id, status="downloading", progress=5, message="Downloading from YouTube...")

        def download_and_process():
            try:
                vp = download_video(youtube_url, job_dir)
                if is_cached:
                    update_job(job_id, progress=8, message="Loaded from cache. Processing...")
                else:
                    update_job(job_id, progress=8, message="Download complete. Processing...")
                process_video_job(job_id, vp, num_clips, clip_duration, youtube_url, caption_path, captions_enabled, caption_style)
            except Exception as e:
                update_job(job_id, status="error", progress=0, message=f"Download error: {e}", error=str(e))

        background_tasks.add_task(download_and_process)

    return {"job_id": job_id}


@app.get("/api/events/{job_id}")
async def stream_events(job_id: str):
    """SSE endpoint for real-time progress."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        if job_id not in events:
            events[job_id] = asyncio.Queue(maxsize=50)

        # Send current state immediately
        yield f"data: {json.dumps(jobs[job_id])}\n\n"

        while True:
            try:
                data = await asyncio.wait_for(events[job_id].get(), timeout=30)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("status") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield f"data: {json.dumps(jobs[job_id])}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.get("/api/results/{job_id}")
async def get_results(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return {"status": jobs[job_id]["status"], "clips": jobs[job_id].get("clips", [])}


@app.get("/api/download/{job_id}/{clip_index}")
async def download_clip(job_id: str, clip_index: int, version: str = "captioned"):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    clips = jobs[job_id].get("clips", [])
    if clip_index >= len(clips):
        raise HTTPException(404, "Clip not found")
    clip = clips[clip_index]
    # Pick version: captioned > clean > default file_path
    if version == "clean" and clip.get("file_path_clean"):
        fp = clip["file_path_clean"]
    elif version == "captioned" and clip.get("file_path_captioned"):
        fp = clip["file_path_captioned"]
    else:
        fp = clip.get("file_path")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "Clip file not found")
    title = re.sub(r'[^\w\s-]', '', clip.get("title", f"clip_{clip_index}"))[:40]
    suffix = "_clean" if version == "clean" else ""
    return FileResponse(fp, media_type="video/mp4", filename=f"{title.replace(' ', '_')}_short{suffix}.mp4")


@app.get("/api/thumbnail/{job_id}/{clip_index}")
async def get_thumbnail(job_id: str, clip_index: int):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    clips = jobs[job_id].get("clips", [])
    if clip_index >= len(clips):
        raise HTTPException(404, "Clip not found")
    fp = clips[clip_index].get("file_path")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "Clip file not found")
    job_dir = OUTPUT_DIR / job_id
    thumb = job_dir / f"thumb_{clip_index}.jpg"
    if not thumb.exists():
        try:
            run_cmd(["ffmpeg", "-y", "-i", fp, "-ss", "5", "-vframes", "1", "-q:v", "2", str(thumb)])
        except Exception:
            run_cmd(["ffmpeg", "-y", "-i", fp, "-ss", "0", "-vframes", "1", "-q:v", "2", str(thumb)])
    return FileResponse(str(thumb), media_type="image/jpeg")


@app.get("/api/stream/{job_id}/{clip_index}")
async def stream_clip(job_id: str, clip_index: int, version: str = "captioned"):
    """Stream a clip for in-browser preview."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    clips = jobs[job_id].get("clips", [])
    if clip_index >= len(clips):
        raise HTTPException(404, "Clip not found")
    clip = clips[clip_index]
    # Pick version
    if version == "clean" and clip.get("file_path_clean"):
        fp = clip["file_path_clean"]
    elif version == "captioned" and clip.get("file_path_captioned"):
        fp = clip["file_path_captioned"]
    else:
        fp = clip.get("file_path")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "Clip file not found")

    file_path = Path(fp)
    file_size = file_path.stat().st_size

    async def iterfile():
        with open(file_path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Disposition": "inline",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.BACKEND_PORT)
