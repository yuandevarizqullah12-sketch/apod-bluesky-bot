#!/usr/bin/env python3
"""
NASA APOD Bluesky Bot – GitHub Actions Edition
Run with: python apod.py daily   or   python apod.py flashback
"""

import os
import sys
import time
import json
import hashlib
import logging
import tempfile
import re
import subprocess
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable

import requests
from PIL import Image
from dotenv import load_dotenv
from atproto import Client
from grapheme import length, slice

# =====================
# LOGGING
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =====================
# ENV
# =====================
load_dotenv()

NASA_API_KEY = os.getenv("NASA_API_KEY")
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")   # provided by GitHub Actions

STATE_FILE = "data.json"
APOD_URL = "https://api.nasa.gov/planetary/apod"

# =====================
# RETRY DECORATOR
# =====================
def retry_call(func: Callable, *args, retries: int = 3, delay: float = 2.0, backoff: float = 2.0, **kwargs) -> Any:
    """Execute a callable with exponential backoff retries."""
    last_exception = None
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Retry {attempt+1}/{retries} for {func.__name__}: {e}")
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt)
                time.sleep(sleep_time)
    raise last_exception

# =====================
# HTTP SESSION (reused)
# =====================
_session = requests.Session()
_session.headers.update({"User-Agent": "NASA-APOD-Bot/1.0"})

def get_with_retry(url: str, params: Optional[Dict] = None, timeout: int = 20) -> requests.Response:
    def _get():
        return _session.get(url, params=params, timeout=timeout)
    return retry_call(_get, retries=3, delay=1)

# =====================
# STATE (atomic load/save)
# =====================
DEFAULT_STATE = {
    "last_date": None,
    "keys": [],
    "is_posting": False,
    "last_daily_post": None,
    "last_flashback_post": None
}

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        logger.info("State file not found, creating default")
        save_state(DEFAULT_STATE)
        return DEFAULT_STATE.copy()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return {**DEFAULT_STATE, **data}
    except Exception as e:
        logger.error(f"Failed to load state: {e}, using defaults")
        return DEFAULT_STATE.copy()

def save_state(state: Dict[str, Any]) -> None:
    """Atomically write state to a temporary file, then rename."""
    try:
        dirname = os.path.dirname(STATE_FILE) or "."
        with tempfile.NamedTemporaryFile(mode="w", dir=dirname, delete=False) as tf:
            json.dump(state, tf, indent=2)
            temp_name = tf.name
        os.replace(temp_name, STATE_FILE)
        logger.debug("State saved atomically")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")
        raise

# =====================
# NASA API
# =====================
def fetch_apod(date: Optional[str] = None) -> Dict[str, Any]:
    params = {"api_key": NASA_API_KEY}
    if date:
        params["date"] = date
    r = get_with_retry(APOD_URL, params=params)
    r.raise_for_status()
    return r.json()

def fetch_random_apod() -> Dict[str, Any]:
    y = 2005
    m = int.from_bytes(os.urandom(1), "big") % 12 + 1
    d = int.from_bytes(os.urandom(1), "big") % 28 + 1
    return fetch_apod(f"{y}-{m:02d}-{d:02d}")

# =====================
# TEXT UTILITIES
# =====================
def smart_cut(text: str, limit: int = 260) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = ""
    for s in sentences:
        if len(result + s) > limit:
            break
        result += s + ". " if s else ""
    return result.strip()

def grapheme_truncate(text: str, limit: int = 300) -> str:
    if length(text) <= limit:
        return text
    return slice(text, 0, limit)

# =====================
# POST FORMAT
# =====================
def format_apod(apod: Dict[str, Any], mode: str = "daily") -> str:
    title = apod.get("title", "")
    date = apod.get("date", "")
    explanation = smart_cut(apod.get("explanation", ""), 180)
    credit = apod.get("copyright", "NASA")
    link = "https://apod-web.vercel.app" if mode == "daily" else apod.get("url", "")
    text = f"""🌌 NASA APOD
📅 {date}
✨ {title}

{explanation}

👤 {credit}
🔗 {link}
"""
    return grapheme_truncate(text, 300)

# =====================
# DUPLICATE SYSTEM
# =====================
def make_key(mode: str, date: str, text: str) -> str:
    raw = f"{mode}_{date}_{text}"
    return hashlib.sha256(raw.encode()).hexdigest()

def can_post(state: Dict[str, Any], key: str) -> bool:
    if state.get("is_posting"):
        return False
    if key in state.get("keys", []):
        return False
    return True

def save_key(state: Dict[str, Any], key: str) -> None:
    state["keys"].append(key)
    state["keys"] = state["keys"][-50:]
    save_state(state)

