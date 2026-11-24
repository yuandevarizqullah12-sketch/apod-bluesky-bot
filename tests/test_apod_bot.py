import json
import random
from datetime import date
from io import BytesIO

import pytest
from PIL import Image

from apod_bot import jobs
from apod_bot.media import MediaError, PreparedImage, prepare_image
from apod_bot.nasa import NasaClient, NasaError, clean_explanation, html_to_text, parse_apod
from apod_bot.state import State
from apod_bot.textfmt import LIMIT, build_post, glen
from tests.sample_data import JUPITER, VGR, VIDEO


def test_text_cleaning():
    assert html_to_text("a&nbsp;<b>b</b>&amp;\u00a0c<br>d") == "a b& c d"
    assert clean_explanation(JUPITER["explanation"]).startswith("What creates the colors")
    assert "Tomorrow" not in clean_explanation(JUPITER["explanation"])


def test_parse_uses_hdurl_not_permalink():
    a = parse_apod(JUPITER)
    assert a.image_urls[0] == JUPITER["hdurl"]
    assert JUPITER["url"] not in a.image_urls            # url is an article permalink here
    assert a.permalink == JUPITER["permalink"]
    assert "Matt Brealey" in a.credit and "<" not in a.credit


def test_parse_rejects_malformed():
    for bad in (None, [], {"date": "nope", "title": "x"}, {"date": "2026-01-01", "title": ""}):
        with pytest.raises(NasaError):
            parse_apod(bad)


def test_video_has_no_image():
    a = parse_apod(VIDEO)
    assert a.media_type == "video" and a.image_urls == []


@pytest.mark.parametrize("raw", [JUPITER, VGR, VIDEO])
@pytest.mark.parametrize("mode", ["daily", "flashback"])
def test_post_fits_and_links(raw, mode):
    a = parse_apod(raw)
    post = build_post(a, mode)
    assert glen(post.text) <= LIMIT
    b = post.to_builder()
    assert b.build_text() == post.text
    assert b.build_facets()[0].features[0].uri == a.permalink  # clickable link survives truncation


def test_prepare_image_fits_limit():
    noise = Image.frombytes("RGB", (3000, 2400), random.Random(1).randbytes(3000 * 2400 * 3))
    buf = BytesIO(); noise.save(buf, "PNG")
    out = prepare_image(buf.getvalue())
    assert len(out.data) <= 950_000 and max(out.width, out.height) <= 2000
    with pytest.raises(MediaError):
        prepare_image(b"<html>not an image</html>")


def test_state_migrates_legacy(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"last_date": "2026-11-21", "keys": ["h"], "is_posting": True,
                             "last_daily_post": "2026-11-21", "last_flashback_post": "2005-03-02"}))
    s = State(str(p))
    assert s.daily_last_date == "2026-11-21" and s.flashback_history == ["2005-03-02"]
    s.record_flashback("2010-01-01"); s.save()
    saved = json.loads(p.read_text())
    assert saved["version"] == 2 and "is_posting" not in saved and "keys" not in saved


class FakeNasa(NasaClient):
    def __init__(self): pass
    def latest(self): return parse_apod(JUPITER)
    def by_date(self, d): return parse_apod(VGR) if d == date(2026, 11, 20) else None


class FakeBsky:
    def __init__(self, recent=()): self.recent, self.posted = set(recent), []
    def recent_links(self): return self.recent
    def publish(self, post, image, alt): self.posted.append((post, image, alt))


IMG = lambda apod: PreparedImage(b"jpeg", 100, 80)


def test_daily_posts_once(tmp_path):
    st, bsky = State(str(tmp_path / "d.json")), FakeBsky()
    jobs.run_daily(FakeNasa(), bsky, st, IMG)
    jobs.run_daily(FakeNasa(), bsky, State(str(tmp_path / "d.json")), IMG)  # reloaded from disk
    assert len(bsky.posted) == 1
    assert bsky.posted[0][2].startswith("A busy field")   # real alt text, not "NASA APOD"


def test_daily_skips_if_already_on_account(tmp_path):
    st = State(str(tmp_path / "d.json"))
    bsky = FakeBsky(recent={JUPITER["permalink"]})
    jobs.run_daily(FakeNasa(), bsky, st, IMG)
    assert not bsky.posted and st.daily_last_date == "2026-11-22"


def test_image_failure_is_an_error_and_state_untouched(tmp_path):
    st, bsky = State(str(tmp_path / "d.json")), FakeBsky()
    def boom(apod): raise MediaError("down")
    with pytest.raises(jobs.JobError):
        jobs.run_daily(FakeNasa(), bsky, st, boom)
    assert not bsky.posted and st.daily_last_date is None


def test_dry_run_never_touches_bluesky_or_state(tmp_path):
    path = tmp_path / "d.json"
    st = State(str(path))
    jobs.run_daily(FakeNasa(), None, st, IMG, dry_run=True)   # bsky=None would crash if used
    assert not path.exists()
    jobs.run_flashback(FakeNasa(), None, st, IMG, dry_run=True, on_date=date(2026, 11, 20))
    assert not path.exists()
