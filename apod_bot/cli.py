"""Command line entry point: python apod.py daily|flashback [--dry-run] [--date YYYY-MM-DD]."""
import argparse
import logging
import sys
from datetime import date

from . import jobs
from .bluesky import BlueskyError, BlueskyPublisher
from .config import ConfigError, load_config
from .media import MediaError, download_image, prepare_image
from .nasa import NasaClient, NasaError
from .state import State

log = logging.getLogger("apod_bot")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Post NASA APOD to Bluesky")
    p.add_argument("mode", choices=["daily", "flashback"])
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and format only; never logs in to or posts on Bluesky, never writes state")
    p.add_argument("--date", type=date.fromisoformat, help="use this APOD date instead of the default choice")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        cfg = load_config(require_bluesky=not args.dry_run)
        nasa = NasaClient()
        state = State(cfg.state_file)
        bsky = None
        if not args.dry_run:
            bsky = BlueskyPublisher()
            bsky.login(cfg.bsky_handle, cfg.bsky_password)

        def load_image(apod):
            return prepare_image(download_image(nasa.session, apod.image_urls))

        if args.mode == "daily":
            jobs.run_daily(nasa, bsky, state, load_image, args.dry_run, args.date)
        else:
            jobs.run_flashback(nasa, bsky, state, load_image, args.dry_run, args.date,
                               cfg.flashback_start_year, cfg.flashback_end_year)
        return 0
    except (ConfigError, NasaError, MediaError, BlueskyError, jobs.JobError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
