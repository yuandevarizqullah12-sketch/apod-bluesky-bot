"""Tiny JSON state file (data.json), committed back to the repo by the workflow."""
import json
import logging
import os
import tempfile
from typing import List, Optional

log = logging.getLogger(__name__)
HISTORY_MAX = 500


class State:
    """v2 layout: {"version": 2, "daily": {"last_date": ...}, "flashback": {"history": [...]}}.
    Legacy (v1) files are migrated on load: last_daily_post / last_flashback_post are kept;
    the old text-hash `keys` and the `is_posting` flag are dropped."""

    def __init__(self, path: str):
        self.path = path
        self.daily_last_date: Optional[str] = None
        self.flashback_history: List[str] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); starting with empty state", self.path, exc)
            return
        if raw.get("version") == 2:
            self.daily_last_date = (raw.get("daily") or {}).get("last_date")
            self.flashback_history = list((raw.get("flashback") or {}).get("history") or [])
        else:
            self.daily_last_date = raw.get("last_daily_post") or raw.get("last_date")
            if raw.get("last_flashback_post"):
                self.flashback_history = [raw["last_flashback_post"]]

    def record_daily(self, date_str: str) -> None:
        self.daily_last_date = date_str

    def record_flashback(self, date_str: str) -> None:
        if date_str not in self.flashback_history:
            self.flashback_history.append(date_str)
        self.flashback_history = self.flashback_history[-HISTORY_MAX:]

    def save(self) -> None:
        payload = {"version": 2,
                   "daily": {"last_date": self.daily_last_date},
                   "flashback": {"history": self.flashback_history}}
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
