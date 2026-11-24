"""Builds the Bluesky post text (<= 300 graphemes) with a clickable NASA link."""
import re
from dataclasses import dataclass
from datetime import date

import grapheme
from atproto import client_utils

from .nasa import Apod

LIMIT = 300  # Bluesky counts graphemes, not bytes or code points
LINK_LABEL = "NASA APOD"


def glen(s: str) -> int:
    return grapheme.length(s)


def cut(s: str, limit: int) -> str:
    """Shorten to <= limit graphemes, preferring a word boundary, adding an ellipsis."""
    if glen(s) <= limit:
        return s
    s = grapheme.slice(s, 0, max(limit - 1, 0))
    i = s.rfind(" ")
    if i > len(s) * 0.6:
        s = s[:i]
    return s.rstrip(" ,;:-\u2013\u2014") + "\u2026"


def summarize(text: str, limit: int, target: int = 140) -> str:
    """Whole sentences up to `limit`, stopping once `target` is reached; else a clean cut."""
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        cand = f"{out} {sentence}".strip()
        if glen(cand) > limit:
            break
        out = cand
        if glen(out) >= target:
            break
    return out or cut(text, limit)


@dataclass
class Post:
    text: str
    url: str

    def to_builder(self) -> client_utils.TextBuilder:
        prefix = self.text[: -len(LINK_LABEL)]
        return client_utils.TextBuilder().text(prefix).link(LINK_LABEL, self.url)


def build_post(apod: Apod, mode: str = "daily") -> Post:
    title = cut(apod.title, 100)
    if mode == "flashback":
        d = date.fromisoformat(apod.date)
        head = f"\U0001F570\uFE0F {title} \u00b7 {d.strftime('%b')} {d.day}, {d.year}"
    else:
        head = f"\U0001F30C {title}"

    tail = []
    if apod.credit:
        tail.append("\U0001F4F7 " + cut(apod.credit, 70))
    tail.append(f"\U0001F517 {LINK_LABEL}")
    tail_text = "\n".join(tail)

    budget = LIMIT - glen(head) - glen(tail_text) - 4  # two "\n\n" separators
    blurb = summarize(apod.explanation, min(budget, 170)) if budget >= 40 and apod.explanation else ""
    text = head + ("\n\n" + blurb if blurb else "") + "\n\n" + tail_text
    if glen(text) > LIMIT:  # last-resort safety net
        text = cut(head, LIMIT - glen(tail_text) - 2) + "\n\n" + tail_text
    return Post(text=text, url=apod.permalink)
