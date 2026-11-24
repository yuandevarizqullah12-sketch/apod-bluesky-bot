# apod-bluesky-bot

Posts NASA's Astronomy Picture of the Day (APOD) to Bluesky, with the image, real alt text, credit and a clickable link to the NASA page.

- **daily**: posts the latest APOD once (the workflow runs hourly 12:00-18:00 WIB; runs after the first success do nothing).
- **flashback**: posts a random image APOD from the archive.

## How it works

| Module | Job |
|---|---|
| `apod_bot/nasa.py` | NASA APOD WordPress API client (`science.nasa.gov/wp-json/wp/v2/apod-basic`), no API key needed |
| `apod_bot/textfmt.py` | Post text, max 300 graphemes, link facet |
| `apod_bot/media.py` | Download + re-encode image under Bluesky's 1 MB limit |
| `apod_bot/bluesky.py` | Login, duplicate check against recent posts, posting |
| `apod_bot/state.py` | `data.json` (last daily date, flashback history) |
| `apod_bot/jobs.py`, `cli.py` | Orchestration and command line |

On this API `url` is the article permalink, not an image. The image comes from `hdurl` (falling back to the `<img>` in `basic_html`).

## Configuration

Same names as before:

| Variable | Purpose |
|---|---|
| `BSKY_HANDLE`, `BSKY_PASSWORD` | Bluesky account (use an app password). Required except for `--dry-run` |
| `NASA_API_KEY` | Still accepted, no longer used: the new API is keyless |
| `FLASHBACK_START_YEAR`, `FLASHBACK_END_YEAR` | Optional range for flashbacks (default: whole archive up to yesterday) |

For GitHub Actions these are repository secrets. Locally, copy `.env.example` to `.env` (git-ignored).

## Usage

```bash
pip install -r requirements.txt
python apod.py daily --dry-run                  # fetch + format + prepare image; never logs in or posts
python apod.py flashback --dry-run --date 2005-03-02
python apod.py daily                            # real post
pip install -r requirements-dev.txt && pytest   # tests are fully mocked
```

Exit code is non-zero if the post fails, so the workflow run shows as failed (and the next hourly run retries).

## Reliability notes

- NASA requests: timeouts, exponential backoff on 429/5xx (honours `Retry-After`).
- Bluesky login and image upload are retried; the final post is not blindly retried (a timeout could hide a post that succeeded). Before every post the bot checks its own recent posts for the same NASA link, so a lost `data.json` cannot cause a duplicate.
- Workflows share one concurrency group, so state commits never race.
- Videos and other non-image entries are posted as text plus link.

## License

MIT
