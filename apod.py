import os
import time
import json
import hashlib
import logging
import tempfile
import re
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable

import requests
from PIL import Image
from dotenv import load_dotenv
from atproto import Client
from grapheme import length, slice  # new for grapheme awareness

# =====================
# LOGGING SETUP
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =====================
# LOAD ENV
# =====================
load_dotenv()

NASA_API_KEY = os.getenv("NASA_API_KEY")
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = "data.json"
APOD_URL = "https://api.nasa.gov/planetary/apod"

# =====================
# RETRY DECORATOR (reused)
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
    """GET request with retries."""
    def _get():
        return _session.get(url, params=params, timeout=timeout)
    return retry_call(_get, retries=3, delay=1)

# =====================
# STATE SAFE LOAD / SAVE (atomic)
# =====================
DEFAULT_STATE = {
    "last_date": None,
    "keys": [],
    "is_posting": False,
    "last_daily_post": None,      # new
    "last_flashback_post": None   # new
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
# SMART TEXT CUT (improved punctuation)
# =====================
def smart_cut(text: str, limit: int = 260) -> str:
    # Split on ., !, ? followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = ""

    for s in sentences:
        if len(result + s) > limit:
            break
        result += s + ". " if s else ""

    return result.strip()

# =====================
# GRAPHEME‑AWARE TRUNCATION (new)
# =====================
def grapheme_truncate(text: str, limit: int = 300) -> str:
    if length(text) <= limit:
        return text
    return slice(text, 0, limit)

# =====================
# FORMAT POST (with grapheme limit)
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

    return grapheme_truncate(text, 300)   # 300 graphemes (Bluesky limit)

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
# IMAGE COMPRESSION (with resource cleanup)
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

    img.close()   # free resources

    return buffer.read()

# =====================
# BLUESKY POST (with retry for upload)
# =====================
def upload_image(client: Client, url: str) -> Any:
    img = compress_image(url)
    return client.com.atproto.repo.upload_blob(img).blob

def post(client: Client, text: str, media_type: str, image_url: Optional[str] = None) -> None:
    if media_type == "image" and image_url:
        # Reuse the retry mechanism for blob upload
        blob = retry_call(upload_image, client, image_url, retries=3, delay=2)

        client.send_post(
            text=text,
            embed={
                "$type": "app.bsky.embed.images",
                "images": [{
                    "alt": "NASA APOD",
                    "image": blob
                }]
            }
        )
    else:
        client.send_post(text=text)

# =====================
# SAFE POST ENGINE
# =====================
def safe_post(client: Client, state: Dict[str, Any], text: str, key: str,
              media_type: str, image_url: Optional[str] = None) -> None:
    if not can_post(state, key):
        logger.info("🚫 Duplicate skipped")
        return

    try:
        state["is_posting"] = True
        save_state(state)

        post(client, text, media_type, image_url)

        save_key(state, key)
        logger.info("✅ Posted successfully")

    except Exception as e:
        logger.error(f"💥 Post error: {e}")

    finally:
        state["is_posting"] = False
        save_state(state)

# =====================
# WAIT FOR DAILY UPDATE
# =====================
def wait_for_new_apod(last_date: Optional[str]) -> Dict[str, Any]:
    while True:
        try:
            apod = fetch_apod()
            if apod["date"] != last_date:
                return apod
            logger.info("⏳ Waiting NASA update...")
            time.sleep(3600)
        except Exception as e:
            logger.error(f"Error fetching APOD: {e}, retrying in 1 hour")
            time.sleep(3600)

# =====================
# JOBS (with extra safety fields)
# =====================
def run_daily(client: Client, state: Dict[str, Any]) -> None:
    apod = wait_for_new_apod(state.get("last_date"))

    # Extra safety: skip if we already posted this APOD date
    if state.get("last_daily_post") == apod["date"]:
        logger.info("⏭️ Daily post for this date already done")
        return

    text = format_apod(apod, "daily")
    key = make_key("daily", apod["date"], text)

    safe_post(client, state, text, key, apod["media_type"], apod["url"])

    state["last_date"] = apod["date"]
    state["last_daily_post"] = apod["date"]
    save_state(state)

def run_flashback(client: Client, state: Dict[str, Any]) -> None:
    apod = fetch_random_apod()

    if state.get("last_flashback_post") == apod["date"]:
        logger.info("⏭️ Flashback for this date already done")
        return

    text = format_apod(apod, "flashback")
    key = make_key("flashback", apod["date"], text)

    safe_post(client, state, text, key, apod["media_type"], apod["url"])

    state["last_flashback_post"] = apod["date"]
    save_state(state)

# =====================
# SCHEDULER (with periodic sleep)
# =====================
def sleep_until(hour: int) -> None:
    """Sleep until the next occurrence of the given hour, waking every hour."""
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=0, second=0)

        if target <= now:
            target += timedelta(days=1)

        remaining = (target - now).seconds
        if remaining <= 0:
            break
        # Sleep in chunks of at most 3600 seconds to keep Railway alive
        sleep_chunk = min(remaining, 3600)
        time.sleep(sleep_chunk)
        # Recalculate after waking (in case system time changed)
        if datetime.now() >= target:
            break

def scheduler(client: Client) -> None:
    state = load_state()

    while True:
        try:
            now = datetime.now()

            if now.hour == 12:
                logger.info("🚀 DAILY MODE")
                run_daily(client, state)
                sleep_until(19)

            elif now.hour == 20:
                logger.info("🚀 FLASHBACK MODE")
                run_flashback(client, state)
                sleep_until(12)

            else:
                time.sleep(30)

        except Exception as e:
            logger.error(f"💥 Scheduler error: {e}")
            time.sleep(10)

# =====================
# AUTO RESTART (with retried login)
# =====================
def main() -> None:
    client = Client()

    # Reuse retry_call for login
    retry_call(client.login, BSKY_HANDLE, BSKY_PASSWORD, retries=3, delay=2)

    while True:
        try:
            logger.info("🚀 BOT STARTED")
            scheduler(client)

        except Exception as e:
            logger.critical(f"💥 FATAL: {e}")
            time.sleep(10)

# =====================
# RUN
# =====================
if __name__ == "__main__":
    main()