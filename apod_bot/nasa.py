"""Client for NASA's APOD WordPress API (science.nasa.gov/wp-json/wp/v2/apod-basic).

Documented behaviour (NASA "APOD Feed And API User Guide"):
  list:   ?page=N&per_page=M   (per_page capped at 25, newest first)
  single: /apod-basic/YYMMDD
No API key is needed. On this API `url` is the article permalink, NOT an image;
the image is `hdurl` (a dynamicimage URL).
"""
import html
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Callable, List, Optional, Set
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BASE_URL = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"
FIRST_APOD = date(1995, 6, 20)
TIMEOUT = (10, 30)  # connect, read
_IMG_SRC = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class NasaError(Exception):
    pass


@dataclass
class Apod:
    date: str
    title: str
    explanation: str
    credit: str
    alt: str
    media_type: str
    permalink: str
    image_urls: List[str] = field(default_factory=list)


def make_session() -> requests.Session:
    """Session with timeouts handled by callers and backoff on 429/5xx (honours Retry-After)."""
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "apod-bluesky-bot/2.0 (+https://github.com/yuandevarizqullah12-sketch/apod-bluesky-bot)"
    return s


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p"):
            self.parts.append(" ")


def html_to_text(raw: Optional[str]) -> str:
    """Strip tags/entities and collapse whitespace (incl. non-breaking spaces)."""
    if not raw:
        return ""
    p = _Text()
    p.feed(raw)
    p.close()
    return re.sub(r"\s+", " ", html.unescape("".join(p.parts))).strip()


def clean_explanation(raw: Optional[str]) -> str:
    text = html_to_text(raw)
    text = re.sub(r"^Explanation:\s*", "", text)
    text = re.split(r"Tomorrow[’']s picture", text, maxsplit=1)[0]
    return text.strip()


def parse_apod(raw: dict) -> Apod:
    if not isinstance(raw, dict):
        raise NasaError("unexpected APOD payload type")
    try:
        d = date.fromisoformat(str(raw.get("date")))
    except ValueError as exc:
        raise NasaError(f"missing/invalid date: {raw.get('date')!r}") from exc
    title = html_to_text(raw.get("title"))
    if not title:
        raise NasaError(f"APOD {d} has no title")

    permalink = next((u for u in (raw.get("permalink"), raw.get("url"))
                      if isinstance(u, str) and u.startswith("https://")), "")
    if not permalink:
        permalink = f"https://science.nasa.gov/apod/?date={d:%y%m%d}"

    # Image candidates, best first: hdurl, then the <img> shown on the APOD page,
    # then `url` only if it really looks like an image file.
    urls: List[str] = []
    candidates = [raw.get("hdurl")]
    candidates += _IMG_SRC.findall(raw.get("basic_html") or "")
    candidates.append(raw.get("url"))
    for u in candidates:
        if not isinstance(u, str):
            continue
        u = html.unescape(u).strip()
        is_last = u == raw.get("url")
        if u.startswith("https://") and u not in urls and (
                not is_last or u.split("?")[0].lower().endswith(_IMG_EXT)):
            urls.append(u)

    return Apod(
        date=d.isoformat(),
        title=title,
        explanation=clean_explanation(raw.get("explanation")),
        credit=html_to_text(raw.get("credit") or raw.get("copyright")),
        alt=html_to_text(raw.get("alt")),
        media_type=str(raw.get("media_type") or "").lower(),
        permalink=permalink,
        image_urls=urls,
    )


class NasaClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or make_session()

    def _get(self, url: str, params: Optional[dict] = None, allow_404: bool = False):
        try:
            r = self.session.get(url, params=params, timeout=TIMEOUT)
            if allow_404 and r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            raise NasaError(f"APOD request failed: {type(exc).__name__}: {exc}") from exc

    def latest(self) -> Apod:
        data = self._get(BASE_URL, {"page": 1, "per_page": 5})
        if not isinstance(data, list):
            raise NasaError("APOD list response was not a list")
        items = []
        for raw in data:
            try:
                items.append(parse_apod(raw))
            except NasaError as exc:
                log.warning("skipping malformed APOD entry: %s", exc)
        if not items:
            raise NasaError("no valid APOD entries returned")
        return max(items, key=lambda a: a.date)  # don't rely on API ordering

    def by_date(self, d: date) -> Optional[Apod]:
        data = self._get(f"{BASE_URL}/{d:%y%m%d}", allow_404=True)
        if data is None:
            return None
        if isinstance(data, list):
            data = data[0] if data else None
        return parse_apod(data) if data else None

    def random_image_apod(self, exclude: Set[str], accept: Callable[[Apod], bool] = lambda a: True,
                          start_year: int = 1995, end_year: Optional[int] = None,
                          tries: int = 20, rng=random) -> Apod:
        yesterday_et = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
        lo = max(FIRST_APOD, date(start_year, 1, 1))
        hi = min(yesterday_et, date(end_year, 12, 31)) if end_year else yesterday_et
        if lo > hi:
            raise NasaError("empty flashback date range")
        span = (hi - lo).days
        for _ in range(tries):
            d = lo + timedelta(days=rng.randint(0, span))
            if d.isoformat() in exclude:
                continue
            try:
                apod = self.by_date(d)
            except NasaError as exc:
                log.warning("flashback candidate %s failed: %s", d, exc)
                continue
            if apod and apod.media_type == "image" and apod.image_urls and accept(apod):
                return apod
        raise NasaError(f"no usable flashback APOD found in {tries} tries")
