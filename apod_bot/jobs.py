"""The two jobs: `daily` (latest APOD) and `flashback` (random archive APOD)."""
import logging
from datetime import date as Date
from typing import Callable, Optional

from .media import PreparedImage
from .nasa import Apod, NasaClient, NasaError
from .state import State
from .textfmt import build_post

log = logging.getLogger(__name__)


class JobError(Exception):
    pass


def _post(apod: Apod, mode: str, bsky, load_image: Callable[[Apod], PreparedImage],
          state: State, dry_run: bool) -> None:
    post = build_post(apod, mode)
    image = None
    if apod.media_type == "image":
        try:
            image = load_image(apod)
        except Exception as exc:
            raise JobError(f"image unavailable for {apod.date}: {exc}") from exc
    else:
        log.info("media_type=%r: posting text + link only", apod.media_type)
    alt = apod.alt or apod.title

    log.info("post text (%d chars):\n%s", len(post.text), post.text)
    if dry_run:
        log.info("DRY RUN: image=%s alt=%r link=%s",
                 f"{image.width}x{image.height} {len(image.data)}B" if image else None, alt[:80], post.url)
        return
    bsky.publish(post, image, alt)
    (state.record_daily if mode == "daily" else state.record_flashback)(apod.date)
    state.save()  # persist immediately after a successful post
    log.info("posted %s APOD %s", mode, apod.date)


def run_daily(nasa: NasaClient, bsky, state: State, load_image, dry_run: bool = False,
              on_date: Optional[Date] = None) -> None:
    apod = nasa.by_date(on_date) if on_date else nasa.latest()
    if apod is None:
        raise JobError(f"no APOD for {on_date}")
    if state.daily_last_date == apod.date and not on_date:
        log.info("APOD %s already posted; nothing to do", apod.date)
        return
    if not dry_run and apod.permalink in bsky.recent_links():
        log.info("APOD %s already on the account; updating state only", apod.date)
        state.record_daily(apod.date)
        state.save()
        return
    _post(apod, "daily", bsky, load_image, state, dry_run)


def run_flashback(nasa: NasaClient, bsky, state: State, load_image, dry_run: bool = False,
                  on_date: Optional[Date] = None, start_year: int = 1995,
                  end_year: Optional[int] = None) -> None:
    if on_date:
        apod = nasa.by_date(on_date)
        if apod is None:
            raise JobError(f"no APOD for {on_date}")
    else:
        recent = set() if dry_run else bsky.recent_links()
        try:
            apod = nasa.random_image_apod(set(state.flashback_history),
                                          accept=lambda a: a.permalink not in recent,
                                          start_year=start_year, end_year=end_year)
        except NasaError as exc:
            raise JobError(str(exc)) from exc
    _post(apod, "flashback", bsky, load_image, state, dry_run)