# =====================
# IMAGE COMPRESSION
# =====================
def compress_image(url: str) -> bytes:
    img_data = get_with_retry(url, timeout=30).content
    img = Image.open(BytesIO(img_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail((1400, 1400))
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=75, optimize=True)
    buffer.seek(0)
    img.close()
    return buffer.read()

# =====================
# BLUESKY
# =====================
def upload_image(client: Client, url: str) -> Any:
    img = compress_image(url)
    return client.com.atproto.repo.upload_blob(img).blob

def post(client: Client, text: str, media_type: str, image_url: Optional[str] = None) -> None:
    if media_type == "image" and image_url:
        blob = retry_call(upload_image, client, image_url, retries=3, delay=2)
        client.send_post(
            text=text,
            embed={
                "$type": "app.bsky.embed.images",
                "images": [{"alt": "NASA APOD", "image": blob}]
            }
        )
    else:
        client.send_post(text=text)

# =====================
# SAFE POST ENGINE
# =====================
def safe_post(client: Client, state: Dict[str, Any], text: str, key: str,
              media_type: str, image_url: Optional[str] = None) -> bool:
    if not can_post(state, key):
        logger.info("🚫 Duplicate skipped")
        return False

    try:
        state["is_posting"] = True
        save_state(state)

        post(client, text, media_type, image_url)

        save_key(state, key)
        logger.info("✅ Posted successfully")
        return True

    except Exception as e:
        logger.error(f"💥 Post error: {e}")
        return False
    finally:
        state["is_posting"] = False
        save_state(state)

# =====================
# JOB FUNCTIONS
# =====================
def run_daily(client: Client, state: Dict[str, Any]) -> bool:
    """Run the daily APOD job. Returns True if state changed."""
    # Get today's UTC date
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Fetch latest APOD (may be today or earlier)
    apod = fetch_apod()
    apod_date = apod["date"]

    # If APOD date is not today, NASA hasn't updated yet
    if apod_date != today:
        logger.info(f"NASA APOD not available yet (latest: {apod_date}, today: {today})")
        return False

    # Already posted today?
    if state.get("last_daily_post") == apod_date:
        logger.info("⏭️ Daily post for today already done")
        return False

    text = format_apod(apod, "daily")
    key = make_key("daily", apod["date"], text)

    success = safe_post(client, state, text, key, apod["media_type"], apod["url"])
    if success:
        state["last_date"] = apod["date"]
        state["last_daily_post"] = apod["date"]
        save_state(state)
    return success

def run_flashback(client: Client, state: Dict[str, Any]) -> bool:
    """Run the flashback job. Returns True if state changed."""
    # Try up to 10 random APODs to find a unique one
    for _ in range(10):
        apod = fetch_random_apod()
        text = format_apod(apod, "flashback")
        key = make_key("flashback", apod["date"], text)

        if can_post(state, key):
            # Also check if we already posted this exact date as a flashback
            if state.get("last_flashback_post") == apod["date"]:
                continue
            success = safe_post(client, state, text, key, apod["media_type"], apod["url"])
            if success:
                state["last_flashback_post"] = apod["date"]
                save_state(state)
            return success
        else:
            logger.info(f"Flashback candidate {apod['date']} already posted, trying another")

    logger.warning("Could not find a unique flashback APOD after 10 attempts")
    return False

# =====================
# GIT COMMIT (for state persistence)
# =====================
def commit_state_if_changed() -> None:
    """Commit data.json if it has changed, and push using GITHUB_TOKEN."""
    if not GITHUB_TOKEN:
        logger.info("No GITHUB_TOKEN found – skipping git commit (local run)")
        return

    # Check if the file has changes
    try:
        status = subprocess.check_output(["git", "status", "--porcelain", STATE_FILE], text=True).strip()
        if not status:
            logger.debug("No changes to data.json")
            return
    except Exception as e:
        logger.error(f"Git status check failed: {e}")
        return

    # Configure git (use the token for authentication)
    repo_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{os.environ.get('GITHUB_REPOSITORY')}.git"
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Update APOD state [skip ci]"], check=True)
        subprocess.run(["git", "push", repo_url, "HEAD"], check=True)
        logger.info("State committed and pushed to repository")
    except Exception as e:
        logger.error(f"Git commit/push failed: {e}")

# =====================
# MAIN ENTRY
# =====================
def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python apod.py [daily|flashback]")
        sys.exit(1)

    mode = sys.argv[1].lower()
    if mode not in ("daily", "flashback"):
        print("Invalid mode. Choose 'daily' or 'flashback'.")
        sys.exit(1)

    # Load state
    state = load_state()

    # Login to Bluesky (with retries)
    client = Client()
    retry_call(client.login, BSKY_HANDLE, BSKY_PASSWORD, retries=3, delay=2)

    # Run the selected job
    changed = False
    if mode == "daily":
        changed = run_daily(client, state)
    else:  # flashback
        changed = run_flashback(client, state)

    # If state was updated, commit the change for next workflow run
    if changed:
        commit_state_if_changed()
    else:
        logger.info("No state change – nothing to commit")

    logger.info(f"Job '{mode}' finished. Exiting.")

if __name__ == "__main__":
    main()